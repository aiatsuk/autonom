"""TEST-007: agent screenshots (AS-03, RISK-013).

`screenshot --agent [--max-edge N]` and a plain `screenshot` in an
agent-profile session ask the on-device helper for a small JPEG with the
factor that turns image pixels into tap coordinates; without the helper they
are today's PNG with `scale` 1 and `fallback_reason`; every other screenshot
is exactly today's PNG. The CLI runs in this process against
`tests/fakes/fake_adb.py` and `tests/fakes/fake_android_helper.py` (whose
`screenshot` scales like Shot.java and writes a JPEG frame header); no
device, no javac or d8.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FIXTURES = ROOT / "tests/fixtures/android_helper"
HOME_XML = (FIXTURES / "settings_home.xml").read_text(encoding="utf-8")
HOME_DUMP = FIXTURES / "settings_home_uiautomator.txt"
SERIAL = "emulator-5612"
IOS_UDID = "AAAAAAAA-1111-2222-3333-CCCCCCCCCCCC"
BUILD = "0123456789abcdef"
SCREENCAP = ["exec-out", "screencap", "-p"]
AGENT_KEYS = ("format", "screen", "scale", "coordinates", "fallback_reason", "fallback_hint")
# What a PNG shot's index row has always carried (the PNG pins of
# tests/test_pretest_store.py rely on it): no key is added.
PNG_ROW_KEYS = {"captured_at", "label", "task", "session_id", "platform", "target_id",
                "app_id", "foreground", "mocks_active", "file", "width", "height"}

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/fakes"))
from autonom_lib import android_helper as helper_mod  # noqa: E402
from autonom_lib import android_helper_build as build_mod  # noqa: E402
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import screenshot as shot_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui, ui_android  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402
import fake_android_helper  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


class ShotCase(EnvSandboxMixin, unittest.TestCase):
    """A private state directory, the fake adb and a fake helper serving a
    1080x2400 screen; the backend left to auto."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "adb-state.json"
        self.log = self.root / "adb-log.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        (self.root / "user").mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.set_env(AUTONOM_HOME=str(self.root / "state"), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), PATH=str(bin_dir),
                     HOME=str(self.root / "user"), JAVA_HOME=None, ANDROID_HOME=None,
                     ANDROID_SDK_ROOT=None, AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_UI_BACKEND=None, AUTONOM_ANDROID_UI_BACKEND=None,
                     AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None)
        self.helper = fake_android_helper.FakeHelper(state_path=self.state, xml=HOME_XML).start()
        self.addCleanup(self.helper.close)
        self.state.write_text(json.dumps({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "helper_port": self.helper.port, "ui_dump": str(HOME_DUMP),
            "wm_size": "Physical size: 1080x2400", "device_files": ["autonom-helper.log"]}),
            encoding="utf-8")
        dex = self.root / "classes.dex"
        dex.write_bytes(b"dex\n035\0fake")
        patch = mock.patch.object(helper_mod.build_mod, "ensure",
                                  return_value=build_mod.Build(dex, BUILD, None, None))
        self.built = patch.start()
        self.addCleanup(patch.stop)
        ui_android.set_preference(None)
        self.addCleanup(ui_android.set_preference, None)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        self.cli_module: Any = None

    def no_tools(self) -> None:
        self.built.return_value = build_mod.Build(
            None, None, "javac not found", "Install a JDK (17 or newer) and put javac on PATH.")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"][2:] if row["argv"][:1] == ["-s"] else row["argv"] for row in rows]

    def helper_setup_calls(self) -> list[list[str]]:
        return [call for call in self.calls()
                if call[:1] in (["push"], ["forward"]) or "app_process" in " ".join(call)]

    def ops(self, op: str | None = None) -> list[dict[str, Any]]:
        return [item for item in self.helper.ops(op) if item["op"] != "hello"]

    def cli_raw(self, *argv: str, stdin: str = "") -> tuple[int, dict[str, Any]]:
        if self.cli_module is None:
            spec = importlib.util.spec_from_file_location("autonom_cli_agent_shot", CLI)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            self.addCleanup(control_mod.end_invocation)
            cwd = os.getcwd()
            os.chdir(self.work)
            self.addCleanup(os.chdir, cwd)
            self.cli_module = module
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = self.cli_module.main(list(argv))
        stream = out.getvalue() if out.getvalue().strip() else err.getvalue()
        self.assertNotIn("Traceback", stream)
        return code, json.loads(stream.strip().splitlines()[-1]) if stream.strip() else {}

    def cli(self, *argv: str, stdin: str = "") -> tuple[int, dict[str, Any]]:
        return self.cli_raw("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv, stdin=stdin)

    def start_session(self, *extra: str) -> dict[str, Any]:
        code, payload = self.cli("session", "start", "--app-id", "com.example.app", *extra)
        self.assertEqual(code, 0, payload)
        return payload["session"]

    def written(self) -> list[Path]:
        return sorted(path for path in self.work.iterdir() if path.suffix in (".png", ".jpg"))

    def assert_agent_png(self, payload: dict[str, Any]) -> None:
        """Today's full-size PNG, answered as an agent screenshot without the
        helper: scale 1 and why."""
        self.assertTrue(payload["path"].endswith(".png"), payload["path"])
        self.assertTrue(Path(payload["path"]).read_bytes().startswith(shot_mod.PNG_SIGNATURE))
        self.assertEqual((payload["format"], payload["scale"]), ("png", 1))
        self.assertEqual(payload["screen"], [payload["width"], payload["height"]])
        self.assertEqual(payload["coordinates"],
                         "image pixel (x, y) → ui tap --x round(x*1) --y round(y*1)")
        self.assertTrue(payload["fallback_reason"])
        self.assertTrue(payload["metadata_embedded"], "today's PNG keeps its metadata")


class AgentJpegTests(ShotCase):
    def test_a_small_jpeg_with_its_scale_and_note(self) -> None:  # AS-03-S01
        code, payload = self.cli("screenshot", "--agent")
        self.assertEqual(code, 0, payload)
        path = Path(payload["path"])
        self.assertEqual(path.suffix, ".jpg")
        self.assertTrue(path.read_bytes().startswith(b"\xff\xd8"))
        self.assertEqual(shot_mod.jpeg_size(path), (450, 1000))
        self.assertEqual((payload["format"], payload["width"], payload["height"]),
                         ("jpeg", 450, 1000))
        self.assertEqual(payload["screen"], [1080, 2400])
        self.assertEqual(payload["scale"], 2.4)
        self.assertEqual(payload["coordinates"],
                         "image pixel (x, y) → ui tap --x round(x*2.4) --y round(y*2.4)")
        self.assertIs(payload["metadata_embedded"], False, "a JPEG carries no iTXt")
        self.assertNotIn("fallback_reason", payload)
        asked = self.ops("screenshot")
        self.assertEqual(len(asked), 1)
        self.assertEqual((asked[0]["max_edge"], asked[0]["quality"]), (1000, 80))
        self.assertNotIn(SCREENCAP, self.calls(), "made on the device, not by screencap")

    def test_a_tap_at_the_converted_point_lands_on_the_row(self) -> None:  # AS-03-S01
        code, shot = self.cli("screenshot", "--agent")
        self.assertEqual(code, 0, shot)
        scale = shot["scale"]
        row = next(node for node in ui.nodes_from_payload("android", HOME_XML)
                   if node.get("text") == "Network & internet")
        left, top, right, bottom = row["bounds"]
        # where the row is in the image, as an agent reading the JPEG sees it
        image_x, image_y = (left + right) / 2 / scale, (top + bottom) / 2 / scale
        x, y = round(image_x * scale), round(image_y * scale)
        code, payload = self.cli("ui", "tap", "--x", str(x), "--y", str(y))
        self.assertEqual(code, 0, payload)
        tap = self.ops("tap")[-1]
        self.assertTrue(left <= tap["x"] <= right and top <= tap["y"] <= bottom, tap)

    def test_max_edge_sets_the_long_edge(self) -> None:
        code, payload = self.cli("screenshot", "--agent", "--max-edge", "1568")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["width"], payload["height"]), (706, 1568))
        self.assertEqual(payload["scale"], 1.5306)
        self.assertIn("round(x*1.5306)", payload["coordinates"])
        self.assertEqual(self.ops("screenshot")[-1]["max_edge"], 1568)

    def test_a_smaller_screen_is_never_upscaled(self) -> None:
        self.helper.display = {"width": 720, "height": 1280, "rotation": 0}
        code, payload = self.cli("screenshot", "--agent", "--max-edge", "2000")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["width"], payload["height"], payload["screen"]),
                         (720, 1280, [720, 1280]))
        self.assertEqual(payload["scale"], 1)
        self.assertIn("round(x*1)", payload["coordinates"])

    def test_a_landscape_screen_scales_its_long_edge(self) -> None:
        self.helper.display = {"width": 2400, "height": 1080, "rotation": 1}
        code, payload = self.cli("screenshot", "--agent")
        self.assertEqual((payload["width"], payload["height"], payload["scale"]),
                         (1000, 450, 2.4))


class AgentProfileTests(ShotCase):
    def test_a_plain_screenshot_in_an_agent_session_is_the_jpeg(self) -> None:
        session = self.start_session("--profile", "agent")
        code, payload = self.cli("screenshot", "--label", "home")
        self.assertEqual(code, 0, payload)
        path = Path(payload["path"])
        self.assertEqual(path.parent, Path(session["artifacts_dir"]).resolve() / "shots")
        self.assertRegex(path.name, r"^0001_\d{6}_home\.jpg$")
        self.assertEqual((payload["format"], payload["scale"]), ("jpeg", 2.4))
        row = shot_mod.load_index(session)[-1]
        self.assertEqual(row["file"], f"shots/{path.name}")
        self.assertEqual((row["format"], row["screen"], row["scale"], row["width"],
                          row["height"]), ("jpeg", [1080, 2400], 2.4, 450, 1000))
        self.assertEqual(row["label"], "home", "the metadata lives in the index")
        # `shots show` reads its size; it carries no embedded metadata
        code, shown = self.cli_raw("shots", "show", row["file"])
        self.assertEqual(code, 0, shown)
        self.assertEqual((shown["width"], shown["height"], shown["metadata"]), (450, 1000, {}))

    def test_no_agent_keeps_the_png_and_numbering_counts_the_jpeg(self) -> None:
        session = self.start_session("--profile", "agent")
        self.assertEqual(self.cli("screenshot")[0], 0)
        code, payload = self.cli("screenshot", "--no-agent", "--label", "full")
        self.assertEqual(code, 0, payload)
        self.assertRegex(Path(payload["path"]).name, r"^0002_\d{6}_full\.png$")
        for key in AGENT_KEYS:
            self.assertNotIn(key, payload)
        self.assertEqual(set(shot_mod.load_index(session)[-1]), PNG_ROW_KEYS)
        self.assertEqual(len(self.ops("screenshot")), 1, "only the agent shot asked the helper")

    def test_an_agent_session_on_another_device_keeps_the_png(self) -> None:
        self.start_session("--profile", "agent")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["devices"].append(["emulator-5614", "device", "product:sdk_gphone64_arm64"])
        self.state.write_text(json.dumps(state), encoding="utf-8")
        code, payload = self.cli_raw("--adb", str(FAKE_ADB), "--serial", "emulator-5614",
                                     "screenshot", "--out", str(self.work / "other.png"))
        self.assertEqual(code, 0, payload)
        self.assertNotIn("format", payload)
        self.assertEqual(self.ops("screenshot"), [])

    def test_flow_take_screenshot_stays_png_in_an_agent_session(self) -> None:
        session = self.start_session("--profile", "agent")
        code, reply = self.cli("ui", "do", stdin='[{"takeScreenshot": "flowshot"}]')
        self.assertEqual(code, 0, reply)
        shots = list(Path(session["artifacts_dir"]).rglob("*flowshot*"))
        self.assertTrue(shots, "the flow took its screenshot")
        self.assertTrue(all(path.suffix == ".png" for path in shots), shots)
        self.assertEqual(self.ops("screenshot"), [], "the helper was not asked")


class TodaysPngTests(ShotCase):
    def test_without_agent_outside_the_agent_profile_nothing_changes(self) -> None:
        session = self.start_session()
        code, payload = self.cli("screenshot", "--label", "plain")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["path"].endswith("_plain.png"))
        for key in AGENT_KEYS:
            self.assertNotIn(key, payload)
        self.assertTrue(payload["metadata_embedded"])
        self.assertEqual(set(shot_mod.load_index(session)[-1]), PNG_ROW_KEYS)
        self.assertIn(SCREENCAP, self.calls())
        self.assertEqual(self.helper.ops(), [], "no helper contacted")
        self.assertEqual(self.helper_setup_calls(), [])


class FallbackTests(ShotCase):
    def test_without_the_tools_it_is_todays_png_with_scale_1(self) -> None:
        self.no_tools()
        code, payload = self.cli("screenshot", "--agent")
        self.assertEqual(code, 0, payload)
        self.assert_agent_png(payload)
        self.assertEqual(payload["fallback_reason"], "javac not found")
        self.assertTrue(payload["fallback_hint"])
        self.assertEqual(self.helper.ops(), [])
        self.assertEqual(self.helper_setup_calls(), [], "no adb call for the helper")

    def test_the_uiautomator_choice_contacts_no_helper(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        code, payload = self.cli("screenshot", "--agent")
        self.assertEqual(code, 0, payload)
        self.assert_agent_png(payload)
        self.assertIn("uiautomator", payload["fallback_reason"])
        self.assertEqual(self.helper.ops(), [])
        self.assertEqual(self.helper_setup_calls(), [])
        self.assertEqual(self.built.call_count, 0, "no build either")

    def test_an_error_reply_falls_back_in_auto(self) -> None:
        self.helper.replies["screenshot"] = {
            "error": {"code": "screenshot_failed", "message": "The device returned no screenshot"}}
        code, payload = self.cli("screenshot", "--agent")
        self.assertEqual(code, 0, payload)
        self.assert_agent_png(payload)
        self.assertIn("screenshot_failed", payload["fallback_reason"])

    def test_a_reply_without_a_jpeg_falls_back_in_auto(self) -> None:
        for bad in ({"data_base64": "bm90IGEganBlZw=="}, {"format": "png"}, {"width": 0},
                    {"data_base64": "%%%"}):
            with self.subTest(bad=bad):
                self.helper.replies["screenshot"] = bad
                code, payload = self.cli("screenshot", "--agent")
                self.assertEqual(code, 0, payload)
                self.assert_agent_png(payload)
                self.assertIn("bad_reply", payload["fallback_reason"])

    def test_a_busy_helper_falls_back_without_cutting_its_call(self) -> None:
        with mock.patch.object(helper_mod, "request",
                               side_effect=helper_mod.HelperBusy(20.0, 4321)):
            code, payload = self.cli("screenshot", "--agent")
        self.assertEqual(code, 0, payload)
        self.assert_agent_png(payload)
        self.assertTrue(payload["fallback_reason"].startswith("helper_busy"))
        self.assertFalse([call for call in self.calls()
                          if call[:2] == ["shell", "kill"] or call[:2] == ["forward", "--remove"]])

    def test_helper_mode_refuses_instead_of_falling_back(self) -> None:
        self.no_tools()
        code, envelope = self.cli("--ui-backend", "helper", "screenshot", "--agent")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.ANDROID_HELPER_UNAVAILABLE)
        self.assertEqual(self.written(), [])
        self.assertNotIn(SCREENCAP, self.calls())

    def test_helper_mode_fails_on_an_error_reply(self) -> None:
        self.helper.replies["screenshot"] = {
            "error": {"code": "screenshot_failed", "message": "no screenshot"}}
        code, envelope = self.cli("--ui-backend", "helper", "screenshot", "--agent")
        self.assertEqual(code, 2, envelope)
        self.assertEqual((envelope["error_code"], envelope["helper_error"]),
                         (errors.BACKEND_FAILED, "screenshot_failed"))
        self.assertEqual(self.written(), [])

    def test_helper_mode_busy_is_unavailable(self) -> None:
        with mock.patch.object(helper_mod, "request",
                               side_effect=helper_mod.HelperBusy(20.0, 4321)):
            code, envelope = self.cli("--ui-backend", "helper", "screenshot", "--agent")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.ANDROID_HELPER_UNAVAILABLE)
        self.assertIs(envelope["helper_busy"], True)
        self.assertEqual(self.written(), [])


class FlagTests(ShotCase):
    def test_max_edge_out_of_range_is_refused_before_any_device_call(self) -> None:
        for value in ("0", "15", "100001", "-5"):
            with self.subTest(value=value):
                self.log.unlink(missing_ok=True)
                code, envelope = self.cli("screenshot", "--agent", "--max-edge", value)
                self.assertEqual(code, 2, envelope)
                self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
                self.assertEqual(self.calls(), [], "no device call")
        self.assertEqual(self.written(), [])

    def test_max_edge_needs_agent_mode(self) -> None:
        for argv in (("screenshot", "--max-edge", "500"),
                     ("screenshot", "--no-agent", "--max-edge", "500")):
            with self.subTest(argv=argv):
                code, envelope = self.cli(*argv)
                self.assertEqual(code, 2, envelope)
                self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
                self.assertIn("--agent", envelope["hint"])
        self.assertEqual(self.written(), [])
        self.assertNotIn(SCREENCAP, self.calls())
        # in an agent session --max-edge alone is enough
        self.start_session("--profile", "agent")
        code, payload = self.cli("screenshot", "--max-edge", "500")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["width"], payload["height"]), (225, 500))

    def test_the_new_flags_are_matched_only_in_full(self) -> None:
        for argv in (("screenshot", "--ag"), ("screenshot", "--max-e", "500"),
                     ("screenshot", "--no-ag")):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as caught:
                    self.cli(*argv)
                self.assertEqual(caught.exception.code, 2, "a usage error")
        self.assertEqual(self.ops("screenshot"), [])
        self.assertNotIn(SCREENCAP, self.calls())

    def test_ios_refuses_agent_screenshots(self) -> None:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["booted"] = [IOS_UDID]
        self.state.write_text(json.dumps(state), encoding="utf-8")
        for argv in (("screenshot", "--agent"), ("screenshot", "--max-edge", "500")):
            with self.subTest(argv=argv):
                code, envelope = self.cli_raw("--simctl", str(FAKE_SIMCTL), "--platform", "ios",
                                              "--udid", IOS_UDID, *argv)
                self.assertEqual(code, 2, envelope)
                self.assertEqual(envelope["error_code"], errors.UNSUPPORTED_ON_PLATFORM,
                                 envelope)
        self.assertEqual(self.written(), [])


class OutSuffixTests(ShotCase):
    """`--out` and the agent JPEG: an explicit `.png` in an agent-profile
    session is today's PNG; an agent screenshot whose `--out` does not end
    in `.jpg`/`.jpeg` is refused before any capture; one whose helper is not
    available writes today's PNG under the `--out` name with `.png`
    (`out_renamed`). No image is written under another format's suffix."""

    RENAMED_HINT = "the helper was not available, so this is today's PNG"

    def assert_refused(self, envelope: dict[str, Any], code: int) -> None:
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
        self.assertIn("use a .jpg name or drop --agent", envelope["hint"].lower())
        self.assertNotIn(SCREENCAP, self.calls(), "nothing captured")
        self.assertEqual(self.ops("screenshot"), [], "the helper was not asked")

    def test_a_png_out_in_an_agent_session_is_todays_png(self) -> None:
        session = self.start_session("--profile", "agent")
        out = self.work / "shot.png"
        code, payload = self.cli("screenshot", "--out", str(out))
        self.assertEqual(code, 0, payload)
        self.assertTrue(out.read_bytes().startswith(shot_mod.PNG_SIGNATURE), "PNG bytes")
        self.assertTrue(payload["path"].endswith(".png"), payload["path"])
        self.assertIn(str(out), payload["copies"])
        for key in AGENT_KEYS:
            self.assertNotIn(key, payload)
        self.assertEqual(set(shot_mod.load_index(session)[-1]), PNG_ROW_KEYS)
        self.assertEqual(self.ops("screenshot"), [], "the helper was not asked")
        self.assertIn(SCREENCAP, self.calls())

    def test_a_jpg_out_in_an_agent_session_is_the_jpeg(self) -> None:
        self.start_session("--profile", "agent")
        out = self.work / "shot.jpg"
        code, payload = self.cli("screenshot", "--out", str(out))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["format"], "jpeg")
        self.assertTrue(out.read_bytes().startswith(shot_mod.JPEG_SIGNATURE))
        self.assertTrue(payload["path"].endswith(".jpg"), payload["path"])
        self.assertNotIn("warnings", payload, "written where asked: nothing renamed")

    def test_a_jpg_out_without_the_helper_is_written_as_png(self) -> None:
        self.no_tools()
        for name, png in (("shot.jpg", "shot.png"), ("other.JPEG", "other.png")):
            with self.subTest(name=name):
                out, written = self.work / name, self.work / png
                code, payload = self.cli("screenshot", "--agent", "--out", str(out))
                self.assertEqual(code, 0, payload)
                self.assert_agent_png(payload)
                self.assertEqual(payload["fallback_reason"], "javac not found")
                self.assertFalse(out.exists(), "no PNG bytes under a JPEG name")
                self.assertTrue(written.read_bytes().startswith(shot_mod.PNG_SIGNATURE))
                self.assertEqual(Path(payload["path"]).name, png)
                self.assertEqual(payload["copies"], [payload["path"]])
                self.assertEqual(payload["warnings"], [
                    {"code": "out_renamed", "requested": str(out), "written": str(written),
                     "hint": self.RENAMED_HINT}])

    def test_a_jpg_out_in_an_agent_session_without_the_helper_is_a_png_copy(self) -> None:
        self.no_tools()
        self.start_session("--profile", "agent")
        out, written = self.work / "shot.jpg", self.work / "shot.png"
        code, payload = self.cli("screenshot", "--out", str(out))
        self.assertEqual(code, 0, payload)
        self.assert_agent_png(payload)
        self.assertFalse(out.exists(), "no PNG bytes under a JPEG name")
        self.assertTrue(written.read_bytes().startswith(shot_mod.PNG_SIGNATURE))
        self.assertEqual(len(payload["copies"]), 2)
        self.assertTrue(payload["copies"][0].endswith(".png"), payload["copies"])
        self.assertEqual(payload["copies"][1], str(written))
        self.assertEqual(payload["warnings"], [
            {"code": "out_renamed", "requested": str(out), "written": str(written),
             "hint": self.RENAMED_HINT}])

    def test_agent_with_a_png_out_is_refused_before_any_capture(self) -> None:
        out = self.work / "shot.png"
        code, envelope = self.cli("screenshot", "--agent", "--out", str(out))
        self.assert_refused(envelope, code)
        self.assertFalse(out.exists())
        # in an agent session too, and with --max-edge (it asks for the JPEG)
        self.start_session("--profile", "agent")
        for argv in (("screenshot", "--agent", "--out", str(out)),
                     ("screenshot", "--max-edge", "500", "--out", str(out))):
            with self.subTest(argv=argv):
                self.log.unlink(missing_ok=True)
                code, envelope = self.cli(*argv)
                self.assert_refused(envelope, code)
                self.assertFalse(out.exists())

    def test_the_agent_default_with_another_suffix_is_refused(self) -> None:
        self.start_session("--profile", "agent")
        self.log.unlink(missing_ok=True)
        out = self.work / "shot.bin"
        code, envelope = self.cli("screenshot", "--out", str(out))
        self.assert_refused(envelope, code)
        self.assertFalse(out.exists())

    def test_agent_with_a_jpeg_out_writes_the_jpeg_there(self) -> None:
        for name in ("shot.jpeg", "SHOT.JPG"):
            with self.subTest(name=name):
                out = self.work / name
                code, payload = self.cli("screenshot", "--agent", "--out", str(out))
                self.assertEqual(code, 0, payload)
                self.assertEqual(Path(payload["path"]).name, name)
                self.assertTrue(out.read_bytes().startswith(shot_mod.JPEG_SIGNATURE))

    def test_capture_evidence_refuses_a_jpeg_under_another_suffix(self) -> None:
        target = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        with self.assertRaises(errors.AutonomError) as caught:
            shot_mod.capture_evidence(target, None, out=self.work / "x.png", agent=True)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(self.calls(), [], "no device call")
        self.assertFalse((self.work / "x.png").exists())


if __name__ == "__main__":
    unittest.main()
