from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
SESSION_V1 = ROOT / "tests/fixtures/session_v1.json"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, platform as platform_mod, session as session_mod  # noqa: E402

UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"


def namespace(**kwargs):
    base = {
        "platform": None, "target": None, "serial": None, "udid": None,
        "adb": str(FAKE_ADB), "simctl": str(FAKE_SIMCTL),
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


class TargetResolutionTests(unittest.TestCase):
    """CAP-PLAT-001 — one precedence order, identical on both platforms."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name) / "state.json"
        state.write_text("{}", encoding="utf-8")
        os.environ["AUTONOM_FAKE_STATE"] = str(state)

    def tearDown(self) -> None:
        os.environ.pop("AUTONOM_FAKE_STATE", None)
        self.tmp.cleanup()

    def test_explicit_platform_and_target_win_over_session(self) -> None:
        session = {"platform": "android", "target_id": "emulator-5554"}
        target = platform_mod.resolve(
            namespace(platform="ios", target=UDID), session_record=session
        )
        self.assertEqual(target.platform, "ios")
        self.assertEqual(target.target_id, UDID)

    def test_serial_implies_android_and_keeps_the_alias(self) -> None:
        target = platform_mod.resolve(namespace(serial="emulator-5554"))
        self.assertEqual(target.platform, "android")
        self.assertEqual(target.target_id, "emulator-5554")
        self.assertEqual(target.serial, "emulator-5554")
        self.assertEqual(target.identity()["serial"], "emulator-5554")

    def test_udid_implies_ios_and_emits_no_serial(self) -> None:
        target = platform_mod.resolve(namespace(udid=UDID))
        self.assertEqual(target.platform, "ios")
        self.assertNotIn("serial", target.identity())

    def test_session_record_supplies_the_target(self) -> None:
        session = {"platform": "ios", "target_id": UDID}
        target = platform_mod.resolve(namespace(), session_record=session)
        self.assertEqual((target.platform, target.target_id), ("ios", UDID))

    def test_v1_session_record_without_target_id_still_resolves(self) -> None:
        legacy = session_mod.upgrade(json.loads(SESSION_V1.read_text(encoding="utf-8")))
        target = platform_mod.resolve(namespace(), session_record=legacy)
        self.assertEqual(target.platform, "android")
        self.assertEqual(target.target_id, "emulator-5554")

    def test_conflicting_aliases_are_rejected_before_any_backend_call(self) -> None:
        for kwargs in (
            {"platform": "ios", "serial": "emulator-5554"},
            {"platform": "android", "udid": UDID},
            {"serial": "emulator-5554", "udid": UDID},
            {"target": "emulator-5554", "udid": UDID},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(errors.AutonomError) as caught:
                    platform_mod.resolve(namespace(**kwargs))
                self.assertEqual(caught.exception.code, errors.CONFLICTING_TARGET_FLAGS)

    def test_unknown_platform_is_rejected(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            platform_mod.resolve(namespace(platform="windows"))
        self.assertEqual(caught.exception.code, errors.UNKNOWN_PLATFORM)


class SessionSchemaTests(unittest.TestCase):
    """CAP-PLAT-003 / VER-002 — v2 is additive and v1 records are never rewritten."""

    def test_v2_record_keeps_every_v1_android_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = session_mod.start_session(
                "adb", serial="emulator-5554", app_id="com.example", cwd=Path(tmp)
            )
            for key in ("session_id", "platform", "serial", "app_id", "started_at",
                        "artifacts_dir", "adb"):
                self.assertIn(key, record, f"0.4.0 key {key} disappeared")
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["target_id"], "emulator-5554")

    def test_ios_record_has_no_serial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = session_mod.start_session(
                "xcrun", platform="ios", target_id=UDID, cwd=Path(tmp)
            )
            self.assertNotIn("serial", record)
            self.assertEqual(record["aliases"], {"udid": UDID})

    def test_v1_record_is_readable_and_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".autonom").mkdir()
            current = cwd / ".autonom/current.json"
            shutil.copyfile(SESSION_V1, current)
            before = hashlib.sha256(current.read_bytes()).hexdigest()

            loaded = session_mod.load_current(cwd)
            self.assertEqual(loaded["platform"], "android")
            self.assertEqual(loaded["target_id"], "emulator-5554")
            self.assertEqual(loaded["aliases"], {"serial": "emulator-5554"})

            after = hashlib.sha256(current.read_bytes()).hexdigest()
            self.assertEqual(before, after, "reading a v1 record must not rewrite it")

    def test_teardown_isolates_failures(self) -> None:
        def boom() -> None:
            raise RuntimeError("companion refused to disconnect")

        results = session_mod.run_teardown([("ok", lambda: "done"), ("bad", boom)])
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertIn("companion", results[1]["error"])


class DevicesDegradationTests(unittest.TestCase):
    """CAP-PLAT-002 / DEC-011 — degrade per platform, fail only when asked explicitly."""

    def _run(self, *args: str, path: str | None = None):
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = path
        env.pop("AUTONOM_ADB", None)
        env.pop("AUTONOM_SIMCTL", None)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_both_platforms_are_listed(self) -> None:
        result = self._run("devices", "--adb", str(FAKE_ADB), "--simctl", str(FAKE_SIMCTL))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        platforms = {device["platform"] for device in payload["devices"]}
        self.assertEqual(platforms, {"android", "ios"})
        self.assertEqual(payload["warnings"], [])

    def test_ios_entry_carries_a_readable_runtime(self) -> None:
        result = self._run("devices", "--platform", "ios", "--simctl", str(FAKE_SIMCTL))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["devices"][0]["runtime"], "iOS 26.0")

    def test_android_entry_keeps_serial_and_gains_target_id(self) -> None:
        result = self._run("devices", "--platform", "android", "--adb", str(FAKE_ADB))
        device = json.loads(result.stdout)["devices"][0]
        self.assertEqual(device["serial"], "emulator-5554")
        self.assertEqual(device["target_id"], "emulator-5554")

    def test_missing_adb_degrades_to_a_warning(self) -> None:
        result = self._run("devices", "--simctl", str(FAKE_SIMCTL), path="/usr/bin:/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        codes = {warning["error_code"] for warning in payload["warnings"]}
        self.assertIn("adb_not_found", codes)
        self.assertTrue(all(device["platform"] == "ios" for device in payload["devices"]))

    def test_explicitly_requesting_a_missing_platform_is_an_error(self) -> None:
        result = self._run("devices", "--platform", "android", path="/usr/bin:/bin")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"], "adb_not_found")

    def test_bare_host_returns_an_empty_list_with_two_warnings(self) -> None:
        # A truly empty PATH: on macOS `xcrun` lives in /usr/bin, so trimming to
        # /usr/bin:/bin would still find the iOS toolchain.
        with tempfile.TemporaryDirectory() as empty:
            result = self._run("devices", path=empty)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["devices"], [])
        self.assertEqual(len(payload["warnings"]), 2)
        self.assertIn("doctor", payload["next_action"])


class TargetFlagPositionTests(unittest.TestCase):
    """The plan's examples put target flags both before and after the subcommand."""

    def _devices(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_flags_are_accepted_in_either_position(self) -> None:
        after = self._devices("devices", "--platform", "ios", "--simctl", str(FAKE_SIMCTL))
        before = self._devices("--platform", "ios", "--simctl", str(FAKE_SIMCTL), "devices")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertEqual(json.loads(after.stdout)["devices"], json.loads(before.stdout)["devices"])


if __name__ == "__main__":
    unittest.main()
