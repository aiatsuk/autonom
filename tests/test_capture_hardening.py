"""Hardening of the capture-state pins and the simulator controls (CAPST-001..009).

Each case reproduces a defect found in review or on a live device with the
fake tools: a refused value that had already changed the device, a clock left
frozen by an earlier demo pin, a `verified` that matched `level: 10` inside
`level: 100`, a clear that reset state it never set, a keyboard reset that
deleted the device's own locale, and controls that accepted any action word.
What is asserted is the argv the fakes received and the state they hold.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
SERIAL = "emulator-5554"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_prefs, ios_simctl, simulator  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


class HardeningBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.devices_dir = root / "Devices"
        self.home = root / "home"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.home),
            AUTONOM_CORESIMULATOR_DEVICES=str(self.devices_dir),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
        )
        self.env = dict(os.environ)

    # --- fakes ----------------------------------------------------------------

    def set_state(self, **kwargs) -> None:
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def fake_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def argv_log(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["tool"] == tool]

    def adb_calls(self) -> list[str]:
        return [" ".join(argv[2:]) for argv in self.argv_log("adb")]

    def clear_log(self) -> None:
        if self.log.exists():
            self.log.unlink()

    # --- entry points -----------------------------------------------------------

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, env=self.env, timeout=60,
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed.returncode, json.loads(stream)

    def android(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv)

    def ios(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID, *argv)

    def android_target(self) -> Target:
        return Target(ANDROID, SERIAL, str(FAKE_ADB), {"serial": SERIAL})

    def ios_target(self) -> Target:
        return Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    def refused(self, code: int, payload: dict, error_code: str) -> None:
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], error_code, payload)
        self.assertNotIn("could not convert", payload["error"])
        self.assertNotIn("invalid literal", payload["error"])


class ValidateBeforeDeviceTests(HardeningBase):
    """CAPST-001: a refused value must not have changed anything first."""

    def test_bad_clock_enters_no_demo_mode(self) -> None:
        # Before: `hhmm=9x41` sent `sysui_demo_allowed 1` and `enter` first.
        code, payload = self.android("simulator", "status-bar", "pin", "--value", "hhmm=9x41")
        self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.argv_log("adb"), [])

    def test_bad_signal_level_sets_no_battery(self) -> None:
        # Before: `mobile_level=9` had already set the battery level.
        code, payload = self.android("simulator", "status-bar", "pin",
                                     "--value", "mobile_level=9")
        self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.argv_log("adb"), [])

    def test_every_demo_value_is_checked_before_the_first_broadcast(self) -> None:
        for pairs in (["hhmm=0941", "battery=500"], ["hhmm=2561"],
                      ["hhmm=0941", "wifi_level=7"], ["hhmm=0941", "plugged=maybe"],
                      ["mobile=show", "datatype=6g"], ["hhmm=0941", "wifi=perhaps"]):
            with self.subTest(pairs=pairs):
                argv = ["simulator", "status-bar", "pin"]
                for pair in pairs:
                    argv += ["--value", pair]
                code, payload = self.android(*argv)
                self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
                self.assertEqual(self.argv_log("adb"), [])

    def test_a_bad_live_boolean_is_refused_not_read_as_false(self) -> None:
        code, payload = self.android("simulator", "status-bar", "pin",
                                     "--value", "plugged=maybe")
        self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.argv_log("adb"), [])

    def test_ios_values_are_checked_before_simctl(self) -> None:
        for pair in ("batteryLevel=150", "wifiBars=9", "cellularMode=on", "bogus=1"):
            with self.subTest(pair=pair):
                code, payload = self.ios("simulator", "status-bar", "pin", "--value", pair)
                self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
                self.assertEqual([argv for argv in self.argv_log("simctl")
                                  if argv[1:2] == ["status_bar"]], [])


class LivePinTests(HardeningBase):
    def test_live_pin_after_a_demo_pin_leaves_demo_mode_first(self) -> None:
        """CAPST-002: reproduced live — the clock stayed frozen at 9:41."""
        code, payload = self.android("simulator", "status-bar", "pin", "--value", "hhmm=0941")
        self.assertEqual(code, 0, payload)
        self.clear_log()
        code, payload = self.android("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        calls = self.adb_calls()
        exit_at = next(index for index, call in enumerate(calls)
                       if "com.android.systemui.demo" in call and call.endswith("command exit"))
        battery_at = calls.index("shell dumpsys battery set level 100")
        self.assertLess(exit_at, battery_at)
        self.assertFalse(any(call.endswith("command enter") for call in calls))

    def test_battery_is_verified_by_the_exact_level_line(self) -> None:
        """CAPST-003: a device that ignored `set level 10` still reads 100;
        `"level: 10" in "level: 100"` used to call that verified."""
        self.set_state(fail={f"-s {SERIAL} shell dumpsys battery set level": [0, ""]})
        code, payload = self.android("simulator", "status-bar", "pin", "--value", "battery=10")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["observed_battery"], 100)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["verification"], "mismatch")
        code, payload = self.android("simulator", "battery", "set", "--value", "level=10")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["verified"])

    def test_battery_parser_reads_the_level_line_only(self) -> None:
        parsed = simulator.parse_battery(
            "Current Battery Service state:\n  (UPDATES STOPPED -- use 'reset' to restart)\n"
            "  AC powered: true\n  USB powered: false\n  level: 100\n  scale: 100\n")
        self.assertEqual(parsed, {"overridden": True, "level": 100, "ac": True, "usb": False})
        self.assertNotEqual(simulator.parse_battery("  level: 100\n")["level"], 10)

    def test_signal_is_pinned_with_an_explicit_rssi_and_flagged(self) -> None:
        """CAPST-003: `signal-profile` alone drifted from 4 to 2 bars in 65 s."""
        code, payload = self.android("simulator", "status-bar", "pin",
                                     "--value", "mobile_level=2")
        self.assertEqual(code, 0, payload)
        calls = self.adb_calls()
        self.assertLess(calls.index("emu gsm signal-profile 2"), calls.index("emu gsm signal 7 0"))
        state = self.fake_state()
        self.assertEqual((state["signal_profile"], state["signal_rssi"]), ("2", "7"))
        self.assertEqual(payload["verified_keys"], ["battery"],
                         "the bars cannot be read back, so they are not claimed")
        warning = next(item for item in payload["warnings"]
                       if item["code"] == errors.SIGNAL_UNSTABLE)
        self.assertIn("hint", warning)

    def test_every_level_maps_into_its_android_asu_band(self) -> None:
        bands = {0: range(0, 3), 1: range(3, 5), 2: range(5, 8), 3: range(8, 12),
                 4: range(12, 32)}
        for level, rssi in simulator.ANDROID_SIGNAL_RSSI.items():
            self.assertIn(rssi, bands[level])


class ClearRestoresSnapshotTests(HardeningBase):
    """CAPST-004: clear puts back what the first pin replaced."""

    def snapshot(self) -> dict:
        path = self.home / "simulator-state" / f"{SERIAL}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_a_pre_existing_battery_override_survives_pin_and_clear(self) -> None:
        self.set_state(battery_overridden=True, battery_level="42", battery_ac="true")
        self.android("simulator", "status-bar", "pin", "--value", "battery=50")
        self.android("simulator", "status-bar", "pin", "--value", "battery=60")
        self.assertEqual(self.snapshot()["status_bar"]["battery"]["level"], 42,
                         "a second pin must not overwrite the snapshot")
        code, payload = self.android("simulator", "status-bar", "clear")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["snapshot"])
        self.assertTrue(payload["verified"])
        state = self.fake_state()
        self.assertTrue(state["battery_overridden"])
        self.assertEqual((state["battery_level"], state["battery_ac"]), ("42", "true"))
        self.assertEqual(self.snapshot(), {}, "the snapshot is consumed by clear")

    def test_a_live_battery_is_reset_and_the_signal_restored(self) -> None:
        self.android("simulator", "status-bar", "pin", "--value", "mobile_level=1")
        self.clear_log()
        code, payload = self.android("simulator", "status-bar", "clear")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        calls = self.adb_calls()
        self.assertIn("shell dumpsys battery reset", calls)
        self.assertIn("emu gsm signal-profile 4", calls)
        self.assertIn("emu gsm signal 31 0", calls)
        self.assertFalse(self.fake_state()["battery_overridden"])

    def test_clear_after_a_demo_pin_leaves_battery_and_signal_alone(self) -> None:
        self.set_state(battery_overridden=True, battery_level="42")
        self.android("simulator", "status-bar", "pin", "--value", "hhmm=0941")
        self.clear_log()
        code, payload = self.android("simulator", "status-bar", "clear")
        self.assertEqual(code, 0, payload)
        calls = self.adb_calls()
        self.assertFalse(any("dumpsys battery" in call and "reset" in call for call in calls))
        self.assertFalse(any(call.startswith("emu gsm") for call in calls))
        # sysui_demo_allowed was unset before the demo pin; clear unsets it again.
        self.assertIn("shell settings delete global sysui_demo_allowed", calls)
        self.assertEqual(self.fake_state()["battery_level"], "42")



class RepinBeforeCaptureTests(HardeningBase):
    """CAPST-003 on a live emulator: the bars fell to none and the battery icon
    vanished within 80 s of a live pin, so no one-shot pin holds. The pin is
    re-asserted right before each capture instead."""

    def shoot(self, run) -> tuple[int, dict]:
        return run("screenshot", "--out", str(Path(self.tmp.name) / "shot.png"))

    def calls_before_screencap(self) -> list[str]:
        calls = self.adb_calls()
        index = next(i for i, call in enumerate(calls) if call.startswith("exec-out screencap"))
        return calls[:index]

    def snapshot(self) -> dict:
        path = self.home / "simulator-state" / f"{SERIAL}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_a_live_pin_is_re_sent_right_before_the_screencap(self) -> None:
        self.set_state(battery_overridden=True, battery_level="42")
        code, payload = self.android("simulator", "status-bar", "pin",
                                     "--value", "mobile_level=3", "--value", "battery=80")
        self.assertEqual(code, 0, payload)
        warning = next(item for item in payload["warnings"]
                       if item["code"] == errors.SIGNAL_UNSTABLE)
        self.assertIn("automatically", warning["hint"])
        restore = self.snapshot()["status_bar"]["battery"]
        # the modem drifts: what the host last sent is no longer on screen
        state = self.fake_state()
        state.update(signal_profile="0", signal_rssi="1", battery_level="5")
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.clear_log()
        code, payload = self.shoot(self.android)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["repinned"])
        self.assertEqual(payload["repinned_controls"],
                         ["battery", "mobile_level", "notifications"])
        before = self.calls_before_screencap()
        for call in ("shell dumpsys battery set level 80", "shell dumpsys battery unplug",
                     "emu gsm signal-profile 3", "emu gsm signal 10 0",
                     "shell cmd statusbar send-disable-flag notification-icons"):
            self.assertIn(call, before)
        self.assertLess(before.index("emu gsm signal-profile 3"),
                        before.index("emu gsm signal 10 0"))
        state = self.fake_state()
        self.assertEqual((state["signal_profile"], state["signal_rssi"],
                          state["battery_level"]), ("3", "10", "80"))
        self.assertEqual(self.snapshot()["status_bar"]["battery"], restore,
                         "re-asserting must not touch what clear restores")

    def test_nothing_is_re_sent_without_a_pin_or_after_clear(self) -> None:
        code, payload = self.shoot(self.android)
        self.assertEqual(code, 0, payload)
        self.assertNotIn("repinned", payload)
        self.assertEqual(self.calls_before_screencap(), [])
        self.android("simulator", "status-bar", "pin")
        self.android("simulator", "status-bar", "clear")
        self.clear_log()
        code, payload = self.shoot(self.android)
        self.assertEqual(code, 0, payload)
        self.assertNotIn("repinned", payload)
        self.assertEqual(self.calls_before_screencap(), [])

    def test_a_demo_pin_over_a_live_pin_is_not_re_sent(self) -> None:
        self.android("simulator", "status-bar", "pin")
        self.android("simulator", "status-bar", "pin", "--value", "hhmm=0941")
        self.clear_log()
        code, payload = self.shoot(self.android)
        self.assertEqual(code, 0, payload)
        self.assertNotIn("repinned", payload)
        self.assertEqual(self.calls_before_screencap(), [])

    def test_ios_overrides_persist_and_are_never_re_sent(self) -> None:
        self.assertEqual(self.ios("simulator", "status-bar", "pin")[0], 0)
        self.clear_log()
        code, payload = self.shoot(self.ios)
        self.assertEqual(code, 0, payload)
        self.assertNotIn("repinned", payload)
        self.assertFalse(any(argv[1:2] == ["status_bar"] for argv in self.argv_log("simctl")))
        self.assertIsNone(simulator.reassert_pins(self.ios_target()))

    def test_the_settle_wait_follows_a_re_send_only(self) -> None:
        sleeps: list[float] = []
        self.assertIsNone(simulator.repin_before_capture(self.android_target(), sleeps.append))
        self.assertEqual(sleeps, [])
        self.android("simulator", "status-bar", "pin")
        result = simulator.repin_before_capture(self.android_target(), sleeps.append)
        self.assertTrue(result["repinned"])
        self.assertEqual(sleeps, [simulator.REPIN_SETTLE_SECONDS])


class DeviceBoundPinTests(HardeningBase):
    """CAPST-010: the record lives under the adb serial, which names a console
    port, not a device. Killed without a clear, the next emulator on the port
    — maybe the user's own AVD — used to receive the pin before every capture
    and the first device's snapshot on clear. The record is bound to the AVD
    name and boot id it was taken on and dropped unsent on any mismatch."""

    AVD_A, BOOT_A = "Pixel_Autonom_A", "11111111-aaaa-4000-8000-000000000001"
    AVD_B, BOOT_B = "Users_Own_Pixel", "22222222-bbbb-4000-8000-000000000002"
    PIN_PREFIXES = ("shell dumpsys battery set", "shell dumpsys battery unplug",
                    "emu gsm", "shell cmd statusbar send-disable-flag notification-icons")

    def setUp(self) -> None:
        super().setUp()
        simulator._IDENTITY_CACHE.clear()  # noqa: SLF001
        self.addCleanup(simulator._IDENTITY_CACHE.clear)  # noqa: SLF001

    def record_path(self, serial: str = SERIAL) -> Path:
        return self.home / "simulator-state" / f"{serial}.json"

    def record(self) -> dict:
        path = self.record_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def device(self, avd: str | None, boot: str | None, **extra) -> None:
        """Put `avd`/`boot` on SERIAL, keeping whatever else the fake holds."""
        state = self.fake_state()
        state["avd_names"] = {SERIAL: avd} if avd else {}
        state["boot_ids"] = {SERIAL: boot}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def pin_on_a(self, *values: str) -> dict:
        self.set_state(battery_overridden=True, battery_level="42")
        self.device(self.AVD_A, self.BOOT_A)
        argv = ["simulator", "status-bar", "pin"]
        for value in values:
            argv += ["--value", value]
        code, payload = self.android(*argv)
        self.assertEqual(code, 0, payload)
        return payload

    def kill_without_clear_and_boot_b(self) -> None:
        # What a real `adb emu kill` then `emulator -avd B` leaves behind:
        # the same serial, another AVD, a fresh boot, a live battery.
        self.device(self.AVD_B, self.BOOT_B, battery_overridden=False,
                    battery_level="77", signal_profile="4")

    def pin_calls(self) -> list[str]:
        return [call for call in self.adb_calls() if call.startswith(self.PIN_PREFIXES)]

    def test_the_record_carries_the_identity_it_was_taken_on(self) -> None:
        self.pin_on_a()
        self.assertEqual(self.record()["identity"],
                         {"avd": self.AVD_A, "boot": f"boot_id:{self.BOOT_A}"})
        # the same device keeps being re-pinned
        self.clear_log()
        code, payload = self.android("screenshot", "--out", str(Path(self.tmp.name) / "a.png"))
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["repinned"])
        self.assertNotIn("stale_pin_dropped", payload)

    def test_another_avd_on_the_same_serial_receives_no_pin_command(self) -> None:
        self.pin_on_a("battery=80", "mobile_level=3")
        self.kill_without_clear_and_boot_b()
        self.clear_log()
        code, payload = self.android("screenshot", "--out", str(Path(self.tmp.name) / "b.png"))
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.pin_calls(), [], self.adb_calls())
        self.assertFalse(payload["repinned"])
        self.assertTrue(payload["stale_pin_dropped"])
        self.assertIn("stale_pin_dropped", [w["code"] for w in payload["warnings"]])
        self.assertFalse(self.record_path().exists(), "the stale record must be removed")
        state = self.fake_state()
        self.assertEqual((state["battery_level"], state["battery_overridden"],
                          state["signal_profile"]), ("77", False, "4"))
        # and it stays gone: the next capture does not even mention a pin
        self.clear_log()
        code, payload = self.android("screenshot", "--out", str(Path(self.tmp.name) / "c.png"))
        self.assertEqual(code, 0, payload)
        self.assertNotIn("repinned", payload)
        self.assertEqual(self.pin_calls(), [])

    def test_a_flow_capture_on_another_avd_receives_no_pin_command(self) -> None:
        self.pin_on_a()
        self.kill_without_clear_and_boot_b()
        self.clear_log()
        result = simulator.repin_before_capture(self.android_target(), sleep=self.fail)
        self.assertEqual(result, {"repinned": False, "controls": [],
                                  "stale_pin_dropped": True})
        self.assertEqual(self.pin_calls(), [])
        self.assertFalse(self.record_path().exists())

    def test_a_reboot_of_the_same_avd_drops_the_pin(self) -> None:
        self.pin_on_a()
        self.device(self.AVD_A, "33333333-cccc-4000-8000-000000000003")
        self.clear_log()
        self.assertTrue(simulator.reassert_pins(self.android_target())["stale_pin_dropped"])
        self.assertEqual(self.pin_calls(), [])

    def test_clear_on_another_avd_does_not_restore_the_first_ones_snapshot(self) -> None:
        self.pin_on_a()
        self.assertEqual(self.record()["status_bar"]["battery"]["level"], 42)
        self.kill_without_clear_and_boot_b()
        self.clear_log()
        code, payload = self.android("simulator", "status-bar", "clear")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["stale_pin_dropped"])
        self.assertIn("stale_pin_dropped", [w["code"] for w in payload["warnings"]])
        self.assertFalse(payload["snapshot"])
        self.assertNotIn("shell dumpsys battery set level 42", self.adb_calls())
        self.assertFalse(self.fake_state()["battery_overridden"])
        self.assertFalse(self.record_path().exists())

    def test_animation_reset_on_another_avd_does_not_restore_the_first_ones_scales(self) -> None:
        scales = {key: "0.5" for key in simulator.ANDROID_ANIMATION_SCALES}
        self.set_state(settings=scales)
        self.device(self.AVD_A, self.BOOT_A)
        self.assertEqual(self.android("simulator", "animations", "pin")[0], 0)
        self.kill_without_clear_and_boot_b()
        code, payload = self.android("simulator", "animations", "reset")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["stale_pin_dropped"])
        self.assertFalse(payload["snapshot"])
        self.assertEqual(set(self.fake_state()["settings"].values()),
                         {simulator.ANDROID_DEFAULT_ANIMATION_SCALE})

    def test_a_new_pin_does_not_inherit_the_previous_devices_snapshot(self) -> None:
        self.pin_on_a()
        self.kill_without_clear_and_boot_b()
        code, payload = self.android("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["stale_pin_dropped"])
        record = self.record()
        self.assertEqual(record["identity"]["avd"], self.AVD_B)
        self.assertFalse(record["status_bar"]["battery"]["overridden"],
                         "the pre-pin snapshot must describe B, not A's override")

    def test_a_record_without_an_identity_is_never_replayed(self) -> None:
        self.record_path().parent.mkdir(parents=True, exist_ok=True)
        self.record_path().write_text(json.dumps({"status_bar": {"pinned": {
            "mode": "live", "battery": 5, "plugged": False, "mobile_level": 0,
            "hide_notification_icons": True}}}), encoding="utf-8")
        code, payload = self.android("screenshot", "--out", str(Path(self.tmp.name) / "l.png"))
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["stale_pin_dropped"])
        self.assertEqual(self.pin_calls(), [])

    def test_an_unreadable_identity_is_never_replayed(self) -> None:
        self.set_state()
        self.device(None, None)
        code, payload = self.android("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        self.assertIn("pin_not_device_bound", [w["code"] for w in payload["warnings"]])
        self.clear_log()
        code, payload = self.android("screenshot", "--out", str(Path(self.tmp.name) / "u.png"))
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.pin_calls(), [])

    def test_devices_shutdown_drops_the_record(self) -> None:
        self.pin_on_a()
        self.assertTrue(self.record_path().exists())
        code, payload = self.android("devices", "shutdown")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["pin_record_dropped"])
        self.assertFalse(self.record_path().exists())

    # RISK-012: emulators quickboot, and `emu kill` saves the snapshot the
    # next boot resumes from, so a pin left on the device outlived the
    # record that could undo it.
    RESTORE_PREFIXES = ("shell dumpsys battery", "emu gsm",
                        "shell cmd statusbar send-disable-flag",
                        "shell settings put global", "shell settings delete global")

    def pin_status_bar_and_animations_on_a(self) -> None:
        self.set_state(battery_overridden=True, battery_level="42",
                       settings={key: "0.5" for key in simulator.ANDROID_ANIMATION_SCALES})
        self.device(self.AVD_A, self.BOOT_A)
        for argv in (("simulator", "status-bar", "pin"), ("simulator", "animations", "pin")):
            code, payload = self.android(*argv)
            self.assertEqual(code, 0, payload)
        state = self.fake_state()
        self.assertEqual(state["battery_level"], "100")
        self.assertEqual(set(state["settings"].values()), {"0"})

    def restore_calls_and_kill(self) -> tuple[list[str], int]:
        calls = self.adb_calls()
        restore = [call for call in calls if call.startswith(self.RESTORE_PREFIXES)]
        return restore, calls.index("emu kill")

    def test_devices_shutdown_restores_both_pins_before_the_kill(self) -> None:
        self.pin_status_bar_and_animations_on_a()
        self.clear_log()
        code, payload = self.android("devices", "shutdown")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["pins_restored"], ["status-bar", "animations"])
        self.assertNotIn("warnings", payload)
        self.assertTrue(payload["pin_record_dropped"])
        self.assertFalse(self.record_path().exists())
        calls = self.adb_calls()
        restore, kill = self.restore_calls_and_kill()
        self.assertTrue(restore, calls)
        self.assertTrue(all(calls.index(call) < kill for call in restore), calls)
        # status bar first (the pre-pin override re-applied, icons back),
        # then the three scales put back to what the pin replaced
        battery = calls.index("shell dumpsys battery set level 42")
        icons = calls.index("shell cmd statusbar send-disable-flag none")
        scales = [calls.index(f"shell settings put global {key} 0.5")
                  for key in simulator.ANDROID_ANIMATION_SCALES]
        self.assertLess(battery, min(scales))
        self.assertLess(icons, min(scales))
        self.assertLess(max(scales), kill)
        state = self.fake_state()
        self.assertEqual((state["battery_level"], state["battery_overridden"]), ("42", True))
        self.assertEqual(set(state["settings"].values()), {"0.5"})

    def test_devices_shutdown_restores_nothing_on_another_avd(self) -> None:
        self.pin_status_bar_and_animations_on_a()
        self.kill_without_clear_and_boot_b()
        self.clear_log()
        code, payload = self.android("devices", "shutdown")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["pins_restored"], [])
        self.assertIn("stale_pin_dropped", [w["code"] for w in payload["warnings"]])
        restore, _ = self.restore_calls_and_kill()
        self.assertEqual(restore, [], self.adb_calls())
        self.assertTrue(payload["pin_record_dropped"])
        self.assertFalse(self.record_path().exists())

    def test_a_failed_restore_still_kills_and_drops_the_record(self) -> None:
        from autonom_lib import emulator as emulator_mod
        self.pin_status_bar_and_animations_on_a()
        self.clear_log()
        failure = errors.AutonomError(errors.BACKEND_FAILED, "adb went away")
        with mock.patch.object(simulator, "_android_status_bar_clear", side_effect=failure):
            detail = emulator_mod.kill_emulator(str(FAKE_ADB), SERIAL, timeout=5)
        self.assertEqual(detail["pins_restored"], ["animations"])
        self.assertEqual([w["code"] for w in detail["warnings"]], ["pin_restore_failed"])
        self.assertIn("emu kill", self.adb_calls())
        self.assertTrue(detail["gone"])
        self.assertFalse(self.record_path().exists())

    def test_devices_shutdown_without_a_record_sends_no_restore(self) -> None:
        self.set_state()
        code, payload = self.android("devices", "shutdown")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["pins_restored"], [])
        self.assertNotIn("warnings", payload)
        restore, _ = self.restore_calls_and_kill()
        self.assertEqual(restore, [])
        self.assertFalse(any("boot_id" in call for call in self.adb_calls()),
                         "no identity read without a record")

    def test_devices_boot_drops_a_record_left_on_its_serial(self) -> None:
        from autonom_lib import emulator as emulator_mod
        self.pin_on_a()
        self.set_state(devices=[], boot_serial=SERIAL)
        detail = emulator_mod.boot_avd(str(ROOT / "tests/fakes/fake_emulator.py"),
                                       str(FAKE_ADB), "Pixel_9", timeout=30)
        self.assertEqual(detail["serial"], SERIAL)
        self.assertTrue(detail["stale_pin_dropped"])
        self.assertFalse(self.record_path().exists())

    def test_ios_records_are_keyed_by_udid_and_need_no_identity(self) -> None:
        self.assertFalse(simulator.drop_stale_record(self.ios_target()))


class KeyboardMergeTests(HardeningBase):
    """CAPST-005: repeated pins never lose the original preferences."""

    ORIGINAL = {"AppleLocale": "en_US@rg=nlzzzz", "AppleLanguages": ["en-US"],
                "AppleKeyboards": ["en_US@sw=QWERTY"]}

    def prefs_dir(self) -> Path:
        path = self.devices_dir / UDID / "data/Library/Preferences"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def global_prefs(self) -> dict:
        with (self.prefs_dir() / ".GlobalPreferences.plist").open("rb") as handle:
            return plistlib.load(handle)

    def seed(self) -> None:
        with (self.prefs_dir() / ".GlobalPreferences.plist").open("wb") as handle:
            plistlib.dump(self.ORIGINAL, handle)

    def test_pin_then_locale_pin_then_reset_restores_the_original(self) -> None:
        # Reproduced: the second pin kept the first backup, which had no
        # global domain, and reset deleted AppleLocale en_US@rg=nlzzzz.
        self.seed()
        self.assertEqual(self.ios("simulator", "keyboard", "pin")[0], 0)
        code, pinned = self.ios("simulator", "keyboard", "pin", "--value", "locale=de-DE")
        self.assertEqual(code, 0, pinned)
        self.assertEqual(self.global_prefs()["AppleLocale"], "de_DE")
        code, reset = self.ios("simulator", "keyboard", "reset")
        self.assertEqual(code, 0, reset)
        self.assertTrue(reset["verified"])
        self.assertEqual(self.global_prefs(), self.ORIGINAL)

    def test_pin_without_locale_then_reset_keeps_the_device_locale(self) -> None:
        self.seed()
        self.ios("simulator", "keyboard", "pin")
        code, reset = self.ios("simulator", "keyboard", "reset")
        self.assertEqual(code, 0, reset)
        self.assertNotIn(".GlobalPreferences", reset["removed"])
        self.assertEqual(self.global_prefs(), self.ORIGINAL)

    def test_locale_pin_keeps_the_region_in_apple_languages(self) -> None:
        self.assertEqual(ios_prefs.keyboard_pins("en-US")[ios_prefs.GLOBAL_DOMAIN],
                         {"AppleLocale": "en_US", "AppleLanguages": ["en-US"]})
        self.assertEqual(ios_prefs.keyboard_pins("de")[ios_prefs.GLOBAL_DOMAIN]
                         ["AppleLanguages"], ["de"])
        self.assertEqual(ios_prefs.language_tag("zh_Hans_CN"), "zh-Hans-CN")

    def test_record_backup_merges_new_keys_only(self) -> None:
        self.seed()
        first = ios_prefs.record_backup(UDID, ios_prefs.keyboard_pins(None))
        self.assertIsNotNone(first)
        # The pinned values are now live; a merge must not pick them up.
        ios_prefs.apply_pins(UDID, ios_prefs.keyboard_pins("fr-FR"))
        self.assertIsNotNone(ios_prefs.record_backup(UDID, ios_prefs.keyboard_pins("fr-FR")))
        self.assertIsNone(ios_prefs.record_backup(UDID, ios_prefs.keyboard_pins("fr-FR")))
        backup = ios_prefs.read_backup(UDID)
        self.assertEqual(backup[".GlobalPreferences"]["AppleLocale"], "fr_FR",
                         "the key was recorded after apply_pins wrote it")
        self.assertEqual(backup["com.apple.Preferences"]["KeyboardAutocorrection"],
                         {"__absent__": True})


class KeyboardRebootTests(HardeningBase):
    """CAPST-009: nothing is written unless the device really shut down."""

    def prefs_dir(self) -> Path:
        path = self.devices_dir / UDID / "data/Library/Preferences"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def device(self, state: str, **extra) -> None:
        self.set_state(simctl_devices={"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": state, "isAvailable": True}
            ]}}, **extra)

    def test_a_shutdown_that_leaves_the_device_booted_is_refused(self) -> None:
        self.prefs_dir()
        self.device("Booted", shutdown_ignored=True)
        code, payload = self.ios("simulator", "keyboard", "pin", "--value", "reboot=true")
        self.refused(code, payload, errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertEqual(payload["state"], "Booted")
        self.assertEqual(list(self.prefs_dir().iterdir()), [], "nothing may be written")

    def test_transitional_states_are_refused_even_with_reboot(self) -> None:
        self.prefs_dir()
        for state in ("Booting", "Shutting Down"):
            with self.subTest(state=state):
                self.device(state)
                code, payload = self.ios("simulator", "keyboard", "pin",
                                         "--value", "reboot=true")
                self.refused(code, payload, errors.SIMULATOR_MUST_BE_SHUTDOWN)
                self.assertFalse(any(argv[1:2] == ["shutdown"]
                                     for argv in self.argv_log("simctl")))
                self.assertEqual(list(self.prefs_dir().iterdir()), [])

    def test_a_failed_boot_does_not_mask_the_write_failure(self) -> None:
        self.prefs_dir()
        self.device("Booted")
        write_failure = errors.AutonomError(errors.BACKEND_FAILED, "write failed")
        boot_failure = errors.AutonomError(errors.IOS_BOOT_FAILED, "boot failed")
        with mock.patch.object(ios_prefs, "apply_pins", side_effect=write_failure), \
                mock.patch.object(ios_simctl, "boot", side_effect=boot_failure) as boot:
            with self.assertRaises(errors.AutonomError) as caught:
                simulator.apply(self.ios_target(), "keyboard", "pin", {"reboot": "true"})
        self.assertIs(caught.exception, write_failure)
        boot.assert_called_once()


class IosAndroidKeyTests(HardeningBase):
    """CAPST-006: Android-only keys are refused on iOS with the iOS spelling."""

    def test_mode_live_is_refused_with_a_hint(self) -> None:
        code, payload = self.ios("simulator", "status-bar", "pin", "--value", "mode=live")
        self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
        self.assertIn("hint", payload)
        self.assertFalse(any("--mode" in argv for argv in self.argv_log("simctl")))

    def test_each_android_key_names_its_ios_equivalent(self) -> None:
        for key, hint in (("hhmm=0941", "time=9:41"), ("wifi=show", "wifiMode"),
                          ("mobile_level=2", "cellularBars"),
                          ("notifications=false", "notification"),
                          ("wifi_level=3", "wifiBars"), ("mobile=hide", "cellularMode")):
            with self.subTest(key=key):
                code, payload = self.ios("simulator", "status-bar", "override", "--value", key)
                self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
                self.assertIn(hint, payload["hint"])
        self.assertEqual(self.argv_log("simctl"), [])


class AnimationTests(HardeningBase):
    """CAPST-007: the three Android animation scales, pinned off and back."""

    def scales(self) -> dict:
        return {key: self.fake_state().get("settings", {}).get(key)
                for key in simulator.ANDROID_ANIMATION_SCALES}

    def test_pin_zeroes_all_three_and_reset_restores_them(self) -> None:
        self.set_state(settings={key: "1.0" for key in simulator.ANDROID_ANIMATION_SCALES})
        target = self.android_target()
        pinned = simulator.apply(target, "animations", "pin", {})
        self.assertTrue(pinned["verified"])
        self.assertEqual(set(self.scales().values()), {"0"})
        shown = simulator.apply(target, "animations", "show", {})
        self.assertTrue(shown["pinned"])
        self.assertTrue(shown["snapshot"])
        # A second pin must not record the zeroes as the original.
        simulator.apply(target, "animations", "pin", {})
        reset = simulator.apply(target, "animations", "reset", {})
        self.assertTrue(reset["verified"])
        self.assertEqual(set(self.scales().values()), {"1.0"})
        self.assertFalse(simulator.apply(target, "animations", "show", {})["pinned"])

    def test_reset_deletes_scales_that_were_unset(self) -> None:
        target = self.android_target()
        simulator.apply(target, "animations", "pin", {})
        reset = simulator.apply(target, "animations", "reset", {})
        self.assertTrue(reset["verified"])
        self.assertEqual(set(self.scales().values()), {None})
        self.assertIn("shell settings delete global window_animation_scale", self.adb_calls())

    def test_reset_without_a_snapshot_restores_the_default(self) -> None:
        self.set_state(settings={key: "0" for key in simulator.ANDROID_ANIMATION_SCALES})
        reset = simulator.apply(self.android_target(), "animations", "reset", {})
        self.assertFalse(reset["snapshot"])
        self.assertEqual(set(self.scales().values()), {"1"})

    def test_ios_is_refused(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            simulator.apply(self.ios_target(), "animations", "pin", {})
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(self.argv_log("simctl"), [])

    def test_unknown_action_is_refused(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            simulator.apply(self.android_target(), "animations", "off", {})
        self.assertEqual(caught.exception.code, errors.INVALID_SIMULATOR_ACTION)
        self.assertEqual(self.argv_log("adb"), [])


class ControlActionTests(HardeningBase):
    """CAPST-008: every control whitelists its actions and values."""

    def test_unknown_actions_are_refused_before_any_device_command(self) -> None:
        cases = [
            (self.android, "battery", "bogus"), (self.android, "network", "offlien"),
            (self.android, "call", "bogus"), (self.android, "sms", "bogus"),
            (self.android, "clipboard", "get"), (self.android, "appearance", "dim"),
            (self.ios, "push", "bogus"), (self.ios, "appearance", "dim"),
            (self.ios, "battery", "bogus"), (self.ios, "biometric", "wink"),
        ]
        for run, control, action in cases:
            with self.subTest(control=control, action=action):
                code, payload = run("simulator", control, action)
                self.refused(code, payload, errors.INVALID_SIMULATOR_ACTION)
                self.assertTrue(payload["valid_actions"])
                self.assertIn("Valid", payload["hint"])
        self.assertEqual(self.argv_log("adb"), [])
        self.assertEqual(self.argv_log("simctl"), [])

    def test_text_size_values_are_whitelisted(self) -> None:
        code, payload = self.ios("simulator", "text-size", "bogus")
        self.refused(code, payload, errors.INVALID_SIMULATOR_ACTION)
        self.assertIn("accessibility-large", payload["valid_actions"])
        code, payload = self.android("simulator", "text-size", "huge")
        self.refused(code, payload, errors.INVALID_SIMULATOR_ACTION)
        code, payload = self.android("simulator", "text-size", "3")
        self.refused(code, payload, errors.INVALID_SIMULATOR_ACTION)
        self.assertEqual(self.argv_log("adb") + self.argv_log("simctl"), [])

    def test_ios_battery_level_is_bounded(self) -> None:
        for level in ("150", "-1", "full"):
            with self.subTest(level=level):
                code, payload = self.ios("simulator", "battery", "set", "--value",
                                         f"level={level}")
                self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.argv_log("simctl"), [])

    def test_network_shape_values_are_checked(self) -> None:
        code, payload = self.android("simulator", "network", "shape", "--value", "speed=warp")
        self.refused(code, payload, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.argv_log("adb"), [])
        code, payload = self.android("simulator", "network", "shape", "--value", "speed=edge",
                                     "--value", "delay=100:200")
        self.assertEqual(code, 0, payload)

    def test_ios_text_size_and_appearance_are_read_back(self) -> None:
        code, payload = self.ios("simulator", "text-size", "accessibility-large")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["observed"], "accessibility-large")
        self.assertIn(["simctl", "ui", UDID, "content_size"], self.argv_log("simctl"))
        code, payload = self.ios("simulator", "appearance", "dark")
        self.assertTrue(payload["verified"])

    def test_android_text_size_and_appearance_are_read_back(self) -> None:
        code, payload = self.android("simulator", "text-size", "1.3")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        code, payload = self.android("simulator", "appearance", "dark")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["observed"], "yes")

    def test_controls_without_a_read_back_are_not_verified(self) -> None:
        for run, argv in ((self.ios, ["battery", "set", "--value", "level=50"]),
                          (self.android, ["network", "offline"]),
                          (self.android, ["biometric", "match"]),
                          (self.ios, ["status-bar", "pin"])):
            with self.subTest(argv=argv):
                code, payload = run("simulator", *argv)
                self.assertEqual(code, 0, payload)
                self.assertFalse(payload["verified"])
                self.assertEqual(payload["verification"], "unavailable")

    def test_android_clipboard_without_the_shell_command_is_unsupported(self) -> None:
        self.set_state(clipboard_unsupported=True)
        code, payload = self.android("simulator", "clipboard", "set", "--value", "text=hi")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["verified"])
        self.assertFalse(payload["supported"])
        self.assertEqual(payload["verification"], "unsupported")
        self.assertEqual(payload["warnings"][0]["code"], "clipboard_unsupported")

    def test_android_clipboard_text_survives_the_device_shell(self) -> None:
        text = "it's $HOME; echo gone && `id` \"quoted\""
        code, payload = self.android("simulator", "clipboard", "set", "--json",
                                     json.dumps({"text": text}))
        self.assertEqual(code, 0, payload)
        state = self.fake_state()
        self.assertEqual(state["clipboard"], text)
        self.assertEqual(state["clipboard_words"], 1, "one argument, not a parsed line")
        self.assertEqual(simulator.shell_quote("a'b"), "'a'\\''b'")

    def test_ios_clipboard_is_read_back(self) -> None:
        code, payload = self.ios("simulator", "clipboard", "set", "--value", "text=hello")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        self.assertEqual(self.fake_state()["pasteboard"], "hello")


class BiometryKindTests(unittest.TestCase):
    def test_face_and_touch_id_devices(self) -> None:
        kind = simulator.biometry_kind
        prefix = "com.apple.CoreSimulator.SimDeviceType."
        self.assertEqual(kind(prefix + "iPhone-17-Pro", "iPhone 17 Pro"), "face")
        self.assertEqual(kind(prefix + "iPhone-X", "iPhone X"), "face")
        self.assertEqual(kind(prefix + "iPhone-16e", "iPhone 16e"), "face")
        self.assertEqual(kind(prefix + "iPhone-8", "iPhone 8"), "touch")
        self.assertEqual(kind(prefix + "iPhone-SE-3rd-generation",
                              "iPhone SE (3rd generation)"), "touch")
        self.assertEqual(kind(prefix + "iPad-Pro-11-inch-4th-generation-8GB",
                              "iPad Pro (11-inch) (4th generation)"), "face")
        self.assertEqual(kind(prefix + "iPad-Pro--12-9-inch---2nd-generation-",
                              "iPad Pro (12.9-inch) (2nd generation)"), "touch")
        self.assertEqual(kind(prefix + "iPad-Air-5th-generation",
                              "iPad Air (5th generation)"), "touch")
        self.assertIsNone(kind("com.example.Unknown", "Custom"))


if __name__ == "__main__":
    unittest.main()
