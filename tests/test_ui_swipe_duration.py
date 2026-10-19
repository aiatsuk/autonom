"""`ui swipe --duration` (B.2): finite seconds in (0, 60], else `invalid_value`.

`--duration=-inf` used to end in a raw OverflowError and a negative or zero
duration reached the device. The check runs before the target is resolved,
so a refused value sends nothing (the fake adb's log stays empty).
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
SERIAL = "emulator-5554"
HINT = "seconds, e.g. 0.4"


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_swipe", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SwipeDurationUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = load_cli()

    def test_values_in_range_pass_through(self) -> None:
        for value in (0.001, 0.3, 0.4, 59.9, 60.0):
            with self.subTest(value=value):
                self.assertEqual(self.cli._swipe_duration(value), value)

    def test_out_of_range_and_non_finite_are_invalid_value(self) -> None:
        for value in (0.0, -0.0, -1.0, -0.4, 60.0001, 400.0, math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(self.cli.errors.AutonomError) as caught:
                    self.cli._swipe_duration(value)
                exc = caught.exception
                self.assertEqual(exc.code, self.cli.errors.INVALID_VALUE)
                self.assertIn(HINT, exc.hint)
                payload = exc.as_dict()
                self.assertEqual(payload["max_seconds"], 60.0)
                # the payload is JSON: an infinity or NaN is reported as text
                json.dumps(payload, allow_nan=False)
                if math.isfinite(value):
                    self.assertEqual(payload["duration"], value)
                else:
                    self.assertEqual(payload["duration"], str(value))


class SwipeDurationCliTests(unittest.TestCase):
    """The real CLI against the fake adb: refusals exit 2 with a JSON error
    and never reach the device; a value in range is swiped."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.log = root / "fake.log"
        state = root / "state.json"
        state.write_text(json.dumps({"devices": [[SERIAL, "device", "product:sdk_gphone64"]]}),
                         encoding="utf-8")
        self.env = {**os.environ, "AUTONOM_HOME": str(root / "home"),
                    "AUTONOM_FAKE_STATE": str(state), "AUTONOM_FAKE_LOG": str(self.log)}
        for key in ("AUTONOM_ADB", "AUTONOM_SESSION_ID"):
            self.env.pop(key, None)
        self.cwd = root

    def swipe(self, *duration: str) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL,
             "ui", "swipe", "--from", "100,800", "--to", "100,200", *duration],
            cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=120,
            stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def device_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_refused_values_send_nothing(self) -> None:
        for flag in ("--duration=-inf", "--duration=inf", "--duration=nan",
                     "--duration=-1", "--duration=0", "--duration=60.5",
                     "--duration=400"):
            with self.subTest(flag=flag):
                completed = self.swipe(flag)
                self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                payload = json.loads(completed.stderr)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error_code"], "invalid_value")
                self.assertIn(HINT, payload["hint"])
        self.assertEqual(self.device_calls(), [])

    def test_in_range_value_is_swiped(self) -> None:
        completed = self.swipe("--duration", "0.4")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["duration"], 0.4)
        swipes = [argv for argv in self.device_calls() if "swipe" in argv]
        self.assertEqual(len(swipes), 1)
        self.assertEqual(swipes[0][-1], "400")  # adb takes milliseconds

    def test_help_names_the_unit(self) -> None:
        completed = subprocess.run([sys.executable, str(CLI), "ui", "swipe", "--help"],
                                   env=self.env, text=True, capture_output=True, timeout=60,
                                   stdin=subprocess.DEVNULL)
        self.assertIn(HINT, " ".join(completed.stdout.split()))


if __name__ == "__main__":
    unittest.main()
