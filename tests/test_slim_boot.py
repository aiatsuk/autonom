"""`devices boot --slim` and `session start --slim`: boot a simulator slimmed.

Hermetic like test_ios_slim.py: the fake `simctl` and a temporary override
store; no real simulator is booted or written.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_slim, simulator_slim  # noqa: E402
from autonom_lib.platform import ANDROID, Target  # noqa: E402

try:
    from test_ios_slim import FAKE_SIMCTL, UDID, SlimBase  # noqa: E402
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.test_ios_slim import FAKE_SIMCTL, UDID, SlimBase  # noqa: E402

MINIMAL = ios_slim.resolve({"preset": "minimal"})
OLD_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-17-5"


class BootBase(SlimBase):
    def boot_error(self, value: str, target=None) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            simulator_slim.boot(target or self.target, value)
        error = caught.exception
        self.assertTrue(error.code and error.message and error.hint, error.as_dict())
        return error

    def devices_boot(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                            "devices", "boot", *argv)


class FlagTests(unittest.TestCase):
    def test_a_preset_id_is_a_preset_and_anything_else_a_profile_path(self) -> None:
        for preset in ios_slim.PRESETS:
            self.assertEqual(simulator_slim.flag_values(preset), {"preset": preset})
        self.assertEqual(simulator_slim.flag_values("./p.json"), {"profile": "./p.json"})
        self.assertEqual(simulator_slim.flag_values("Minimal"), {"profile": "Minimal"})
        with self.assertRaises(errors.AutonomError) as caught:
            simulator_slim.flag_values("  ")
        self.assertEqual(caught.exception.code, errors.SLIM_PROFILE_INVALID)
        self.assertTrue(caught.exception.hint)


class BootTests(BootBase):
    def test_shutdown_simulator_is_written_booted_and_read_back(self) -> None:
        self.set_device("Shutdown")
        booted, block = simulator_slim.boot(self.target, "minimal")
        self.assertTrue(booted)
        self.assertEqual(self.device_state(), "Booted")
        self.assertEqual(self.launchd_disabled(), set(MINIMAL))
        self.assertEqual(block["state"], "slim")
        self.assertEqual(block["disabled"], len(MINIMAL))
        self.assertTrue(block["verified"])
        self.assertEqual(block["verification"], "read_back")
        self.assertEqual(block["preset"], "minimal")
        self.assertEqual(block["changed"], len(MINIMAL))
        self.assertEqual(self.lifecycle(), ["bootstatus"])
        # restore knows what to undo
        self.assertEqual(set(self.saved()["disabled"]), set(MINIMAL))

    def test_profile_path(self) -> None:
        self.set_device("Shutdown")
        path = self.root / "profile.json"
        path.write_text(json.dumps({"schema": ios_slim.PROFILE_SCHEMA, "preset": "minimal",
                                    "need": ["storekit"]}), encoding="utf-8")
        booted, block = simulator_slim.boot(self.target, str(path))
        self.assertTrue(booted)
        self.assertEqual(block["profile"], str(path))
        self.assertEqual(self.launchd_disabled(),
                         set(MINIMAL - ios_slim.feature_labels("storekit")))

    def test_booted_and_matching_changes_nothing(self) -> None:
        self.set_device("Booted")
        self.set_launchd(disabled=MINIMAL)
        booted, block = simulator_slim.boot(self.target, "minimal")
        self.assertFalse(booted)
        self.assertEqual(block["changed"], 0)
        self.assertEqual(block["state"], "slim")
        self.assertTrue(block["verified"])
        self.assertIn("nothing changed", block["note"])
        self.assertEqual(self.lifecycle(), [])
        self.assertFalse(self.store_path().exists())

    def test_booted_stock_matches_stock(self) -> None:
        self.set_device("Booted")
        booted, block = simulator_slim.boot(self.target, "stock")
        self.assertFalse(booted)
        self.assertEqual(block["state"], "stock")

    def test_booted_and_different_is_refused_untouched(self) -> None:
        self.set_device("Booted")
        error = self.boot_error("minimal")
        self.assertEqual(error.code, errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertIn("devices shutdown", error.hint)
        self.assertIn("simulator slim apply --value reboot=true", error.hint)
        self.assertEqual(error.extra["to_disable"], len(MINIMAL))
        self.assertEqual(self.device_state(), "Booted")
        self.assertEqual(self.lifecycle(), [])
        self.assertFalse(self.store_path().exists())
        self.assertFalse(self.state_file().exists())

    def test_in_flux_is_refused(self) -> None:
        self.set_device("Booting")
        self.assertEqual(self.boot_error("minimal").code, errors.SIMULATOR_MUST_BE_SHUTDOWN)

    def test_bad_profile_is_refused_before_the_device_is_touched(self) -> None:
        self.set_device("Shutdown")
        error = self.boot_error(str(self.root / "missing.json"))
        self.assertEqual(error.code, errors.SLIM_PROFILE_INVALID)
        self.assertEqual(self.calls(), [])

    def test_old_runtime_is_refused_and_not_booted(self) -> None:
        self.set_device("Shutdown", runtime=OLD_RUNTIME)
        error = self.boot_error("minimal")
        self.assertEqual(error.code, errors.SLIM_UNSUPPORTED_RUNTIME)
        self.assertEqual(self.device_state(), "Shutdown")

    def test_store_ignored_falls_back_and_still_verifies(self) -> None:
        # a runtime that ignores the store: the overrides are set one by one
        # with launchctl, the simulator is rebooted once and read back again
        self.set_device("Shutdown")
        self.patch_fake(slim_store_ignored=True)
        booted, block = simulator_slim.boot(self.target, "minimal")
        self.assertTrue(booted)
        self.assertTrue(block["fallback"])
        self.assertTrue(block["verified"])
        self.assertEqual(self.launchd_disabled(), set(MINIMAL))
        self.assertEqual(self.lifecycle(), ["bootstatus", "shutdown", "bootstatus"])

    def test_overrides_lost_is_slim_not_persisted(self) -> None:
        self.set_device("Shutdown")
        self.patch_fake(slim_overrides_lost=True)
        error = self.boot_error("minimal")
        self.assertEqual(error.code, errors.SLIM_NOT_PERSISTED)
        self.assertIs(error.extra["store_written"], True)
        self.assertEqual(error.extra["state_file"], str(self.state_file()))
        self.assertEqual(self.saved()["status"], "not_persisted")

    def test_a_boot_that_fails_after_the_write_says_so(self) -> None:
        self.set_device("Shutdown")
        self.patch_fake(simctl_fail={"simctl bootstatus": [1, "Unable to boot device"]})
        error = self.boot_error("minimal")
        self.assertEqual(error.code, errors.IOS_BOOT_FAILED)
        self.assertIs(error.extra["store_written"], True)
        self.assertEqual(error.extra["state_file"], str(self.state_file()))
        self.assertEqual(error.extra["simulator_state"], "Shutdown")
        self.assertIn(f"autonom devices boot --platform ios --udid {UDID}", error.hint)
        self.assertIn("autonom simulator slim restore", error.hint)
        self.assertEqual(ios_slim.disabled_in(self.store()), set(MINIMAL))
        # a retry finds the store as it should be and says it wrote nothing new
        error = self.boot_error("minimal")
        self.assertIs(error.extra["store_written"], False)
        self.assertEqual(error.extra["state_file"], str(self.state_file()))
        # and once the boot works, the plain retry the hint names is enough
        self.patch_fake(simctl_fail={})
        booted, block = simulator_slim.boot(self.target, "minimal")
        self.assertTrue(booted and block["verified"])
        self.assertEqual(block["state"], "slim")

    def test_android_is_unsupported(self) -> None:
        error = self.boot_error("minimal", Target(ANDROID, "emulator-5554", "adb", {}))
        self.assertEqual(error.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(error.extra["capability"], "simulator.slim")


class CliTests(BootBase):
    def test_devices_boot_slim(self) -> None:
        self.set_device("Shutdown")
        code, payload = self.devices_boot("--slim", "minimal")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["booted"])
        self.assertFalse(payload["already_running"])
        self.assertEqual(payload["slim"]["state"], "slim")
        self.assertEqual(payload["slim"]["disabled"], len(MINIMAL))
        self.assertTrue(payload["slim"]["verified"])
        self.assertEqual(payload["target_id"], UDID)
        # already booted and matching: said, exit 0
        code, payload = self.devices_boot("--slim", "minimal")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["already_running"])
        self.assertEqual(payload["slim"]["changed"], 0)
        # already booted and different: refused
        code, payload = self.devices_boot("--slim", "optimized")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertTrue(payload["hint"])

    def test_a_failed_boot_names_the_written_store_through_the_cli(self) -> None:
        self.patch_fake(simctl_fail={"simctl bootstatus": [1, "Unable to boot device"]})
        commands = (("devices", "boot", "--slim", "minimal"),
                    ("session", "start", "--slim", "minimal"))
        for index, command in enumerate(commands):
            with self.subTest(command=command[:2]):
                self.set_device("Shutdown")
                code, payload = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                                             *command)
                self.assertNotEqual(code, 0)
                self.assertEqual(payload["error_code"], errors.IOS_BOOT_FAILED)
                self.assertIs(payload["store_written"], index == 0)
                self.assertEqual(payload["state_file"], str(self.state_file()))
                self.assertEqual(payload["simulator_state"], "Shutdown")
                self.assertIn("autonom devices boot", payload["hint"])
                self.assertIn("autonom simulator slim restore", payload["hint"])
        code, payload = self.run_cli("session", "status")
        self.assertNotEqual(payload.get("ok"), True, payload)

    def test_devices_boot_without_slim_changes_nothing(self) -> None:
        self.set_device("Shutdown")
        code, payload = self.devices_boot()
        self.assertEqual(code, 0, payload)
        self.assertNotIn("slim", payload)
        self.assertFalse(self.store_path().exists())
        self.assertFalse(self.state_file().exists())
        self.assertEqual(self.launchctl_calls(), [])

    def test_devices_boot_slim_on_android_is_unsupported(self) -> None:
        code, payload = self.run_cli("devices", "boot", "--avd", "Pixel_9",
                                     "--slim", "minimal")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.UNSUPPORTED_CAPABILITY)
        self.assertTrue(payload["hint"])

    def test_session_start_slim(self) -> None:
        self.set_device("Shutdown")
        code, payload = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                                     "session", "start", "--slim", "minimal")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["booted"])
        self.assertEqual(payload["slim"]["state"], "slim")
        self.assertTrue(payload["slim"]["verified"])
        self.assertEqual(self.launchd_disabled(), set(MINIMAL))
        self.run_cli("session", "stop")
        # stopping the session does not undo the slimming
        self.assertEqual(self.launchd_disabled(), set(MINIMAL))
        self.assertTrue(self.state_file().exists())

    def test_session_start_slim_refusal_leaves_no_session(self) -> None:
        self.set_device("Booted")
        code, payload = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                                     "session", "start", "--slim", "minimal")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.SIMULATOR_MUST_BE_SHUTDOWN)
        code, payload = self.run_cli("session", "status")
        self.assertNotEqual(payload.get("ok"), True, payload)

    def test_session_start_without_slim_changes_nothing(self) -> None:
        self.set_device("Shutdown")
        code, payload = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                                     "session", "start")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("slim", payload)
        self.assertFalse(self.store_path().exists())
        self.assertEqual(self.launchctl_calls(), [])
        self.run_cli("session", "stop")


if __name__ == "__main__":
    unittest.main()
