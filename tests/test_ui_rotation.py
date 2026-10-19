"""Android screen geometry follows the display rotation.

- rotation comes from `dumpsys input` SurfaceOrientation, else `dumpsys
  window displays` mCurrentRotation (`ROTATION_90` or `1`), else
  `settings get system user_rotation`; each source is asked only when the
  one before it named none;
- rotation 1 or 3 swaps the natural `wm size`; 0, 2 and an unknown rotation
  keep it;
- the coordinate guard checks taps against the rotated size: (2000, 500) on
  a 1080x2400 phone is in range in landscape and refused in portrait;
- `ui tree` reports the rotated `screen` through the CLI.

adb is an in-process fake (or `tests/fakes/fake_adb.py` for the CLI); no device.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ui, ui_android  # noqa: E402
from autonom_lib.platform import ANDROID, Target  # noqa: E402

SERIAL = "emulator-5554"
TARGET = Target(ANDROID, SERIAL, "/fake/adb", {"serial": SERIAL})
WM = ("shell", "wm", "size")
INPUT = ("shell", "dumpsys", "input")
WINDOW = ("shell", "dumpsys", "window", "displays")
USER = ("shell", "settings", "get", "system", "user_rotation")


class FakeAdb:
    def __init__(self, answers: dict[tuple[str, ...], object]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, adb, args, *, serial=None, timeout=30, check=True, binary=False):
        argv = list(args)
        self.calls.append(argv)
        answer = self.answers.get(tuple(argv), "")
        if isinstance(answer, BaseException):
            raise answer
        code = 0
        if isinstance(answer, tuple):
            answer, code = answer
        return subprocess.CompletedProcess([adb, *argv], code, answer, "")


def device(rotation: int | None, *, source: str = "input",
           size: str = "Physical size: 1080x2400\n") -> FakeAdb:
    answers: dict[tuple[str, ...], object] = {WM: size}
    if rotation is not None:
        if source == "input":
            answers[INPUT] = f"Input Reader State:\n  Viewport:\n    SurfaceOrientation: {rotation}\n"
        elif source == "window":
            answers[WINDOW] = f"Display: mDisplayId=0\n  mCurrentRotation=ROTATION_{rotation * 90}\n"
        else:
            answers[USER] = f"{rotation}\n"
    return FakeAdb(answers)


class ParseRotationTests(unittest.TestCase):
    def test_sources(self) -> None:
        self.assertEqual(ui_android.parse_rotation("  SurfaceOrientation: 3\n"), 3)
        self.assertEqual(ui_android.parse_rotation(window_dump="mCurrentRotation=ROTATION_90"), 1)
        self.assertEqual(ui_android.parse_rotation(window_dump="mCurrentRotation=ROTATION_270"), 3)
        self.assertEqual(ui_android.parse_rotation(window_dump="mCurrentRotation=2 x"), 2)
        self.assertEqual(ui_android.parse_rotation(user_rotation="1\n"), 1)

    def test_first_source_wins(self) -> None:
        self.assertEqual(ui_android.parse_rotation("SurfaceOrientation: 0", "mCurrentRotation=1",
                                                   "1"), 0)

    def test_unknown_values(self) -> None:
        self.assertIsNone(ui_android.parse_rotation())
        self.assertIsNone(ui_android.parse_rotation("SurfaceOrientation: 7"))
        self.assertIsNone(ui_android.parse_rotation(window_dump="mCurrentRotation=ROTATION_45"))
        self.assertIsNone(ui_android.parse_rotation(window_dump="mCurrentRotation=5"))
        self.assertIsNone(ui_android.parse_rotation(user_rotation="null"))

    def test_rotated_size(self) -> None:
        self.assertEqual(ui_android.rotated_size((1080, 2400), 1), (2400, 1080))
        self.assertEqual(ui_android.rotated_size((1080, 2400), 3), (2400, 1080))
        self.assertEqual(ui_android.rotated_size((1080, 2400), 2), (1080, 2400))
        self.assertEqual(ui_android.rotated_size((1080, 2400), 0), (1080, 2400))
        self.assertEqual(ui_android.rotated_size((1080, 2400), None), (1080, 2400))


class ScreenSizeTests(unittest.TestCase):
    def size(self, fake: FakeAdb):
        with mock.patch.object(ui_android.adb_mod, "run_adb", fake):
            return ui.screen_size(TARGET)

    def test_landscape_swaps_and_portrait_does_not(self) -> None:
        for rotation, expected in ((1, (2400, 1080)), (0, (1080, 2400)), (3, (2400, 1080)),
                                   (2, (1080, 2400)), (None, (1080, 2400))):
            with self.subTest(rotation=rotation):
                self.assertEqual(self.size(device(rotation)), expected)

    def test_each_fallback_source(self) -> None:
        for source in ("input", "window", "user"):
            with self.subTest(source=source):
                fake = device(1, source=source)
                self.assertEqual(self.size(fake), (2400, 1080))
                asked = [tuple(c) for c in fake.calls]
                order = [INPUT, WINDOW, USER]
                expected = order[:order.index({"input": INPUT, "window": WINDOW,
                                               "user": USER}[source]) + 1]
                self.assertEqual(asked, [WM, *expected])

    def test_a_failing_source_falls_through(self) -> None:
        fake = device(1, source="user")
        fake.answers[INPUT] = ("SurfaceOrientation: 0", 1)  # exit 1: not trusted
        self.assertEqual(self.size(fake), (2400, 1080))

    def test_a_slow_or_broken_source_keeps_the_natural_size(self) -> None:
        """A rotation source that times out or cannot start names no rotation;
        the next source is asked and, when none answers, the natural size stays."""
        for failure in (subprocess.TimeoutExpired(["adb", "shell", "dumpsys", "input"], 10),
                        PermissionError(13, "Permission denied"), OSError("broken pipe")):
            with self.subTest(failure=type(failure).__name__):
                fake = device(None)
                fake.answers[INPUT] = failure
                self.assertEqual(self.size(fake), (1080, 2400))
                self.assertEqual([tuple(c) for c in fake.calls], [WM, INPUT, WINDOW, USER])
                fake = device(1, source="user")
                fake.answers[INPUT] = failure
                fake.answers[WINDOW] = failure
                self.assertEqual(self.size(fake), (2400, 1080))
        fake = device(None)
        fake.answers[INPUT] = subprocess.TimeoutExpired(["adb"], 10)
        with mock.patch.object(ui_android.adb_mod, "run_adb", fake):
            self.assertEqual(ui_android._shell_text("/fake/adb", SERIAL,  # noqa: SLF001
                                                    ["dumpsys", "input"]), "")

    def test_override_size_is_rotated(self) -> None:
        fake = device(1, size="Physical size: 1080x2400\nOverride size: 720x1600\n")
        self.assertEqual(self.size(fake), (1600, 720))

    def test_no_size_asks_no_rotation(self) -> None:
        fake = device(1, size="")
        self.assertIsNone(self.size(fake))
        self.assertEqual([tuple(c) for c in fake.calls], [WM])


class TapRangeTests(unittest.TestCase):
    def tap(self, fake: FakeAdb, x: int, y: int) -> None:
        with mock.patch.object(ui_android.adb_mod, "run_adb", fake), \
                mock.patch.object(ui, "_pretest_guard"):
            ui.tap(TARGET, x, y)

    def test_landscape_tap_is_in_range(self) -> None:
        fake = device(1)
        self.tap(fake, 2000, 500)
        self.assertIn(["shell", "input", "tap", "2000", "500"], fake.calls)

    def test_the_same_tap_in_portrait_is_refused_without_input(self) -> None:
        fake = device(0)
        with self.assertRaises(errors.AutonomError) as caught:
            self.tap(fake, 2000, 500)
        self.assertEqual(caught.exception.code, errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(caught.exception.extra["screen"], [1080, 2400])
        self.assertFalse([c for c in fake.calls if c[:2] == ["shell", "input"]])

    def test_a_portrait_point_is_refused_in_landscape(self) -> None:
        fake = device(1)
        with self.assertRaises(errors.AutonomError) as caught:
            self.tap(fake, 500, 2000)
        self.assertEqual(caught.exception.extra["screen"], [2400, 1080])


class UiTreeScreenCliTests(unittest.TestCase):
    """`ui tree` through the CLI and the fake adb (user_rotation source)."""

    def run_tree(self, rotation: str | None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                     "ui_dump": str(UI_DUMP), "wm_size": "Physical size: 1080x2400"}
            if rotation is not None:
                state["settings_system"] = {"user_rotation": rotation}
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            (root / "bin").mkdir()
            (root / "bin" / "python3").symlink_to(sys.executable)
            env = {"PATH": str(root / "bin"), "HOME": str(root),
                   "AUTONOM_HOME": str(root / "home"),
                   "AUTONOM_FAKE_STATE": str(root / "state.json"),
                   "AUTONOM_FAKE_LOG": str(root / "log.jsonl")}
            completed = subprocess.run(
                [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL,
                 "ui", "tree"], cwd=root, env=env, text=True, stdin=subprocess.DEVNULL,
                capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_screen_is_rotated(self) -> None:
        self.assertEqual(self.run_tree("1")["screen"], [2400, 1080])
        self.assertEqual(self.run_tree("0")["screen"], [1080, 2400])
        self.assertEqual(self.run_tree(None)["screen"], [1080, 2400])


if __name__ == "__main__":
    unittest.main()
