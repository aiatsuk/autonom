"""Mobile Canvas scrcpy and idb: the bridge `record` op, including display
changes and the iOS idb transport, `canvas serve` flags, and the optional
scrcpy in doctor and bootstrap (CANVAS-009, CANVAS-013, DISPLAY-006, IOSF-004).

Fakes only. The device tool handed to the bridge and the CLI is a sentinel
that logs any call, `node`, `scrcpy`, `uname`, `brew`, `sudo` and `apt-get`
are scripts in a temporary directory, and every PATH a child process sees
holds nothing but those scripts.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "autonom.py"
BRIDGE = SCRIPTS / "autonom_canvas_bridge.py"
BOOTSTRAP = SCRIPTS / "bootstrap.sh"

sys.path.insert(0, str(SCRIPTS))
import autonom_canvas_bridge as bridge  # noqa: E402
from autonom_lib import actions, doctor, errors, session, ui  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

SERIAL = "fake-canvas-serial"
UDID = "00000000-0000-0000-0000-00000000CAFE"
SECRET = "clipboard-SECRET-7f3a"
# Variables that would point a child process at a real tool on this machine.
TOOL_ENV = ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_MITMDUMP",
            "AUTONOM_AXE", "AUTONOM_IOS_HID", "AUTONOM_IDB_COMPANION", "AUTONOM_IDB_COMPANION_BIN",
            "AUTONOM_IDB_STATE_FILE", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG",
            "AUTONOM_EMULATOR", "DEVELOPER_DIR",
            "AUTONOM_SCRCPY_SERVER", "SCRCPY_SERVER_PATH")


def write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def sentinel(path: Path, trace: Path, code: int = 1) -> Path:
    """A tool that only appends its name and argv to `trace`."""
    return write_script(path, f'#!/bin/sh\necho "{path.name} $*" >> "{trace}"\nexit {code}\n')


def fake_scrcpy(path: Path, version: str) -> Path:
    return write_script(path, (
        "#!/bin/sh\n"
        f'echo "scrcpy {version} <https://github.com/Genymobile/scrcpy>"\n'
        'echo ""\n'
        'echo "Dependencies (compiled / linked):"\n'))


def hermetic_env(home: Path, path: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in TOOL_ENV}
    env.update({"AUTONOM_HOME": str(home), "PATH": path})
    return env


# --- bridge `record` op ------------------------------------------------------------


GESTURE = {"kind": "gesture", "transport": "scrcpy", "pointers": 1, "moves": 40,
           "duration_ms": 812.4, "start": [100, 900], "end": [110.4, 300]}
DISPLAY = {"kind": "display", "transport": "scrcpy", "preset": "tablet",
           "width": 2560, "height": 1600, "density": 320}
DISPLAY_FIELDS = {"kind", "origin", "canvas", "transport", "preset", "width", "height",
                  "density"}
ONE_OF_EACH = [
    (GESTURE, "human"),
    ({"kind": "scroll", "transport": "scrcpy", "events": 12, "dx": 0, "dy": -48.5}, "human"),
    ({"kind": "key", "transport": "scrcpy", "key": "KEYCODE_ENTER"}, "agent"),
    ({"kind": "text", "transport": "scrcpy", "text": "Café ✓", "text_len": 6}, "replay"),
    ({"kind": "paste", "transport": "scrcpy", "text_len": 21}, "human"),
    ({"kind": "system", "transport": "scrcpy", "op": "app-switch"}, "system"),
    ({"kind": "control", "transport": "scrcpy", "mode": "takeover", "owner": "agent"}, "agent"),
    (DISPLAY, "human"),
]


class BridgeRecordTests(EnvSandboxMixin, unittest.TestCase):
    """CANVAS-009: one journal entry per completed action, nothing actuated."""

    def setUp(self) -> None:
        self.sandbox_home()
        self.record = session.start_session("/nonexistent/adb", serial=SERIAL,
                                            app_id="com.example.app")
        self.target = Target(ANDROID, SERIAL, "/nonexistent/adb", {"serial": SERIAL})
        # Any actuator or child process reached by `record` fails the test.
        for name in ("tap", "swipe", "press_key", "type_text", "screen_size"):
            patcher = mock.patch.object(ui, name, side_effect=AssertionError(f"ui.{name} ran"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("run", "Popen"):
            patcher = mock.patch.object(subprocess, name,
                                        side_effect=AssertionError(f"subprocess.{name} ran"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def send(self, payload: object, origin: str = "human") -> dict:
        return bridge.dispatch(self.target, {"id": 7, "op": "record", "origin": origin,
                                             "payload": payload})

    def journal(self) -> list[dict]:
        path = Path(self.record["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def details(self) -> list[dict]:
        return list(actions.read_details(self.record).values())

    def everything_written(self) -> str:
        root = Path(self.record["artifacts_dir"])
        return "\n".join(path.read_text(encoding="utf-8")
                         for path in sorted(root.rglob("*")) if path.is_file())

    def assert_refused(self, payload: object, origin: str = "human") -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            self.send(payload, origin)
        self.assertEqual(caught.exception.code, errors.FLOW_COMMAND_INVALID)

    def test_one_drag_with_forty_moves_is_one_gesture_entry(self) -> None:
        """CANVAS-009-S01."""
        result = self.send(GESTURE)
        self.assertEqual(result, {"ok": True, "recorded": "gesture", "via": "scrcpy",
                                  "detail": "actions/0001_canvas-gesture.json"})
        (entry,) = self.journal()
        self.assertEqual(
            {key: entry[key] for key in ("kind", "verb", "origin", "ok", "result")},
            {"kind": "action", "verb": "ui gesture", "origin": "human", "ok": True,
             "result": {"via": "scrcpy", "detail": "actions/0001_canvas-gesture.json"}})
        self.assertEqual(self.details(), [{
            "kind": "gesture", "origin": "human", "canvas": True, "transport": "scrcpy",
            "pointers": 1, "moves": 40, "duration_ms": 812,
            "start": [100, 900], "end": [110, 300]}])

    def test_each_kind_journals_exactly_one_entry_with_its_origin(self) -> None:
        for payload, origin in ONE_OF_EACH:
            self.send(payload, origin)
        entries = self.journal()
        self.assertEqual([(entry["verb"], entry["origin"]) for entry in entries],
                         [(f"ui {payload['kind']}", origin) for payload, origin in ONE_OF_EACH])
        self.assertEqual([entry["seq"] for entry in entries],
                         list(range(1, len(ONE_OF_EACH) + 1)))
        details = self.details()
        self.assertEqual([detail["kind"] for detail in details], list(bridge.RECORD_KINDS))
        self.assertTrue(all(detail["transport"] == "scrcpy" for detail in details))
        by_kind = {detail["kind"]: detail for detail in details}
        self.assertEqual((by_kind["scroll"]["events"], by_kind["scroll"]["dx"],
                          by_kind["scroll"]["dy"]), (12, 0.0, -48.5))
        self.assertEqual(by_kind["key"]["key"], "KEYCODE_ENTER")
        self.assertEqual(by_kind["text"]["text"], "Café ✓")
        self.assertEqual(by_kind["system"]["op"], "app-switch")
        self.assertEqual((by_kind["control"]["mode"], by_kind["control"]["owner"]),
                         ("takeover", "agent"))
        self.assertEqual({key: by_kind["display"][key] for key in
                          ("preset", "width", "height", "density")},
                         {"preset": "tablet", "width": 2560, "height": 1600, "density": 320})

    def test_transport_defaults_to_scrcpy_and_key_numbers_are_kept(self) -> None:
        self.send({"kind": "key", "key": 66}, "agent")
        (detail,) = self.details()
        self.assertEqual((detail["transport"], detail["key"]), ("scrcpy", 66))

    def test_paste_never_journals_clipboard_text(self) -> None:
        self.send({"kind": "paste", "transport": "scrcpy", "text": SECRET})
        self.send({"kind": "paste", "transport": "scrcpy", "text_len": 5, "text": SECRET})
        self.assertEqual([detail["text_len"] for detail in self.details()], [len(SECRET), 5])
        self.assertTrue(all("text" not in detail for detail in self.details()))
        self.assertEqual(len(self.journal()), 2)
        self.assertNotIn(SECRET, self.everything_written())

    def test_sensitive_text_keeps_only_its_length(self) -> None:
        self.send({"kind": "text", "transport": "scrcpy", "text": SECRET, "sensitive": True})
        (detail,) = self.details()
        self.assertEqual((detail["sensitive"], detail["text"], detail["text_len"]),
                         (True, None, len(SECRET)))
        self.assertNotIn(SECRET, self.everything_written())

    def test_fields_a_kind_does_not_name_are_dropped(self) -> None:
        self.send({**GESTURE, "clipboard": SECRET, "text": SECRET, "raw": [SECRET]})
        self.send({"kind": "system", "op": "home", "text": SECRET})
        gesture, system = self.details()
        self.assertEqual(set(gesture), {"kind", "origin", "canvas", "transport", "pointers",
                                        "moves", "duration_ms", "start", "end"})
        self.assertEqual(set(system), {"kind", "origin", "canvas", "transport", "op"})
        self.assertNotIn(SECRET, self.everything_written())

    def test_kinds_outside_the_set_are_refused_without_an_entry(self) -> None:
        for kind in ("tap", "swipe", "clipboard", "clipboard-get", "", None, "GESTURE", 3):
            with self.subTest(kind=kind):
                self.assert_refused({"kind": kind, "transport": "scrcpy"})
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.details(), [])

    def test_malformed_records_are_refused_without_an_entry(self) -> None:
        bad = [
            ["not", "an", "object"],
            {**GESTURE, "moves": -1},
            {**GESTURE, "moves": "40"},
            {**GESTURE, "moves": True},
            {**GESTURE, "moves": 2.5},
            {**GESTURE, "pointers": 101},
            {**GESTURE, "duration_ms": -3},
            {**GESTURE, "start": [1]},
            {**GESTURE, "end": [float("nan"), 1]},
            {**GESTURE, "end": [-5, 1]},
            {"kind": "scroll", "dx": float("inf")},
            {"kind": "key"},
            {"kind": "key", "key": "KEYCODE ENTER"},
            {"kind": "key", "key": -1},
            {"kind": "text", "text": 42},
            {"kind": "text", "text": "a", "sensitive": "yes"},
            {"kind": "text", "text": "x" * (bridge.MAX_TEXT + 1)},
            {"kind": "paste", "text_len": -1},
            {"kind": "system", "op": "reboot"},
            {"kind": "control", "mode": "steal"},
            {"kind": "control", "mode": "takeover", "owner": "root"},
            {**GESTURE, "transport": "screencap"},
            {**GESTURE, "transport": None},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assert_refused(payload)
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.details(), [])

    def test_origin_is_still_required(self) -> None:
        for payload in (GESTURE, DISPLAY):
            for origin in ("admin", "", None, "Human"):
                with self.subTest(kind=payload["kind"], origin=origin):
                    self.assert_refused(payload, origin)
        self.assertEqual(self.journal(), [])

    def test_ios_idb_records_of_every_streamed_kind_name_idb(self) -> None:
        """IOSF-004: one entry per completed action on the iOS fast transport."""
        for payload, origin in ONE_OF_EACH:
            if payload["kind"] == "display":
                continue
            with self.subTest(kind=payload["kind"]):
                result = self.send({**payload, "transport": "idb"}, origin)
                self.assertEqual((result["recorded"], result["via"]), (payload["kind"], "idb"))
        self.assertEqual([(entry["verb"], entry["origin"]) for entry in self.journal()],
                         [(f"ui {payload['kind']}", origin) for payload, origin in ONE_OF_EACH
                          if payload["kind"] != "display"])
        self.assertTrue(all(detail["transport"] == "idb" for detail in self.details()))

    def test_ios_idb_never_names_a_display_change(self) -> None:
        """Display presets are Android-only, so no `display` record names idb."""
        self.assert_refused({**DISPLAY, "transport": "idb"})
        self.assertEqual(self.journal(), [])

    def test_without_a_session_record_still_answers(self) -> None:
        session.stop_session(reap=False)
        self.assertEqual(self.send(GESTURE), {"ok": True, "recorded": "gesture",
                                              "via": "scrcpy"})

    # --- DISPLAY-006: `ui display` records --------------------------------------------

    def test_a_display_change_is_one_entry_with_its_preset_and_size(self) -> None:
        result = self.send(DISPLAY)
        self.assertEqual(result, {"ok": True, "recorded": "display", "via": "scrcpy",
                                  "detail": "actions/0001_canvas-display.json"})
        (entry,) = self.journal()
        self.assertEqual(
            {key: entry[key] for key in ("kind", "verb", "argv", "origin", "ok", "result")},
            {"kind": "action", "verb": "ui display", "argv": ["ui", "display", "<canvas>"],
             "origin": "human", "ok": True,
             "result": {"via": "scrcpy", "detail": "actions/0001_canvas-display.json"}})
        self.assertEqual(self.details(), [{
            "kind": "display", "origin": "human", "canvas": True, "transport": "scrcpy",
            "preset": "tablet", "width": 2560, "height": 1600, "density": 320}])

    def test_two_changes_are_exactly_two_entries_with_their_presets_and_sizes(self) -> None:
        """DISPLAY-006-S01: the owner applies `small`, then `tablet`."""
        self.send({**DISPLAY, "preset": "small", "width": 720, "height": 1280,
                   "density": 320})
        self.send(DISPLAY)
        self.assertEqual([entry["verb"] for entry in self.journal()],
                         ["ui display", "ui display"])
        self.assertEqual([(detail["preset"], detail["width"], detail["height"],
                           detail["density"]) for detail in self.details()],
                         [("small", 720, 1280, 320), ("tablet", 2560, 1600, 320)])

    def test_the_stop_time_restore_is_one_system_entry(self) -> None:
        self.send({**DISPLAY, "preset": "restore", "width": 1080, "height": 2400,
                   "density": 420}, "system")
        (entry,) = self.journal()
        self.assertEqual((entry["verb"], entry["origin"]), ("ui display", "system"))
        (detail,) = self.details()
        self.assertEqual((detail["preset"], detail["origin"], detail["width"],
                          detail["height"], detail["density"]),
                         ("restore", "system", 1080, 2400, 420))

    def test_every_preset_transport_and_origin_is_accepted(self) -> None:
        sent = [(preset, transport, origin)
                for preset in bridge.DISPLAY_PRESETS
                for transport in ("scrcpy", "screenrecord", "screencap")
                for origin in bridge.ORIGINS]
        for preset, transport, origin in sent:
            with self.subTest(preset=preset, transport=transport, origin=origin):
                result = self.send({**DISPLAY, "preset": preset, "transport": transport},
                                   origin)
                self.assertEqual((result["recorded"], result["via"]), ("display", transport))
        self.assertEqual(bridge.DISPLAY_PRESETS,
                         ("small", "pixel-11", "pixel-fold", "tablet", "default", "restore"))
        self.assertEqual([(entry["verb"], entry["origin"]) for entry in self.journal()],
                         [("ui display", origin) for _preset, _transport, origin in sent])
        self.assertEqual([(detail["preset"], detail["transport"], detail["origin"])
                          for detail in self.details()], sent)

    def test_a_display_record_without_a_transport_names_scrcpy(self) -> None:
        payload = {key: value for key, value in DISPLAY.items() if key != "transport"}
        self.assertEqual(self.send(payload, "agent")["via"], "scrcpy")
        (detail,) = self.details()
        self.assertEqual(detail["transport"], "scrcpy")

    def test_display_sizes_at_the_bounds_are_kept(self) -> None:
        self.send({**DISPLAY, "width": 1, "height": 10_000, "density": 1})
        self.send({**DISPLAY, "width": 10_000, "height": 1, "density": 1_000})
        self.send({**DISPLAY, "width": 2560.0, "height": 1600.0, "density": 320.0})
        self.assertEqual([(detail["width"], detail["height"], detail["density"])
                          for detail in self.details()],
                         [(1, 10_000, 1), (10_000, 1, 1_000), (2560, 1600, 320)])
        self.assertTrue(all(type(detail[name]) is int for detail in self.details()
                            for name in ("width", "height", "density")))

    def test_a_display_record_without_a_read_back_keeps_only_its_preset(self) -> None:
        """A restore whose read-back failed still leaves its record."""
        self.send({"kind": "display", "preset": "restore"}, "system")
        self.send({"kind": "display", "preset": "default", "density": 420})
        restore, default = self.details()
        self.assertEqual(set(restore), DISPLAY_FIELDS - {"width", "height", "density"})
        self.assertEqual(set(default), DISPLAY_FIELDS - {"width", "height"})
        self.assertEqual(len(self.journal()), 2)

    def test_display_fields_outside_the_allowlist_are_dropped(self) -> None:
        self.send({**DISPLAY, "label": "Tablet", "text": SECRET, "serial": SECRET,
                   "command": f"wm size 2560x1600 {SECRET}", "physical": [1080, 2400],
                   "override": {"size": SECRET}})
        (detail,) = self.details()
        self.assertEqual(set(detail), DISPLAY_FIELDS)
        self.assertNotIn(SECRET, self.everything_written())

    def test_malformed_display_records_are_refused_without_an_entry(self) -> None:
        bad = [{"kind": "display"},
               {**DISPLAY, "preset": None}]
        bad += [{**DISPLAY, "preset": preset} for preset in (
            "huge", "", "Tablet", "TABLET", "pixel_11", "pixel11", "custom", "null", 3,
            True, ["tablet"], {"id": "tablet"})]
        for name, maximum in (("width", 10_000), ("height", 10_000), ("density", 1_000)):
            bad += [{**DISPLAY, name: value} for value in (
                0, -1, maximum + 1, 2.5, str(maximum), True, False, float("nan"),
                float("inf"), [maximum], {"value": maximum})]
        bad += [{key: value for key, value in DISPLAY.items() if key != "height"},
                {key: value for key, value in DISPLAY.items() if key != "width"},
                {**DISPLAY, "height": None},
                {**DISPLAY, "width": None}]
        bad += [{**DISPLAY, "transport": transport} for transport in (
            "webrtc", "mjpeg", "", None, "SCRCPY", 0, ["scrcpy"], "idb", "IDB")]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assert_refused(payload)
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.details(), [])

    def test_other_kinds_still_name_only_the_scrcpy_transport(self) -> None:
        """Only a display change happens on the multipart transports too."""
        for transport in ("screenrecord", "screencap"):
            for payload, origin in ONE_OF_EACH:
                if payload["kind"] == "display":
                    continue
                with self.subTest(kind=payload["kind"], transport=transport):
                    self.assert_refused({**payload, "transport": transport}, origin)
        self.assertEqual(self.journal(), [])


OTHER_SERIAL = "fake-other-serial"
# Every bridge op that journals: the `record` op (a scrcpy gesture and a display
# change) and the four the bridge actuates itself, each with the journal verb it writes.
JOURNALED_OPS = [
    ("record", GESTURE, "ui gesture"),
    ("record", {**DISPLAY, "transport": "screencap"}, "ui display"),
    ("tap", {"x": 10, "y": 20}, "ui tap"),
    ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 300}, "ui swipe"),
    ("key", {"key": "KEYCODE_BACK"}, "ui key"),
    ("text", {"text": "hello"}, "ui text"),
]


class BridgeSessionTargetTests(EnvSandboxMixin, unittest.TestCase):
    """Canvas journals into the current session only when that session is on
    the Canvas's own target. Before: a Canvas on one emulator wrote its actions
    into the journal of a session on another."""

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.target = Target(ANDROID, SERIAL, "/nonexistent/adb", {"serial": SERIAL})
        self.actuated: list[tuple[str, str]] = []
        for name in ("tap", "swipe", "press_key", "type_text"):
            patcher = mock.patch.object(ui, name, side_effect=self.actuator(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("run", "Popen"):
            patcher = mock.patch.object(subprocess, name,
                                        side_effect=AssertionError(f"subprocess.{name} ran"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def actuator(self, name: str):
        def actuate(target: Target, *_args: object) -> None:
            self.actuated.append((name, target.target_id))
        return actuate

    def dispatch_each(self) -> list[dict]:
        return [bridge.dispatch(self.target, {"id": index, "op": op, "origin": "human",
                                              "payload": payload})
                for index, (op, payload, _verb) in enumerate(JOURNALED_OPS)]

    def journal(self, record: dict) -> list[dict]:
        path = Path(record["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assert_ran_on_the_canvas_target(self, results: list[dict]) -> None:
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(self.actuated, [("tap", SERIAL), ("swipe", SERIAL),
                                         ("press_key", SERIAL), ("type_text", SERIAL)])

    def assert_nothing_journaled(self, results: list[dict], record: dict) -> None:
        self.assertTrue(all("detail" not in result for result in results), results)
        self.assertEqual(self.journal(record), [])
        self.assertEqual(actions.read_details(record), {})

    def test_a_session_on_the_canvas_target_gets_one_line_per_action(self) -> None:
        record = session.start_session("/nonexistent/adb", serial=SERIAL)
        for op, payload, verb in JOURNALED_OPS:
            with self.subTest(op=op):
                before = len(self.journal(record))
                result = bridge.dispatch(self.target, {"id": 1, "op": op, "origin": "human",
                                                       "payload": payload})
                self.assertTrue(result["ok"])
                self.assertIn("detail", result)
                entries = self.journal(record)
                self.assertEqual(len(entries), before + 1)
                self.assertEqual((entries[-1]["verb"], entries[-1]["origin"]),
                                 (verb, "human"))
        self.assertEqual(len(actions.read_details(record)), len(JOURNALED_OPS))

    def test_a_session_on_another_target_gets_nothing_and_the_action_still_runs(self) -> None:
        record = session.start_session("/nonexistent/adb", serial=OTHER_SERIAL)
        results = self.dispatch_each()
        self.assert_ran_on_the_canvas_target(results)
        self.assert_nothing_journaled(results, record)

    def test_a_session_on_another_platform_with_the_same_id_gets_nothing(self) -> None:
        record = session.start_session("/nonexistent/xcrun", platform="ios",
                                       target_id=SERIAL)
        results = self.dispatch_each()
        self.assert_ran_on_the_canvas_target(results)
        self.assert_nothing_journaled(results, record)

    def test_a_v1_record_on_the_canvas_target_still_journals(self) -> None:
        """A v1 record names only its serial; it is upgraded to an Android one."""
        artifacts = self.home / "sessions" / "s_v1record"
        artifacts.mkdir(parents=True)
        record = {"session_id": "s_v1record", "serial": SERIAL, "adb": "/nonexistent/adb",
                  "artifacts_dir": str(artifacts)}
        (self.home / "sessions" / "current.json").write_text(json.dumps(record),
                                                              encoding="utf-8")
        self.dispatch_each()
        self.assertEqual([entry["verb"] for entry in self.journal(record)],
                         [verb for _op, _payload, verb in JOURNALED_OPS])

    def test_without_a_session_every_action_answers_and_nothing_is_journaled(self) -> None:
        results = self.dispatch_each()
        self.assert_ran_on_the_canvas_target(results)
        self.assertTrue(all("detail" not in result for result in results), results)
        self.assertEqual([path for path in self.home.rglob("*") if path.is_file()], [])


class BridgeIosTextTests(EnvSandboxMixin, unittest.TestCase):
    """IOSF-004: text from the idb transport's control socket goes through the
    bridge `text` op, which types it and journals one `ui text` entry naming idb."""

    def setUp(self) -> None:
        self.sandbox_home()
        self.typed: list[str] = []
        patcher = mock.patch.object(ui, "type_text",
                                    side_effect=lambda target, text: self.typed.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = Target(IOS, UDID, "/nonexistent/xcrun", {"udid": UDID})
        self.record = session.start_session("/nonexistent/xcrun", platform="ios", target_id=UDID)

    def details(self) -> list[dict]:
        return list(actions.read_details(self.record).values())

    def type(self, payload: dict) -> dict:
        return bridge.dispatch(self.target, {"id": 1, "op": "text", "origin": "human",
                                             "payload": payload})

    def test_idb_text_is_typed_and_journaled_once_with_its_transport(self) -> None:
        self.assertTrue(self.type({"text": "wifi", "transport": "idb"})["ok"])
        self.assertTrue(self.type({"text": SECRET, "sensitive": True, "transport": "idb"})["ok"])
        self.assertEqual(self.typed, ["wifi", SECRET])
        first, second = self.details()
        self.assertEqual((first["transport"], first["text"], first["text_len"]), ("idb", "wifi", 4))
        self.assertEqual((second["transport"], second["text"], second["sensitive"]),
                         ("idb", None, True))
        journal = Path(self.record["artifacts_dir"]) / "journal.ndjson"
        self.assertEqual([json.loads(line)["verb"] for line in
                          journal.read_text(encoding="utf-8").splitlines()], ["ui text", "ui text"])
        self.assertNotIn(SECRET, journal.read_text(encoding="utf-8"))

    def test_text_without_a_known_transport_names_none(self) -> None:
        for payload in ({"text": "a"}, {"text": "b", "transport": "webrtc"},
                        {"text": "c", "transport": ["idb"]}):
            self.type(payload)
        self.assertTrue(all("transport" not in detail for detail in self.details()))


class BridgeIosButtonTests(EnvSandboxMixin, unittest.TestCase):
    """The page's Home and Power buttons send Android key names. Before: iOS
    refused them, so a Canvas on a Simulator had no way to press Home."""

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.pressed: list[tuple[str, str]] = []
        patcher = mock.patch.object(ui, "press_key",
                                    side_effect=lambda target, key: self.pressed.append((target.platform, key)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def press(self, target: Target, key: str) -> dict:
        return bridge.dispatch(target, {"id": 1, "op": "key", "origin": "human", "payload": {"key": key}})

    def test_ios_home_and_power_become_ios_buttons(self) -> None:
        ios = Target(IOS, "FAKE-UDID", "/nonexistent/xcrun", {"udid": "FAKE-UDID"})
        for key in ("KEYCODE_HOME", "KEYCODE_POWER", "KEYCODE_BACK", "HOME"):
            self.assertTrue(self.press(ios, key)["ok"])
        self.assertEqual(self.pressed, [(IOS, "HOME"), (IOS, "LOCK"), (IOS, "KEYCODE_BACK"), (IOS, "HOME")])

    def test_android_keys_are_unchanged(self) -> None:
        android = Target(ANDROID, SERIAL, "/nonexistent/adb", {"serial": SERIAL})
        for key in ("KEYCODE_HOME", "KEYCODE_POWER"):
            self.assertTrue(self.press(android, key)["ok"])
        self.assertEqual(self.pressed, [(ANDROID, "KEYCODE_HOME"), (ANDROID, "KEYCODE_POWER")])


class BridgeProcessTests(EnvSandboxMixin, unittest.TestCase):
    def test_the_bridge_process_records_without_running_the_device_tool(self) -> None:
        home = self.sandbox_home()
        record = session.start_session("adb", serial=SERIAL, app_id="com.example.app")
        trace = home / "device-tool.log"
        adb = sentinel(home / "bin" / "adb", trace)
        messages = [
            {"id": 1, "op": "record", "origin": "human", "payload": GESTURE},
            {"id": 2, "op": "record", "origin": "human",
             "payload": {"kind": "paste", "transport": "scrcpy", "text": SECRET}},
            {"id": 3, "op": "record", "origin": "agent",
             "payload": {"kind": "tap", "transport": "scrcpy"}},
            {"id": 4, "op": "record", "origin": "system",
             "payload": {**DISPLAY, "preset": "restore", "transport": "screenrecord"}},
            {"id": 5, "op": "record", "origin": "agent",
             "payload": {**DISPLAY, "preset": "huge"}},
        ]
        completed = subprocess.run(
            [sys.executable, str(BRIDGE), "--platform", "android", "--target", SERIAL,
             "--tool", str(adb)],
            input="".join(json.dumps(message) + "\n" for message in messages),
            env=hermetic_env(home, str(adb.parent)), text=True, capture_output=True,
            check=False, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = {reply["id"]: reply for reply in map(json.loads,
                                                       completed.stdout.splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5])
        self.assertTrue(replies[1]["ok"] and replies[2]["ok"] and replies[4]["ok"])
        self.assertEqual(replies[1]["result"]["recorded"], "gesture")
        self.assertEqual((replies[4]["result"]["recorded"], replies[4]["result"]["via"]),
                         ("display", "screenrecord"))
        for refused in (3, 5):
            self.assertEqual((replies[refused]["ok"], replies[refused]["error_code"]),
                             (False, errors.FLOW_COMMAND_INVALID))
        self.assertIn("small, pixel-11, pixel-fold, tablet, default, restore",
                      replies[5]["error"])
        self.assertFalse(trace.exists(), "the record op reached the device tool")
        journal = Path(record["artifacts_dir"]) / "journal.ndjson"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(line["verb"], line["origin"]) for line in lines],
                         [("ui gesture", "human"), ("ui paste", "human"),
                          ("ui display", "system")])
        written = "".join(path.read_text(encoding="utf-8")
                          for path in Path(record["artifacts_dir"]).rglob("*.json*"))
        self.assertNotIn(SECRET, written + completed.stdout)


# --- canvas serve flags ------------------------------------------------------------


FAKE_NODE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_NODE_OUT"], "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle)
if os.environ.get("FAKE_NODE_ENV"):
    with open(os.environ["FAKE_NODE_ENV"], "w", encoding="utf-8") as handle:
        json.dump({{key: os.environ.get(key) for key in
                   ("AUTONOM_IDB_COMPANION", "AUTONOM_IDB_COMPANION_BIN")}}, handle)
"""


def flag_values(argv: list[str]) -> dict[str, str]:
    return {flag: value for flag, value in zip(argv, argv[1:])
            if flag.startswith("--") and not value.startswith("--")}


class CanvasServeFlagsTests(EnvSandboxMixin, unittest.TestCase):
    """CANVAS-013: `canvas serve` accepts the scrcpy options and forwards them."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.trace = self.root / "device-tool.log"
        self.adb = sentinel(self.root / "bin" / "adb", self.trace)
        self.simctl = sentinel(self.root / "bin" / "xcrun", self.trace)
        write_script(self.root / "bin" / "node", FAKE_NODE.format(python=sys.executable))
        # The supervisor lists its process group with `ps` once node exits;
        # without it every run waits out the 5 s stop timeout.
        ps = shutil.which("ps")
        if ps:
            (self.root / "bin" / "ps").symlink_to(ps)
        self.node_out = self.root / "node-argv.json"
        self.server = self.root / "scrcpy-server-v4.1"
        self.server.write_bytes(b"PK\x03\x04 not a real server")
        self.env = hermetic_env(self.root / "home", str(self.root / "bin"))
        self.env["FAKE_NODE_OUT"] = str(self.node_out)

    def serve(self, *argv: str, ios: bool = False) -> subprocess.CompletedProcess:
        target = (["--simctl", str(self.simctl), "--udid", UDID] if ios
                  else ["--adb", str(self.adb), "--serial", SERIAL])
        completed = subprocess.run(
            [sys.executable, str(CLI), *target, "canvas", "serve", *argv],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=60)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        self.assertFalse(self.trace.exists(), "canvas serve ran the device tool itself")
        return completed

    def node_argv(self, completed: subprocess.CompletedProcess) -> list[str]:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(self.node_out.read_text(encoding="utf-8"))

    def refused(self, completed: subprocess.CompletedProcess) -> dict:
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertFalse(self.node_out.exists(), "node started despite a bad value")
        return json.loads(completed.stderr)

    def test_scrcpy_flags_reach_the_canvas_server(self) -> None:
        argv = self.node_argv(self.serve(
            "--transport", "scrcpy", "--scrcpy-server", str(self.server),
            "--scrcpy-version", "4.1", "--max-size", "1024", "--bit-rate", "4000000",
            "--port", "3999"))
        values = flag_values(argv)
        self.assertEqual(
            {flag: values.get(flag) for flag in (
                "--platform", "--target", "--port", "--transport", "--scrcpy-server",
                "--scrcpy-version", "--max-size", "--bit-rate", "--adb")},
            {"--platform": "android", "--target": SERIAL, "--port": "3999",
             "--transport": "scrcpy", "--scrcpy-server": str(self.server),
             "--scrcpy-version": "4.1", "--max-size": "1024", "--bit-rate": "4000000",
             "--adb": str(self.adb)})
        self.assertNotIn("--fps", argv)

    def test_a_relative_server_path_is_forwarded_absolute(self) -> None:
        argv = self.node_argv(self.serve("--transport", "auto",
                                         "--scrcpy-server", self.server.name))
        forwarded = Path(flag_values(argv)["--scrcpy-server"])
        self.assertTrue(forwarded.is_absolute(), forwarded)
        self.assertEqual(forwarded.name, self.server.name)
        self.assertEqual(forwarded.resolve(), self.server.resolve())
        self.assertNotIn("--scrcpy-version", argv)

    def test_a_versioned_link_is_forwarded_under_its_own_name(self) -> None:
        """A link like scrcpy-server-v4.1 -> scrcpy-server keeps the name that says the version."""
        target = self.root / "share" / "scrcpy-server"
        target.parent.mkdir()
        target.write_bytes(b"PK\x03\x04 not a real server")
        link = self.root / "links" / "scrcpy-server-v4.1"
        link.parent.mkdir()
        link.symlink_to(target)
        argv = self.node_argv(self.serve("--transport", "scrcpy", "--scrcpy-server", str(link)))
        forwarded = flag_values(argv)["--scrcpy-server"]
        self.assertEqual(forwarded, str(link.absolute()))
        self.assertTrue(forwarded.endswith("scrcpy-server-v4.1"), forwarded)

    def test_defaults_leave_tuning_to_the_canvas_server(self) -> None:
        argv = self.node_argv(self.serve())
        self.assertEqual(flag_values(argv)["--transport"], "auto")
        for flag in ("--fps", "--max-size", "--bit-rate", "--scrcpy-server",
                     "--scrcpy-version"):
            self.assertNotIn(flag, argv)

    def test_every_transport_is_accepted_and_an_explicit_fps_is_forwarded(self) -> None:
        self.env["AUTONOM_SCRCPY_SERVER"] = str(self.server)
        for transport in ("auto", "scrcpy", "screenrecord", "screencap"):
            with self.subTest(transport=transport):
                values = flag_values(self.node_argv(
                    self.serve("--transport", transport, "--fps", "30")))
                self.assertEqual((values["--transport"], values["--fps"]), (transport, "30"))

    def test_bad_values_fail_before_node_starts(self) -> None:
        cases = [
            ("--max-size", "100"), ("--max-size", "5000"),
            ("--bit-rate", "10"), ("--bit-rate", "200000000"),
            ("--fps", "0"), ("--fps", "61"),
            ("--scrcpy-server", str(self.server), "--scrcpy-version", "latest"),
            ("--scrcpy-server", str(self.server), "--scrcpy-version", "v4.1"),
            ("--scrcpy-version", "4.1"),
            ("--scrcpy-server", str(self.root / "missing-server")),
            ("--scrcpy-server", str(self.root)),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.refused(self.serve(*argv))["error_code"],
                                 errors.INVALID_VALUE)
        payload = self.refused(self.serve("--scrcpy-server", str(self.root / "missing-server")))
        self.assertIn("brew install scrcpy", payload["hint"])

    def test_bit_rate_default_is_12_mbit_in_hint_and_help(self) -> None:
        """A60-003: the CLI states the canvas server's 12 Mbit/s default."""
        payload = self.refused(self.serve("--bit-rate", "10"))
        self.assertEqual(payload["hint"], "The default is 12000000.")
        completed = subprocess.run(
            [sys.executable, str(CLI), "canvas", "serve", "--help"],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("(default 12000000)", " ".join(completed.stdout.split()))
        self.assertNotIn("8000000", completed.stdout)

    def test_scrcpy_without_any_server_fails_naming_canvas_scrcpy(self) -> None:
        """CANVAS-013-S01: no flag, no variable, no scrcpy on PATH."""
        payload = self.refused(self.serve("--transport", "scrcpy"))
        self.assertEqual((payload["error_code"], payload["capability"], payload["tool"]),
                         (errors.TOOL_MISSING, "canvas.scrcpy", "scrcpy"))
        self.assertIn("brew install scrcpy", payload["hint"])

    def test_a_configured_server_is_left_to_the_canvas_server_to_resolve(self) -> None:
        """The CLI only checks that something could provide a server; which
        one and its version stay the node server's job."""
        fake_scrcpy(self.root / "bin" / "scrcpy", "4.1")
        argv = self.node_argv(self.serve("--transport", "scrcpy"))
        self.assertEqual(flag_values(argv)["--transport"], "scrcpy")
        self.assertNotIn("--scrcpy-server", argv)
        (self.root / "bin" / "scrcpy").unlink()
        self.node_out.unlink()
        self.env["AUTONOM_SCRCPY_SERVER"] = str(self.root / "elsewhere" / "scrcpy-server-v3.3")
        argv = self.node_argv(self.serve("--transport", "scrcpy"))
        self.assertNotIn("--scrcpy-server", argv)

    def test_a_pre_release_version_reaches_the_canvas_server_to_be_refused_there(self) -> None:
        argv = self.node_argv(self.serve("--scrcpy-server", str(self.server),
                                         "--scrcpy-version", "4.1-rc1"))
        self.assertEqual(flag_values(argv)["--scrcpy-version"], "4.1-rc1")

    def test_an_unknown_transport_is_a_usage_error(self) -> None:
        self.assertEqual(self.refused(self.serve("--transport", "webrtc"))["error_code"],
                         errors.USAGE_ERROR)

    def test_scrcpy_on_ios_is_refused_with_its_capability(self) -> None:
        for argv in (("--transport", "scrcpy"), ("--scrcpy-server", str(self.server))):
            with self.subTest(argv=argv):
                payload = self.refused(self.serve(*argv, ios=True))
                self.assertEqual((payload["error_code"], payload["capability"]),
                                 (errors.UNSUPPORTED_ON_PLATFORM, "canvas.scrcpy"))

    def test_idb_on_ios_forwards_an_explicit_companion_as_an_absolute_path(self) -> None:
        """IOSF-001: `--transport idb` and `--idb-companion` reach the canvas server."""
        companion = write_script(self.root / "tools" / "idb_companion", "#!/bin/sh\nexit 0\n")
        argv = self.node_argv(self.serve("--transport", "idb", "--idb-companion",
                                         "tools/idb_companion", ios=True))
        values = flag_values(argv)
        self.assertEqual((values["--platform"], values["--transport"]), ("ios", "idb"))
        self.assertEqual(Path(values["--idb-companion"]).resolve(), companion.resolve())
        self.assertTrue(Path(values["--idb-companion"]).is_absolute())

    def test_idb_on_ios_is_left_to_the_canvas_server_when_a_companion_can_be_found(self) -> None:
        companion = write_script(self.root / "bin" / "idb_companion", "#!/bin/sh\nexit 0\n")
        argv = self.node_argv(self.serve("--transport", "idb", ios=True))
        self.assertNotIn("--idb-companion", argv)
        companion.unlink()
        self.node_out.unlink()
        elsewhere = write_script(self.root / "elsewhere" / "idb_companion", "#!/bin/sh\nexit 0\n")
        self.env["AUTONOM_IDB_COMPANION_BIN"] = str(elsewhere)
        argv = self.node_argv(self.serve("--transport", "idb", ios=True))
        self.assertEqual(flag_values(argv)["--transport"], "idb")
        self.assertNotIn("--idb-companion", argv)

    def test_idb_without_any_companion_fails_naming_canvas_idb(self) -> None:
        """IOSF-005: no flag, no AUTONOM_IDB_COMPANION_BIN, nothing on PATH.
        AUTONOM_IDB_COMPANION is the remote companion of idb calls: neither a
        `host:port` nor a file path there is the Canvas's binary."""
        binary = write_script(self.root / "elsewhere" / "idb_companion", "#!/bin/sh\nexit 0\n")
        for value in (None, "mac-farm-01:10882", str(binary)):
            with self.subTest(value=value):
                if value:
                    self.env["AUTONOM_IDB_COMPANION"] = value
                payload = self.refused(self.serve("--transport", "idb", ios=True))
                self.assertEqual((payload["error_code"], payload["capability"], payload["tool"]),
                                 (errors.TOOL_MISSING, "canvas.idb", "idb_companion"))
                self.assertEqual(payload["error"],
                                 "--transport idb is unavailable: idb_companion was not found "
                                 "(--idb-companion, AUTONOM_IDB_COMPANION_BIN or PATH)")
                self.assertIn("brew install facebook/fb/idb-companion", payload["hint"])
                self.assertIn("AUTONOM_IDB_COMPANION_BIN", payload["hint"])

    def test_idb_lookup_is_the_flag_then_the_bin_variable_then_path(self) -> None:
        """A set AUTONOM_IDB_COMPANION_BIN that names no executable file is
        reported, never skipped for PATH; the flag comes before it."""
        on_path = write_script(self.root / "bin" / "idb_companion", "#!/bin/sh\nexit 0\n")
        plain = self.root / "plain" / "idb_companion"
        plain.parent.mkdir()
        plain.write_text("not executable\n", encoding="utf-8")
        for bad in (self.root / "missing", plain, self.root):
            with self.subTest(bad=bad):
                self.env["AUTONOM_IDB_COMPANION_BIN"] = str(bad)
                payload = self.refused(self.serve("--transport", "idb", ios=True))
                self.assertEqual((payload["error_code"], payload["capability"], payload["tool"]),
                                 (errors.TOOL_MISSING, "canvas.idb", "idb_companion"))
                self.assertEqual(payload["error"], "--transport idb is unavailable: "
                                 f"AUTONOM_IDB_COMPANION_BIN is not an executable file: {bad}")
        argv = self.node_argv(self.serve("--transport", "idb", "--idb-companion", str(on_path),
                                         ios=True))
        self.assertEqual(flag_values(argv)["--idb-companion"], str(on_path))
        self.node_out.unlink()
        # A flag that names no executable file: tool_missing for --transport idb, as the
        # canvas server refuses it, and invalid_value otherwise.
        self.env.pop("AUTONOM_IDB_COMPANION_BIN")
        payload = self.refused(self.serve("--transport", "idb", "--idb-companion", str(plain),
                                          ios=True))
        self.assertEqual((payload["error_code"], payload["error"]), (
            errors.TOOL_MISSING,
            f"--transport idb is unavailable: --idb-companion is not an executable file: {plain}"))
        payload = self.refused(self.serve("--idb-companion", str(plain), ios=True))
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)

    def test_a_remote_companion_variable_is_left_untouched_for_idb_calls(self) -> None:
        """AUTONOM_IDB_COMPANION keeps its meaning: node gets it unchanged, and
        the Canvas binary comes from AUTONOM_IDB_COMPANION_BIN."""
        binary = write_script(self.root / "elsewhere" / "idb_companion", "#!/bin/sh\nexit 0\n")
        node_env = self.root / "node-env.json"
        self.env.update({"AUTONOM_IDB_COMPANION": "mac-farm-01:10882",
                         "AUTONOM_IDB_COMPANION_BIN": str(binary),
                         "FAKE_NODE_ENV": str(node_env)})
        argv = self.node_argv(self.serve("--transport", "idb", ios=True))
        self.assertNotIn("--idb-companion", argv)
        self.assertEqual(json.loads(node_env.read_text(encoding="utf-8")),
                         {"AUTONOM_IDB_COMPANION": "mac-farm-01:10882",
                          "AUTONOM_IDB_COMPANION_BIN": str(binary)})

    def test_auto_on_ios_starts_without_a_companion(self) -> None:
        """auto falls back to screencap inside the canvas server, never refuses here."""
        values = flag_values(self.node_argv(self.serve(ios=True)))
        self.assertEqual(values["--transport"], "auto")

    def test_a_missing_companion_file_fails_before_node_starts(self) -> None:
        for path in (self.root / "missing", self.root):
            with self.subTest(path=path):
                payload = self.refused(self.serve("--idb-companion", str(path), ios=True))
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE)

    def test_idb_on_android_is_refused_with_its_capability(self) -> None:
        companion = write_script(self.root / "tools" / "idb_companion", "#!/bin/sh\nexit 0\n")
        for argv in (("--transport", "idb"), ("--idb-companion", str(companion))):
            with self.subTest(argv=argv):
                payload = self.refused(self.serve(*argv))
                self.assertEqual((payload["error_code"], payload["capability"]),
                                 (errors.UNSUPPORTED_ON_PLATFORM, "canvas.idb"))

    def test_ios_still_forwards_its_own_transport(self) -> None:
        values = flag_values(self.node_argv(self.serve("--transport", "screencap", ios=True)))
        self.assertEqual((values["--platform"], values["--transport"], values["--simctl"]),
                         ("ios", "screencap", str(self.simctl)))


# --- doctor --------------------------------------------------------------------------


IDB_MODULE = (ROOT / "plugins/autonom/skills/android-emulator-browser/scripts/"
              "ios-idb-companion.mjs")
NODE_LOOKUP = """
const [moduleUrl, flag, env] = JSON.parse(process.argv[1]);
const { resolveIdbCompanionBinary } = await import(moduleUrl);
console.log(JSON.stringify(await resolveIdbCompanionBinary({ flag: flag ?? undefined, env })));
"""


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_canvas_idb", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CanvasIdbLookupAgreementTests(unittest.TestCase):
    """The pre-check of `canvas serve` and the canvas server find idb_companion
    by the same rules: --idb-companion, AUTONOM_IDB_COMPANION_BIN, then PATH,
    never AUTONOM_IDB_COMPANION, with the same answer and reason for each case."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = load_cli()
        cls.node = shutil.which("node")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def node_lookup(self, flag: str | None, env: dict[str, str]) -> dict:
        completed = subprocess.run(
            [self.node, "--input-type=module", "-e", NODE_LOOKUP,
             json.dumps([IDB_MODULE.as_uri(), flag, env])],
            text=True, capture_output=True, check=False, timeout=30,
            env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def python_lookup(self, flag: str | None, env: dict[str, str]) -> dict:
        with mock.patch.dict(os.environ, env, clear=True):
            return self.cli._canvas_idb_lookup(argparse.Namespace(idb_companion=flag))

    def test_both_layers_give_the_same_answer_for_every_case(self) -> None:
        first = write_script(self.root / "first" / "idb_companion", "#!/bin/sh\nexit 0\n")
        second = write_script(self.root / "second" / "idb_companion", "#!/bin/sh\nexit 0\n")
        plain = self.root / "plain" / "idb_companion"
        plain.parent.mkdir()
        plain.write_text("not executable\n", encoding="utf-8")
        folder = self.root / "folder"
        (folder / "idb_companion").mkdir(parents=True)
        empty = self.root / "empty"
        empty.mkdir()
        path_both = os.pathsep.join(["", str(folder), str(plain.parent), str(first.parent),
                                     str(second.parent)])
        remote = {"AUTONOM_IDB_COMPANION": "mac-farm-01:10882"}
        cases = {
            "flag before the variable and PATH": (
                str(second), {"AUTONOM_IDB_COMPANION_BIN": str(first), "PATH": path_both},
                ("--idb-companion", str(second))),
            "variable before PATH": (
                None, {"AUTONOM_IDB_COMPANION_BIN": str(second), "PATH": str(first.parent)},
                ("AUTONOM_IDB_COMPANION_BIN", str(second))),
            "PATH skips empty entries, folders and plain files": (
                None, {"PATH": path_both, **remote}, ("PATH", str(first))),
            "an empty variable is unset": (
                None, {"AUTONOM_IDB_COMPANION_BIN": "", "PATH": str(second.parent)},
                ("PATH", str(second))),
            "a bad flag is reported": (str(plain), {"PATH": path_both}, None),
            "a folder flag is reported": (str(folder), {"PATH": path_both}, None),
            "a bad variable is reported, not skipped": (
                None, {"AUTONOM_IDB_COMPANION_BIN": str(self.root / "missing"),
                       "PATH": path_both}, None),
            "the remote companion variable names no binary": (
                None, {"AUTONOM_IDB_COMPANION": str(first), "PATH": str(empty)}, None),
            "nothing anywhere": (None, {"PATH": str(empty), **remote}, None),
        }
        for name, (flag, env, expected) in cases.items():
            with self.subTest(name):
                python, node = self.python_lookup(flag, env), self.node_lookup(flag, env)
                self.assertEqual(python, node)
                if expected:
                    self.assertTrue(python["available"])
                    self.assertEqual((python["source"], python["path"]), expected)
                else:
                    self.assertFalse(python["available"])
                    self.assertTrue(python["reason"])
                    self.assertNotIn("AUTONOM_IDB_COMPANION ", python["reason"] + " ")


class DoctorScrcpyTests(EnvSandboxMixin, unittest.TestCase):
    """CANVAS-013: doctor reports scrcpy as optional, capability canvas.scrcpy."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.links = self.root / "links"
        self.links.mkdir()
        self.set_env(PATH=str(self.links), AUTONOM_SCRCPY_SERVER=None, SCRCPY_SERVER_PATH=None)

    def install_scrcpy(self, version: str, *, with_server: bool = True) -> Path:
        """The Homebrew layout: a PATH symlink into <prefix>/bin, the server in
        <prefix>/share/scrcpy."""
        prefix = self.root / "Cellar" / "scrcpy" / version
        binary = fake_scrcpy(prefix / "bin" / "scrcpy", version)
        if with_server:
            server = prefix / "share" / "scrcpy" / "scrcpy-server"
            server.parent.mkdir(parents=True)
            server.write_bytes(b"server")
        (self.links / "scrcpy").symlink_to(binary)
        return Path(os.path.realpath(prefix))

    def server_file(self, name: str) -> Path:
        path = self.root / "servers" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"server")
        return path

    def test_missing_scrcpy_is_optional_with_an_install_hint(self) -> None:
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual(
            {key: entry[key] for key in ("state", "optional", "capability", "ready",
                                         "path", "server_path", "required_version")},
            {"state": "missing", "optional": True, "capability": "canvas.scrcpy",
             "ready": False, "path": None, "server_path": None, "required_version": "4.1"})
        self.assertIn("brew install scrcpy", entry["install_hint"])
        self.assertIn("apt-get install scrcpy", entry["install_hint"])

    def test_installed_scrcpy_4_1_resolves_the_server_beside_it(self) -> None:
        prefix = self.install_scrcpy("4.1")
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual(
            {key: entry[key] for key in ("state", "ready", "source", "version",
                                         "server_path", "path", "install_hint")},
            {"state": "ok", "ready": True, "source": "scrcpy", "version": "4.1",
             "server_path": str(prefix / "share" / "scrcpy" / "scrcpy-server"),
             "path": str(self.links / "scrcpy"), "install_hint": None})

    def test_another_scrcpy_version_is_reported_not_ready(self) -> None:
        self.install_scrcpy("3.3")
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["state"], entry["ready"], entry["version"]),
                         ("error", False, "3.3"))
        self.assertIn("4.1", entry["error"])

    def test_scrcpy_without_its_server_is_an_error(self) -> None:
        self.install_scrcpy("4.1", with_server=False)
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["state"], entry["ready"], entry["source"]),
                         ("error", False, "scrcpy"))
        self.assertIn("no scrcpy-server file", entry["error"])

    def test_environment_servers_come_first_in_the_canvas_order(self) -> None:
        self.install_scrcpy("3.3")
        autonom = self.server_file("scrcpy-server-v4.1")
        upstream = self.server_file("scrcpy-server-v4.1.jar")
        self.set_env(SCRCPY_SERVER_PATH=str(upstream))
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["source"], entry["server_path"], entry["version"], entry["ready"]),
                         ("SCRCPY_SERVER_PATH", str(upstream), "4.1", True))
        self.set_env(AUTONOM_SCRCPY_SERVER=str(autonom))
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["source"], entry["server_path"], entry["ready"]),
                         ("AUTONOM_SCRCPY_SERVER", str(autonom), True))

    def test_an_unversioned_environment_server_is_never_guessed(self) -> None:
        """Its version comes from its name only: an installed scrcpy says
        nothing about a file a variable names."""
        plain = self.server_file("scrcpy-server")
        self.set_env(AUTONOM_SCRCPY_SERVER=str(plain))
        self.install_scrcpy("4.1")
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["state"], entry["version"], entry["ready"], entry["source"]),
                         ("error", None, False, "AUTONOM_SCRCPY_SERVER"))
        self.assertIn("--scrcpy-version 4.1", entry["install_hint"])

    def test_server_file_names_are_read_like_the_canvas_reads_them(self) -> None:
        cases = {"scrcpy-server-v4.1": "4.1", "scrcpy-server-v4.1.jar": "4.1",
                 "scrcpy-server-v3.3.1": "3.3.1", "scrcpy-server-v4.1-rc1": "4.1-rc1",
                 "scrcpy-server-v4.1.bak": None, "scrcpy-server": None,
                 "old-scrcpy-server-v4.1": None, "scrcpy-server-v4": None}
        for name, version in cases.items():
            with self.subTest(name=name):
                self.assertEqual(doctor.scrcpy_server_version(f"/x/{name}"), version)

    def test_a_pre_release_is_not_4_1(self) -> None:
        self.install_scrcpy("4.1-rc1")
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["state"], entry["ready"], entry["version"]),
                         ("error", False, "4.1-rc1"))
        self.set_env(SCRCPY_SERVER_PATH=str(self.server_file("scrcpy-server-v4.1-rc1")))
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["state"], entry["source"], entry["version"]),
                         ("error", "SCRCPY_SERVER_PATH", "4.1-rc1"))

    def test_scrcpy_that_names_no_version_is_an_error(self) -> None:
        prefix = self.install_scrcpy("4.1")
        write_script(prefix / "bin" / "scrcpy", "#!/bin/sh\necho 'usage: scrcpy'\nexit 1\n")
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["state"], entry["ready"], entry["version"]),
                         ("error", False, None))
        self.assertIn("did not name a version", entry["error"])

    def test_an_environment_server_pointing_at_nothing_is_named(self) -> None:
        self.set_env(AUTONOM_SCRCPY_SERVER=str(self.root / "missing" / "scrcpy-server-v4.1"))
        entry = doctor._scrcpy_entry()  # noqa: SLF001
        self.assertEqual((entry["state"], entry["ready"], entry["source"]),
                         ("error", False, "AUTONOM_SCRCPY_SERVER"))

    def test_optional_scrcpy_never_gates_strict(self) -> None:
        report = {"tools": {"adb": {"state": "ok"}},
                  "optional_tools": {"scrcpy": {"state": "missing"}}}
        self.assertTrue(doctor.is_healthy(report))

    def test_the_doctor_report_lists_scrcpy_as_optional(self) -> None:
        home = self.root / "home"
        (self.links / "python3").symlink_to(sys.executable)
        self.install_scrcpy("4.1")
        completed = subprocess.run(
            [sys.executable, str(CLI), "doctor", "--strict"], cwd=self.root,
            env=hermetic_env(home, str(self.links)), text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        entry = report["optional_tools"]["scrcpy"]
        self.assertEqual((entry["state"], entry["optional"], entry["capability"]),
                         ("ok", True, "canvas.scrcpy"))
        self.assertNotIn("scrcpy", report["tools"])
        # Strict fails on the missing device tools, never on the optional one.
        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("scrcpy", report["strict_failures"])


# --- bootstrap.sh --------------------------------------------------------------------


class BootstrapScrcpyTests(unittest.TestCase):
    """bootstrap.sh offers scrcpy as optional and never fails on it."""

    def setUp(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is not installed")
        self.bash = bash
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.trace = self.root / "installers.log"
        for name in ("adb", "mitmdump", "idb", "xcrun"):
            write_script(self.bin / name, "#!/bin/sh\nexit 0\n")
        # A package manager reached here only logs; nothing is installed.
        for name in ("brew", "sudo", "apt-get"):
            sentinel(self.bin / name, self.trace, code=0)

    def bootstrap(self, *args: str, system: str = "Linux") -> subprocess.CompletedProcess:
        write_script(self.bin / "uname", f"#!/bin/sh\necho {system}\n")
        return subprocess.run(
            [self.bash, str(BOOTSTRAP), *args],
            env={"PATH": str(self.bin), "HOME": str(self.root)}, cwd=self.root, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=60)

    def installs(self) -> list[str]:
        if not self.trace.exists():
            return []
        return self.trace.read_text(encoding="utf-8").splitlines()

    def test_missing_scrcpy_is_offered_and_never_fails_the_check(self) -> None:
        for system, command in (("Linux", "sudo apt-get install -y scrcpy"),
                                ("Darwin", "brew install scrcpy")):
            with self.subTest(system=system):
                completed = self.bootstrap(system=system)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertIn("Optional tools:", completed.stdout)
                self.assertIn("[OPT]  scrcpy 4.1", completed.stdout)
                self.assertIn(command, completed.stdout)
                self.assertIn("Bootstrap check complete.", completed.stdout)
        self.assertEqual(self.installs(), [])

    def test_scrcpy_4_1_reads_ok(self) -> None:
        fake_scrcpy(self.bin / "scrcpy", "4.1")
        completed = self.bootstrap()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[ok]   scrcpy 4.1", completed.stdout)

    def test_another_scrcpy_version_is_named(self) -> None:
        fake_scrcpy(self.bin / "scrcpy", "2.4")
        completed = self.bootstrap()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("scrcpy 2.4 found; the Mobile Canvas scrcpy transport needs 4.1",
                      completed.stdout)

    def test_with_scrcpy_alone_installs_nothing(self) -> None:
        completed = self.bootstrap("--with-scrcpy", system="Darwin")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.installs(), [])

    def test_install_alone_leaves_the_optional_tool_alone(self) -> None:
        completed = self.bootstrap("--install", system="Darwin")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("'--install --with-scrcpy' installs it", completed.stdout)
        self.assertEqual(self.installs(), [])

    def test_install_with_scrcpy_uses_the_package_manager(self) -> None:
        for system, expected in (("Darwin", ["brew install scrcpy"]),
                                 ("Linux", ["sudo apt-get install -y scrcpy"])):
            with self.subTest(system=system):
                self.trace.unlink(missing_ok=True)
                completed = self.bootstrap("--install", "--with-scrcpy", system=system)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(self.installs(), expected)

    def test_an_unknown_argument_is_refused(self) -> None:
        completed = self.bootstrap("--bogus")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
