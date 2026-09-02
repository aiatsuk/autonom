"""Deterministic capture state: status-bar pin and keyboard/locale pin.

Two captures of the same screen used to differ by the clock, the battery
glyph, or a typed word autocorrect had rewritten — noise that a before/after
comparison then reported as a change. Everything here runs against the fake
tools, so what is asserted is the argv that was actually dispatched and the
files that were actually written, not what the CLI claims.
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

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_prefs  # noqa: E402

try:
    from env_isolation import EnvSandbox, EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandbox, EnvSandboxMixin  # noqa: E402


class CaptureStateBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.devices_dir = root / "Devices"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(root / "home"),
            AUTONOM_CORESIMULATOR_DEVICES=str(self.devices_dir),
        )
        self.env = dict(os.environ)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_state(self, **kwargs) -> None:
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def argv_log(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["tool"] == tool]

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, env=self.env, timeout=60,
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed.returncode, json.loads(stream)

    def android(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", "emulator-5554", *argv)

    def ios(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID, *argv)

    def demo_broadcasts(self) -> list[str]:
        """The SystemUI demo-mode commands, in dispatch order, as `command …` text."""
        found = []
        for argv in self.argv_log("adb"):
            if "com.android.systemui.demo" in argv:
                found.append(" ".join(argv[argv.index("command") + 1:]))
        return found


class AndroidStatusBarTests(CaptureStateBase):
    def test_pin_sends_the_whole_deterministic_preset_in_order(self) -> None:
        code, payload = self.android("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["values"]["hhmm"], "0941")
        self.assertEqual(self.demo_broadcasts(), [
            "enter",
            "clock -e hhmm 0941",
            "battery -e level 100 -e plugged false",
            "network -e wifi show -e level 4 -e fully true",
            "network -e mobile hide",
            "notifications -e visible false",
        ])
        allowed = [argv for argv in self.argv_log("adb") if "sysui_demo_allowed" in argv]
        self.assertEqual(len(allowed), 1, "demo mode must be allowed before it is entered")

    def test_pin_values_override_the_preset(self) -> None:
        code, payload = self.android("simulator", "status-bar", "pin", "--value", "hhmm=12:30")
        self.assertEqual(code, 0)
        self.assertEqual(payload["values"]["hhmm"], "1230")
        self.assertIn("clock -e hhmm 1230", self.demo_broadcasts())

    def test_override_sends_only_the_given_keys(self) -> None:
        code, payload = self.android("simulator", "status-bar", "override",
                                     "--value", "wifi=hide", "--value", "notifications=false")
        self.assertEqual(code, 0)
        self.assertEqual(self.demo_broadcasts(),
                         ["enter", "network -e wifi hide", "notifications -e visible false"])
        self.assertEqual(payload["values"], {"wifi": "hide", "notifications": "false"})

    def test_override_still_accepts_the_original_hhmm_key(self) -> None:
        """0.30 callers passed only hhmm; that must keep working unchanged."""
        code, payload = self.android("simulator", "status-bar", "override", "--value", "hhmm=0941")
        self.assertEqual(code, 0)
        self.assertIn("clock -e hhmm 0941", self.demo_broadcasts())

    def test_unknown_key_is_refused_before_anything_is_sent(self) -> None:
        code, payload = self.android("simulator", "status-bar", "override", "--value", "bogus=1")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.FLOW_COMMAND_INVALID)
        self.assertIn("hhmm", payload["hint"])
        self.assertEqual(self.demo_broadcasts(), [])

    def test_bad_clock_is_refused(self) -> None:
        code, payload = self.android("simulator", "status-bar", "pin", "--value", "hhmm=noon")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.FLOW_COMMAND_INVALID)

    def test_clear_exits_demo_mode(self) -> None:
        code, payload = self.android("simulator", "status-bar", "clear")
        self.assertEqual(code, 0)
        self.assertEqual(self.demo_broadcasts(), ["exit"])
        self.assertEqual(payload["values"], {})

    def test_unknown_action_is_refused(self) -> None:
        code, payload = self.android("simulator", "status-bar", "freeze")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.FLOW_COMMAND_INVALID)

    def test_hardware_is_refused(self) -> None:
        code, payload = self.run_cli("--adb", str(FAKE_ADB), "--serial", "R58M123ABC",
                                     "simulator", "status-bar", "pin")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(self.demo_broadcasts(), [])


class IosStatusBarTests(CaptureStateBase):
    def _status_bar_calls(self) -> list[list[str]]:
        return [argv for argv in self.argv_log("simctl") if argv[1:2] == ["status_bar"]]

    def test_pin_overrides_time_battery_and_signal(self) -> None:
        code, payload = self.ios("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        calls = self._status_bar_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:4], ["simctl", "status_bar", UDID, "override"])
        flags = dict(zip(argv[4::2], argv[5::2]))
        self.assertEqual(flags["--time"], "9:41")
        self.assertEqual(flags["--batteryState"], "charged")
        self.assertEqual(flags["--batteryLevel"], "100")
        self.assertEqual(flags["--wifiMode"], "active")
        self.assertEqual(flags["--cellularBars"], "4")
        self.assertEqual(flags["--dataNetwork"], "5g")

    def test_pin_values_override_the_preset(self) -> None:
        code, payload = self.ios("simulator", "status-bar", "pin", "--value", "time=10:00")
        self.assertEqual(code, 0)
        self.assertEqual(payload["values"]["time"], "10:00")
        argv = self._status_bar_calls()[0]
        self.assertEqual(argv[argv.index("--time") + 1], "10:00")

    def test_clear_restores_the_live_bar(self) -> None:
        code, _ = self.ios("simulator", "status-bar", "clear")
        self.assertEqual(code, 0)
        self.assertEqual(self._status_bar_calls(), [["simctl", "status_bar", UDID, "clear"]])


class KeyboardPinTests(CaptureStateBase):
    def prefs_dir(self) -> Path:
        path = self.devices_dir / UDID / "data/Library/Preferences"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def read_plist(self, domain: str) -> dict:
        with (self.prefs_dir() / f"{domain}.plist").open("rb") as handle:
            return plistlib.load(handle)

    def lifecycle_calls(self) -> list[str]:
        return [argv[1] for argv in self.argv_log("simctl")
                if argv[1:2] in (["shutdown"], ["bootstatus"])]

    def test_pin_writes_every_domain_and_verifies_by_reading_back(self) -> None:
        self.prefs_dir()
        code, payload = self.ios("simulator", "keyboard", "pin", "--value", "locale=en-US")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        self.assertFalse(payload["rebooted"])
        self.assertEqual(payload["locale"], "en_US")
        self.assertEqual(self.read_plist("com.apple.Preferences"), {
            "KeyboardAutocorrection": False, "KeyboardPrediction": False,
            "KeyboardAutocapitalization": False,
        })
        self.assertEqual(self.read_plist("com.apple.keyboard.preferences"), {
            "KeyboardAutocorrection": False, "KeyboardPrediction": False,
        })
        self.assertEqual(self.read_plist(".GlobalPreferences"),
                         {"AppleLocale": "en_US", "AppleLanguages": ["en"]})
        # A shut-down simulator needs no lifecycle churn.
        self.assertEqual(self.lifecycle_calls(), [])

    def test_pin_keeps_unrelated_keys_in_an_existing_store(self) -> None:
        with (self.prefs_dir() / "com.apple.Preferences.plist").open("wb") as handle:
            plistlib.dump({"SomethingElse": "kept", "KeyboardPrediction": True}, handle)
        code, _ = self.ios("simulator", "keyboard", "pin")
        self.assertEqual(code, 0)
        stored = self.read_plist("com.apple.Preferences")
        self.assertEqual(stored["SomethingElse"], "kept")
        self.assertFalse(stored["KeyboardPrediction"])
        # No locale asked for: the global domain is left alone.
        self.assertFalse((self.prefs_dir() / ".GlobalPreferences.plist").exists())

    def test_pin_refuses_a_booted_simulator_without_reboot(self) -> None:
        self.prefs_dir()
        self.set_state(simctl_devices={"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}
            ]}})
        code, payload = self.ios("simulator", "keyboard", "pin")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertIn("reboot=true", payload["hint"])
        self.assertEqual(list(self.prefs_dir().iterdir()), [], "nothing may be written")

    def test_pin_with_reboot_shuts_down_writes_and_boots_again(self) -> None:
        self.prefs_dir()
        self.set_state(simctl_devices={"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}
            ]}})
        code, payload = self.ios("simulator", "keyboard", "pin", "--value", "reboot=true")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["rebooted"])
        self.assertTrue(payload["verified"])
        self.assertEqual(self.lifecycle_calls(), ["shutdown", "bootstatus"])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        entry = state["simctl_devices"]["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-0"][0]
        self.assertEqual(entry["state"], "Booted", "the simulator must come back up")

    def test_show_reports_pinned_before_and_after(self) -> None:
        self.prefs_dir()
        code, before = self.ios("simulator", "keyboard", "show", "--value", "locale=de-DE")
        self.assertEqual(code, 0)
        self.assertFalse(before["pinned"])
        self.assertIsNone(before["observed"]["com.apple.Preferences"]["KeyboardAutocorrection"])
        self.ios("simulator", "keyboard", "pin", "--value", "locale=de-DE")
        code, after = self.ios("simulator", "keyboard", "show", "--value", "locale=de-DE")
        self.assertTrue(after["pinned"])
        self.assertEqual(after["observed"][".GlobalPreferences"]["AppleLanguages"], ["de"])

    def test_reset_removes_only_the_owned_keys(self) -> None:
        self.prefs_dir()
        self.ios("simulator", "keyboard", "pin", "--value", "locale=en-US")
        with (self.prefs_dir() / ".GlobalPreferences.plist").open("rb") as handle:
            stored = plistlib.load(handle)
        stored["AppleKeyboards"] = ["en_US@sw=QWERTY"]
        with (self.prefs_dir() / ".GlobalPreferences.plist").open("wb") as handle:
            plistlib.dump(stored, handle)
        # No locale= on reset: the locale pinned earlier must still be removed.
        code, payload = self.ios("simulator", "keyboard", "reset")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        self.assertEqual(sorted(payload["removed"][".GlobalPreferences"]),
                         ["AppleLanguages", "AppleLocale"])
        self.assertEqual(self.read_plist(".GlobalPreferences"),
                         {"AppleKeyboards": ["en_US@sw=QWERTY"]})
        self.assertEqual(self.read_plist("com.apple.Preferences"), {})

    def test_reset_restores_the_values_pin_replaced(self) -> None:
        """The first real device carried a region override the pin flattened."""
        with (self.prefs_dir() / ".GlobalPreferences.plist").open("wb") as handle:
            plistlib.dump({"AppleLocale": "en_US@rg=nlzzzz", "AppleLanguages": ["en-US"],
                           "AppleKeyboards": ["en_US@sw=QWERTY"]}, handle)
        with (self.prefs_dir() / "com.apple.Preferences.plist").open("wb") as handle:
            plistlib.dump({"KeyboardPrediction": True}, handle)
        code, pinned = self.ios("simulator", "keyboard", "pin", "--value", "locale=en-US")
        self.assertEqual(code, 0, pinned)
        self.assertTrue(Path(pinned["backup"]).exists())
        self.assertEqual(self.read_plist(".GlobalPreferences")["AppleLocale"], "en_US")
        code, shown = self.ios("simulator", "keyboard", "show")
        self.assertTrue(shown["backup"])
        code, reset = self.ios("simulator", "keyboard", "reset")
        self.assertEqual(code, 0, reset)
        self.assertTrue(reset["backup"])
        self.assertTrue(reset["verified"])
        self.assertEqual(reset["restored"][".GlobalPreferences"],
                         {"AppleLocale": "en_US@rg=nlzzzz", "AppleLanguages": ["en-US"]})
        self.assertEqual(reset["restored"]["com.apple.Preferences"],
                         {"KeyboardPrediction": True})
        self.assertEqual(sorted(reset["removed"]["com.apple.Preferences"]),
                         ["KeyboardAutocapitalization", "KeyboardAutocorrection"])
        self.assertEqual(self.read_plist(".GlobalPreferences"), {
            "AppleLocale": "en_US@rg=nlzzzz", "AppleLanguages": ["en-US"],
            "AppleKeyboards": ["en_US@sw=QWERTY"]})
        self.assertEqual(self.read_plist("com.apple.Preferences"), {"KeyboardPrediction": True})
        self.assertFalse(Path(pinned["backup"]).exists(), "the backup is consumed by reset")
        code, shown = self.ios("simulator", "keyboard", "show")
        self.assertFalse(shown["backup"])

    def test_a_second_pin_keeps_the_original_snapshot(self) -> None:
        with (self.prefs_dir() / ".GlobalPreferences.plist").open("wb") as handle:
            plistlib.dump({"AppleLocale": "de_DE"}, handle)
        self.ios("simulator", "keyboard", "pin", "--value", "locale=en-US")
        self.ios("simulator", "keyboard", "pin", "--value", "locale=fr-FR")
        self.assertEqual(self.read_plist(".GlobalPreferences")["AppleLocale"], "fr_FR")
        code, reset = self.ios("simulator", "keyboard", "reset")
        self.assertEqual(reset["restored"][".GlobalPreferences"]["AppleLocale"], "de_DE")
        self.assertEqual(self.read_plist(".GlobalPreferences"), {"AppleLocale": "de_DE"})

    def test_reset_without_a_backup_falls_back_to_deleting(self) -> None:
        """A pin made by hand (or a lost state root) still resets cleanly."""
        with (self.prefs_dir() / "com.apple.Preferences.plist").open("wb") as handle:
            plistlib.dump({"KeyboardAutocorrection": False, "Other": 1}, handle)
        code, reset = self.ios("simulator", "keyboard", "reset")
        self.assertEqual(code, 0, reset)
        self.assertFalse(reset["backup"])
        self.assertEqual(reset["restored"], {})
        self.assertEqual(reset["removed"], {"com.apple.Preferences": ["KeyboardAutocorrection"]})
        self.assertTrue(reset["verified"])
        self.assertEqual(self.read_plist("com.apple.Preferences"), {"Other": 1})

    def test_invalid_locale_is_refused(self) -> None:
        self.prefs_dir()
        code, payload = self.ios("simulator", "keyboard", "pin", "--value", "locale=nope!")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.FLOW_COMMAND_INVALID)

    def test_missing_data_directory_is_a_named_failure(self) -> None:
        code, payload = self.ios("simulator", "keyboard", "pin")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.SIMULATOR_DATA_NOT_FOUND)
        self.assertIn("AUTONOM_CORESIMULATOR_DEVICES", payload["hint"])

    def test_unknown_action_is_refused(self) -> None:
        self.prefs_dir()
        code, payload = self.ios("simulator", "keyboard", "toggle")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.FLOW_COMMAND_INVALID)

    def test_android_refuses_honestly(self) -> None:
        code, payload = self.android("simulator", "keyboard", "pin")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.UNSUPPORTED_CAPABILITY)
        self.assertIn("Gboard", payload["hint"])
        self.assertEqual(self.argv_log("adb"), [], "no adb call for a refused verb")


class IosPrefsUnitTests(unittest.TestCase):
    def test_locale_normalises_to_the_apple_locale_form(self) -> None:
        self.assertEqual(ios_prefs.normalize_locale("en-US"), "en_US")
        self.assertEqual(ios_prefs.normalize_locale("pt_BR"), "pt_BR")
        self.assertEqual(ios_prefs.normalize_locale("zh-Hans-CN"), "zh_Hans_CN")
        with self.assertRaises(errors.AutonomError):
            ios_prefs.normalize_locale("English (US)")

    def test_pins_without_locale_leave_the_global_domain_out(self) -> None:
        pins = ios_prefs.keyboard_pins(None)
        self.assertNotIn(ios_prefs.GLOBAL_DOMAIN, pins)
        self.assertEqual(pins["com.apple.Preferences"]["KeyboardAutocorrection"], False)

    def test_owned_keys_always_include_the_locale_pair(self) -> None:
        owned = ios_prefs.owned_keys(ios_prefs.keyboard_pins(None))
        self.assertEqual(owned[ios_prefs.GLOBAL_DOMAIN], ios_prefs.LOCALE_KEYS)

    def test_backups_live_under_the_machine_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = EnvSandbox()
            sandbox.set_env(AUTONOM_HOME=tmp, XDG_STATE_HOME=None)
            try:
                self.assertEqual(ios_prefs.backup_path("X"),
                                 Path(tmp) / "simulator-prefs" / "X.json")
                sandbox.set_env(AUTONOM_HOME=None, XDG_STATE_HOME=tmp)
                self.assertEqual(ios_prefs.state_root(), Path(tmp) / "autonom")
            finally:
                sandbox.doCleanups()


class ScreenshotSizeTests(CaptureStateBase):
    def test_screenshot_reports_its_pixel_size(self) -> None:
        code, payload = self.android("screenshot", "--out", str(Path(self.tmp.name) / "s.png"))
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["width"], payload["height"]), (1, 1))
        code, shown = self.android("shots", "show", payload["path"])
        self.assertEqual(code, 0)
        self.assertEqual((shown["width"], shown["height"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
