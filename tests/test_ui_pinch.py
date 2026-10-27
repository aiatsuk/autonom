"""The pinch CLI validates its contract before dispatching a two-finger gesture."""
from __future__ import annotations

import importlib.util
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ui  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402


def cli_module():
    spec = importlib.util.spec_from_file_location("pinch_cli", ROOT / "scripts/autonom.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PinchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = cli_module()

    def test_rejects_invalid_scale_and_duration_before_target_selection(self):
        for flags in (("--scale", "0"), ("--scale=-1",), ("--scale=nan",),
                      ("--scale=inf",), ("--scale=11",), ("--duration", "0"),
                      ("--duration=nan",), ("--duration=2.1",)):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as home:
                result = subprocess.run([sys.executable, str(ROOT / "scripts/autonom.py"),
                                         "ui", "pinch", "--at", "200,400", *flags],
                                        capture_output=True, text=True,
                                        env={**os.environ, "AUTONOM_HOME": home}, timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stderr)["error_code"], "invalid_value")
                self.assertNotIn("Traceback", result.stderr)

    def test_requires_anchor_without_resolving_target(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/autonom.py"),
                                     "ui", "pinch"], capture_output=True, text=True,
                                    env={**os.environ, "AUTONOM_HOME": home}, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stderr)["error_code"], "invalid_coordinates")

    def test_geometry_keeps_both_fingers_on_screen_for_zoom_in_and_out(self):
        from autonom_lib import pinch
        for scale in (0.1, 0.5, 1, 2, 10):
            radius = pinch.radius_for(200, 400, 402, 874, scale)
            self.assertGreater(radius, 0)
            self.assertLessEqual(radius * max(scale, 1), 200)
        # On a 320px / 160dpi emulator, a fixed 80px radius yields a 160dp
        # span, below Android's usual minimum and pinch-in never starts.
        self.assertGreaterEqual(pinch.radius_for(160, 320, 320, 640, 0.5), 134)

    def test_pinch_rejects_edge_or_unknown_screen_without_sending(self):
        target = Target("android", "emulator-5554", "adb")
        for size, point in ((None, (200, 400)), ((400, 800), (0, 200)),
                            ((400, 800), (400, 200)), ((400, 800), (200, 800))):
            with self.subTest(size=size, point=point), \
                 mock.patch.object(ui, "screen_size", return_value=size), \
                 mock.patch("autonom_lib.pinch.dispatch") as send:
                with self.assertRaises(errors.AutonomError):
                    ui.pinch(target, *point, scale=2, duration=0.4)
                send.assert_not_called()

    def test_shared_input_checks_then_dispatches_and_returns_metadata(self):
        for platform in ("android", "ios"):
            target = Target(platform, "target", "tool")
            with self.subTest(platform=platform), \
                 mock.patch.object(ui, "screen_size", return_value=(400, 800)), \
                 mock.patch.object(ui, "_pretest_guard") as guard, \
                 mock.patch.object(ui, "_sending") as sending, \
                 mock.patch("autonom_lib.pinch.dispatch", return_value="test-backend") as send:
                result = ui.pinch(target, 200, 400, scale=2.0, duration=0.4)
                self.assertEqual(result["backend"], "test-backend")
                self.assertEqual(result["radius"], 99)
                guard.assert_called_once_with("gesture", target)
                sending.assert_called_once_with(target)
                send.assert_called_once_with(target, x=200, y=400, scale=2.0,
                                             duration=0.4, radius=99, width=400, height=800)

    def test_bridge_failures_preserve_action_uncertainty(self):
        from autonom_lib import pinch
        target = Target("android", "emulator-5554", "adb")
        result = subprocess.CompletedProcess([], 1, "", json.dumps({
            "ok": False, "error_code": "ui_action_uncertain", "error": "connection lost",
            "hint": "Read the screen before retrying.", "sent": True}))
        with mock.patch("autonom_lib.pinch.shutil.which", return_value="node"), \
             mock.patch("autonom_lib.pinch._run_bridge", return_value=result):
            with self.assertRaises(errors.AutonomError) as caught:
                pinch.dispatch(target, x=200, y=400, scale=2, duration=.4,
                               radius=80, width=400, height=800)
            self.assertEqual(caught.exception.code, "ui_action_uncertain")
            self.assertTrue(caught.exception.extra["sent"])

    def test_cli_success_preserves_gesture_parameters_and_backend(self):
        target = Target("android", "emulator-5554", "adb")
        args = argparse.Namespace(gesture="pinch", at="200,400", scale=.5, duration=.6,
                                  observe=False, observe_timeout_ms=None)
        with mock.patch.object(self.cli, "_target", return_value=target), \
             mock.patch.object(self.cli.ui_mod, "pinch", return_value={"radius": 80, "backend": "scrcpy"}), \
             mock.patch.object(self.cli._ObservePlan, "resolve", return_value=None), \
             mock.patch.object(self.cli._ObservePlan, "into"), \
             mock.patch.object(self.cli, "emit", return_value=0) as emit:
            self.assertEqual(self.cli.cmd_ui_gesture(args), 0)
            payload = emit.call_args.args[0]
            self.assertEqual({key: payload[key] for key in ("gesture", "at", "scale", "duration", "radius", "backend")},
                             {"gesture": "pinch", "at": [200, 400], "scale": .5,
                              "duration": .6, "radius": 80, "backend": "scrcpy"})

    def test_bridge_missing_node_and_malformed_results_are_structured(self):
        from autonom_lib import pinch
        target = Target("android", "emulator-5554", "adb")
        with mock.patch("autonom_lib.pinch.shutil.which", return_value=None):
            with self.assertRaises(errors.AutonomError) as caught:
                pinch.dispatch(target, x=200, y=400, scale=2, duration=.4,
                               radius=80, width=400, height=800)
            self.assertEqual(caught.exception.code, "tool_missing")
        for result in (subprocess.CompletedProcess([], 1, "", "broken"),
                       subprocess.CompletedProcess([], 1, "", '{"ok":false}'),
                       subprocess.CompletedProcess([], 0, "{}", "")):
            with self.subTest(result=result), mock.patch("autonom_lib.pinch.shutil.which", return_value="node"), \
                 mock.patch("autonom_lib.pinch._run_bridge", return_value=result):
                with self.assertRaises(errors.AutonomError) as caught:
                    pinch.dispatch(target, x=200, y=400, scale=2, duration=.4,
                                   radius=80, width=400, height=800)
                self.assertEqual(caught.exception.code, "ui_action_uncertain")

    def test_bridge_timeout_requests_graceful_cleanup(self):
        from autonom_lib import pinch
        program = ("import signal,sys,time,json\n"
                   "def stop(*_):\n"
                   " print(json.dumps({'cleanup_confirmed':True}),file=sys.stderr,flush=True); sys.exit(2)\n"
                   "signal.signal(signal.SIGTERM,stop)\n"
                   "sys.stdin.read()\n"
                   "time.sleep(30)\n")
        with self.assertRaises(errors.AutonomError) as caught:
            pinch._run_bridge([sys.executable, "-c", program], "{}", timeout=1, cleanup_timeout=5)
        self.assertEqual(caught.exception.code, "ui_action_uncertain")
        self.assertTrue(caught.exception.extra["cleanup_confirmed"])


if __name__ == "__main__":
    unittest.main()
