"""`devices boot` / `devices shutdown` and the normalized inventory flags.

Everything runs against the fake tools, so what is asserted is the argv that
was actually executed and the state the fakes mutated — not what the CLI
merely claims.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_EMULATOR = ROOT / "tests/fakes/fake_emulator.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import emulator as emulator_mod, errors  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

class LifecycleBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(root / "home"),
        )
        # Snapshotted after set_env so subprocesses see exactly what
        # in-process code sees — one source of truth, no parallel dict.
        self.env = dict(os.environ)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_state(self, **kwargs) -> None:
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def argv_log(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line)
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, env=self.env, timeout=60,
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        return completed.returncode, json.loads(stream)


class BootAvdTests(LifecycleBase):
    def test_boot_waits_for_boot_completed_and_reports_the_new_serial(self) -> None:
        self.set_state(avds=["Pixel_9"], devices=[["emulator-5554", "device", ""]])
        detail = emulator_mod.boot_avd(
            str(FAKE_EMULATOR), str(FAKE_ADB), "Pixel_9", timeout=15
        )
        self.assertTrue(detail["booted"])
        self.assertEqual(detail["serial"], "emulator-5556")
        spawned = [entry for entry in self.argv_log()
                   if entry["tool"] == "emulator" and entry["argv"][:1] == ["-avd"]]
        self.assertEqual(spawned[0]["argv"], ["-avd", "Pixel_9"])
        polled = [entry for entry in self.argv_log()
                  if entry["tool"] == "adb" and "sys.boot_completed" in entry["argv"]]
        self.assertTrue(polled, "boot must be proven by sys.boot_completed, not assumed")

    def test_unknown_avd_fails_with_the_available_list(self) -> None:
        self.set_state(avds=["Pixel_9"])
        with self.assertRaises(errors.AutonomError) as ctx:
            emulator_mod.boot_avd(str(FAKE_EMULATOR), str(FAKE_ADB), "Nope", timeout=5)
        self.assertEqual(ctx.exception.code, errors.AVD_NOT_FOUND)
        self.assertIn("Pixel_9", ctx.exception.hint)

    def test_no_wait_returns_after_spawn(self) -> None:
        self.set_state(avds=["Pixel_9"])
        detail = emulator_mod.boot_avd(
            str(FAKE_EMULATOR), str(FAKE_ADB), "Pixel_9", wait=False
        )
        self.assertFalse(detail["booted"])
        self.assertIn("pid", detail)

    def test_cli_refuses_avd_plus_target(self) -> None:
        code, payload = self.run_cli(
            "devices", "boot", "--avd", "X", "--serial", "emulator-5554",
            "--adb", str(FAKE_ADB), "--emulator", str(FAKE_EMULATOR),
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.CONFLICTING_TARGET_FLAGS)

    def test_cli_requires_avd_or_target(self) -> None:
        code, payload = self.run_cli(
            "devices", "boot", "--adb", str(FAKE_ADB), "--emulator", str(FAKE_EMULATOR)
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.NO_TARGET)

    def test_cli_running_serial_is_a_noop(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]])
        code, payload = self.run_cli(
            "devices", "boot", "--serial", "emulator-5554", "--adb", str(FAKE_ADB)
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["already_running"])

    def test_cli_stopped_serial_points_at_avd(self) -> None:
        self.set_state(devices=[])
        code, payload = self.run_cli(
            "devices", "boot", "--serial", "emulator-5599", "--adb", str(FAKE_ADB)
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.AVD_REQUIRED)

    def test_cli_boots_a_simulator(self) -> None:
        code, payload = self.run_cli(
            "devices", "boot", "--udid", UDID, "--simctl", str(FAKE_SIMCTL)
        )
        self.assertEqual(code, 0)
        self.assertIn("booted", payload)


class ShutdownTests(LifecycleBase):
    def test_kill_emulator_and_verify_it_is_gone(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]])
        detail = emulator_mod.kill_emulator(str(FAKE_ADB), "emulator-5554")
        self.assertTrue(detail["stopped"])
        self.assertTrue(detail["gone"])
        killed = [entry for entry in self.argv_log()
                  if entry["tool"] == "adb" and entry["argv"][-2:] == ["emu", "kill"]]
        self.assertEqual(killed[0]["argv"][:2], ["-s", "emulator-5554"])

    def test_refuses_physical_hardware(self) -> None:
        with self.assertRaises(errors.AutonomError) as ctx:
            emulator_mod.kill_emulator(str(FAKE_ADB), "R58M123ABC")
        self.assertEqual(ctx.exception.code, errors.EMULATOR_ONLY)
        adb_calls = [entry for entry in self.argv_log() if entry["tool"] == "adb"]
        self.assertEqual(adb_calls, [], "refusal must happen before any adb call")

    def test_cli_shutdown_android(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]])
        code, payload = self.run_cli(
            "devices", "shutdown", "--serial", "emulator-5554", "--adb", str(FAKE_ADB)
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["stopped"])

    def test_cli_shutdown_ios(self) -> None:
        code, payload = self.run_cli(
            "devices", "shutdown", "--udid", UDID, "--simctl", str(FAKE_SIMCTL)
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["stopped"])


class InventoryTests(LifecycleBase):
    def test_running_flag_is_normalized_and_avds_are_listed(self) -> None:
        self.set_state(
            devices=[["emulator-5554", "device", ""], ["emulator-5560", "offline", ""]],
            avds=["Pixel_9", "Tablet"],
        )
        self.env["AUTONOM_EMULATOR"] = str(FAKE_EMULATOR)
        code, payload = self.run_cli(
            "devices", "--platform", "android", "--adb", str(FAKE_ADB)
        )
        self.assertEqual(code, 0)
        running = {d["target_id"]: d["running"] for d in payload["devices"]}
        self.assertEqual(running, {"emulator-5554": True, "emulator-5560": False})
        self.assertEqual(payload["avds"], ["Pixel_9", "Tablet"])

    def test_avd_profiles_come_from_the_ini_files(self) -> None:
        """Names alone cannot tell the phone from the tablet; config.ini can."""
        home = Path(self.tmp.name) / "avd"
        (home / "Pixel_9.avd").mkdir(parents=True)
        (home / "Pixel_9.ini").write_text(
            f"avd.ini.encoding=UTF-8\npath={home / 'Pixel_9.avd'}\ntarget=android-35\n",
            encoding="utf-8")
        (home / "Pixel_9.avd" / "config.ini").write_text(
            "hw.device.name=pixel_9\nhw.lcd.width=1080\nhw.lcd.height=2424\n"
            "hw.lcd.density=420\nabi.type=arm64-v8a\n", encoding="utf-8")
        self.set_state(devices=[], avds=["Pixel_9", "Ghost"])
        self.env["AUTONOM_EMULATOR"] = str(FAKE_EMULATOR)
        self.env["ANDROID_AVD_HOME"] = str(home)
        code, payload = self.run_cli(
            "devices", "--platform", "android", "--adb", str(FAKE_ADB)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["avds"], ["Pixel_9", "Ghost"])
        pixel, ghost = payload["avd_profiles"]
        self.assertEqual(pixel["device"], "pixel_9")
        self.assertEqual(pixel["screen"], {"width": 1080, "height": 2424, "density": 420})
        self.assertEqual(pixel["api"], 35)
        self.assertEqual(pixel["abi"], "arm64-v8a")
        # An AVD with no readable ini reports nulls, never a guess.
        self.assertEqual(ghost, {"name": "Ghost", "device": None, "screen": None,
                                 "api": None, "abi": None, "path": None})

    def test_running_emulator_names_the_avd_it_booted_from(self) -> None:
        self.set_state(
            devices=[["emulator-5554", "device", ""], ["R58M123ABC", "device", ""]],
            avd_names={"emulator-5554": "Pixel_9"},
        )
        code, payload = self.run_cli(
            "devices", "--platform", "android", "--adb", str(FAKE_ADB)
        )
        self.assertEqual(code, 0)
        by_id = {d["target_id"]: d for d in payload["devices"]}
        self.assertEqual(by_id["emulator-5554"]["avd"], "Pixel_9")
        self.assertNotIn("avd", by_id["R58M123ABC"])
        asked = [entry["argv"] for entry in self.argv_log()
                 if entry["tool"] == "adb" and entry["argv"][-3:] == ["emu", "avd", "name"]]
        self.assertEqual(asked, [["-s", "emulator-5554", "emu", "avd", "name"]],
                         "hardware must never be asked; it has no console")

    def test_running_avd_name_skips_hardware_without_an_adb_call(self) -> None:
        self.assertIsNone(emulator_mod.running_avd_name(str(FAKE_ADB), "R58M123ABC"))
        self.assertEqual(self.argv_log(), [])

    def test_missing_emulator_binary_just_omits_avds(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]])
        # Force discovery to fail regardless of the host's real SDK: a bogus
        # override, no SDK-root env, and a HOME with no ~/Library/Android/sdk.
        self.env["AUTONOM_EMULATOR"] = str(Path(self.tmp.name) / "nonexistent-emulator")
        self.env["HOME"] = str(Path(self.tmp.name) / "empty-home")
        self.env["PATH"] = "/usr/bin:/bin"
        for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            self.env.pop(var, None)
        code, payload = self.run_cli(
            "devices", "--platform", "android", "--adb", str(FAKE_ADB)
        )
        self.assertEqual(code, 0)
        self.assertNotIn("avds", payload)


if __name__ == "__main__":
    unittest.main()
