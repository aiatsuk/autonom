from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import device_state, errors  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FAKE_ADB = str(ROOT / "tests/fakes/fake_adb.py")
FAKE_SIMCTL = str(ROOT / "tests/fakes/fake_simctl.py")
ANDROID_TARGET = Target("android", "emulator-5554", "/fake/adb", {"serial": "emulator-5554"})
ANDROID_EMU = Target("android", "emulator-5554", FAKE_ADB, {"serial": "emulator-5554"})
ANDROID_PHONE = Target("android", "R58M123ABC", FAKE_ADB, {"serial": "R58M123ABC"})
IOS_TARGET = Target("ios", "UDID-1234", FAKE_SIMCTL, {"udid": "UDID-1234"})
# A made-up emulator serial for the newer tests; only the fake adb ever sees it.
CANVAS_EMU = Target("android", "emulator-5580", FAKE_ADB, {"serial": "emulator-5580"})


class PathContainmentTests(unittest.TestCase):
    """INV-09 / CAP-IOSDIAG-005 — file operations stay inside the app container."""

    def test_relative_paths_are_normalized_and_accepted(self) -> None:
        self.assertEqual(device_state.safe_relative("Documents/state.json"), "Documents/state.json")
        self.assertEqual(device_state.safe_relative("./Documents/../Library/x"), "Library/x")

    def test_escapes_are_refused(self) -> None:
        for candidate in (
            "../../../../etc/passwd",
            "/etc/hosts",
            "~/.ssh/id_rsa",
            "..",
            "Documents/../../../secrets",
            "",
        ):
            with self.subTest(path=candidate):
                with self.assertRaises(errors.AutonomError) as caught:
                    device_state.safe_relative(candidate)
                self.assertEqual(caught.exception.code, errors.PATH_OUTSIDE_CONTAINER)


class UrlValidationTests(unittest.TestCase):
    """CAP-IOSDIAG-003 — malformed URLs are rejected before any backend call."""

    def test_malformed_urls_are_rejected_locally(self) -> None:
        for candidate in ("not a url", "", "://missing-scheme", "just/a/path"):
            with self.subTest(url=candidate):
                with self.assertRaises(errors.AutonomError) as caught:
                    device_state.open_url(IOS_TARGET, candidate)
                self.assertEqual(caught.exception.code, errors.INVALID_URL)

    def test_wellformed_schemes_reach_the_backend(self) -> None:
        # The fake accepts anything; getting through means validation passed.
        for candidate in ("myapp://profile/42", "https://example.com"):
            with self.subTest(url=candidate):
                device_state.open_url(IOS_TARGET, candidate)

    def test_a_missing_backend_binary_is_still_machine_readable(self) -> None:
        broken = Target("ios", "UDID-1234", "/nonexistent/xcrun", {"udid": "UDID-1234"})
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.open_url(broken, "https://example.com")
        self.assertEqual(caught.exception.code, errors.SIMCTL_NOT_FOUND)


class CoordinateTests(unittest.TestCase):
    """CAP-IOSDIAG-004 — coordinates are validated before dispatch."""

    def test_valid_coordinates_parse(self) -> None:
        self.assertEqual(
            device_state.parse_coordinates("55.751244,37.618423"), (55.751244, 37.618423)
        )
        self.assertEqual(device_state.parse_coordinates(" -33.9 , 18.4 "), (-33.9, 18.4))

    def test_invalid_coordinates_are_refused(self) -> None:
        for candidate in ("999,999", "abc,def", "1", "1,2,3", ""):
            with self.subTest(value=candidate):
                with self.assertRaises(errors.AutonomError) as caught:
                    device_state.parse_coordinates(candidate)
                self.assertEqual(caught.exception.code, errors.INVALID_COORDINATES)


class AndroidLocationTests(EnvSandboxMixin, unittest.TestCase):
    """CAP-IOSDIAG-004 (Android) — the emulator console path for `geo fix`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "log.jsonl"
        state = Path(self.tmp.name) / "state.json"
        state.write_text("{}", encoding="utf-8")
        self.set_env(AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_FAKE_STATE=str(state))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_set_location_uses_geo_fix_with_longitude_first(self) -> None:
        detail = device_state.set_location(ANDROID_EMU, "55.751244,37.618423")
        self.assertEqual(detail["via"], "emulator_console")
        geo = [argv for argv in self._calls() if argv[-5:-2] == ["emu", "geo", "fix"]]
        self.assertEqual(len(geo), 1)
        # lon before lat — the whole point of the guard.
        self.assertEqual(geo[0][-2:], ["37.6184230", "55.7512440"])
        self.assertEqual(geo[0][:2], ["-s", "emulator-5554"])

    def test_physical_device_is_refused_before_any_adb_call(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.set_location(ANDROID_PHONE, "10,20")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("mock-location", caught.exception.hint)
        self.assertEqual(self._calls(), [], "must refuse before touching adb")

    def test_get_location_reads_the_fused_fix(self) -> None:
        detail = device_state.get_location(ANDROID_EMU)
        self.assertEqual(detail["provider"], "fused")
        self.assertEqual((detail["latitude"], detail["longitude"]), (55.751244, 37.618423))
        self.assertEqual(detail["accuracy_m"], 5.0)

    def test_get_location_reports_no_fix_as_null(self) -> None:
        Path(os.environ["AUTONOM_FAKE_STATE"]).write_text(
            json.dumps({"dumpsys_location": "    network provider:\n      last location=null\n"}),
            encoding="utf-8",
        )
        detail = device_state.get_location(ANDROID_EMU)
        self.assertIsNone(detail["latitude"])
        self.assertIn("no last known location", detail["note"])

    def test_get_location_is_refused_on_ios(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.get_location(IOS_TARGET)
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("set", caught.exception.hint)

    def test_clear_on_emulator_is_an_honest_refusal(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.clear_location(ANDROID_EMU)
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("location set", caught.exception.hint)

    def test_unreachable_console_surfaces_a_machine_readable_error(self) -> None:
        Path(os.environ["AUTONOM_FAKE_STATE"]).write_text(
            json.dumps({"geo_fix_fails": True}), encoding="utf-8"
        )
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.set_location(ANDROID_EMU, "1,2")
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)


DUMPSYS_PACKAGE = """Packages:
  Package [com.example.app] (4a1b2c):
    declared permissions:
      com.example.app.permission.C2D_MESSAGE: prot=signature
    requested permissions:
      android.permission.INTERNET
      android.permission.RECORD_AUDIO
      android.permission.ACCESS_BACKGROUND_LOCATION: restricted=true
      android.permission.CAMERA
      android.permission.READ_CONTACTS
    install permissions:
      android.permission.INTERNET: granted=true
    User 0: ceDataInode=1 installed=true
      gids=[3003]
      runtime permissions:
        android.permission.CAMERA: granted=true, flags=[ USER_SET ]
        android.permission.RECORD_AUDIO: granted=false, flags=[ ]
        android.permission.ACCESS_BACKGROUND_LOCATION: granted=false, flags=[ ]
      disabledComponents:
        com.example.app.Hidden
    User 10: ceDataInode=2 installed=true
      runtime permissions:
        android.permission.CAMERA: granted=false, flags=[ ]
        android.permission.POST_NOTIFICATIONS: granted=true, flags=[ ]
"""


class RuntimePermissionStateTests(EnvSandboxMixin, unittest.TestCase):
    """The Canvas permission list: requested runtime permissions and granted state."""

    def test_states_follow_the_requested_order_and_the_first_user(self) -> None:
        self.assertEqual(device_state.runtime_permission_states(DUMPSYS_PACKAGE), [
            {"name": "android.permission.RECORD_AUDIO", "alias": "microphone",
             "granted": False},
            {"name": "android.permission.ACCESS_BACKGROUND_LOCATION", "alias": None,
             "granted": False},
            {"name": "android.permission.CAMERA", "alias": "camera", "granted": True},
            # requested and a known runtime permission, but in no runtime section
            {"name": "android.permission.READ_CONTACTS", "alias": "contacts",
             "granted": None},
            # in a runtime section but not in the requested list: kept, at the end
            {"name": "android.permission.POST_NOTIFICATIONS", "alias": "notifications",
             "granted": True},
        ])

    def test_install_time_and_declared_permissions_are_left_out(self) -> None:
        names = [entry["name"] for entry in
                 device_state.runtime_permission_states(DUMPSYS_PACKAGE)]
        self.assertNotIn("android.permission.INTERNET", names)
        self.assertNotIn("com.example.app.permission.C2D_MESSAGE", names)

    def test_empty_or_partial_output(self) -> None:
        self.assertEqual(device_state.runtime_permission_states(""), [])
        self.assertEqual(device_state.runtime_permission_states(
            "    requested permissions:\n      android.permission.CAMERA\n"),
            [{"name": "android.permission.CAMERA", "alias": "camera", "granted": None}])
        self.assertEqual(device_state.runtime_permission_states(
            "      runtime permissions:\n        android.permission.CAMERA: granted=true\n"),
            [{"name": "android.permission.CAMERA", "alias": "camera", "granted": True}])

    def test_the_existing_granted_list_is_unchanged(self) -> None:
        self.assertEqual(device_state.granted_runtime_permissions(DUMPSYS_PACKAGE),
                         ["android.permission.CAMERA",
                          "android.permission.POST_NOTIFICATIONS"])

    def test_alias_lookup(self) -> None:
        self.assertEqual(device_state.android_permission_alias(
            "android.permission.ACCESS_COARSE_LOCATION"), "location")
        self.assertIsNone(device_state.android_permission_alias("android.permission.NOPE"))

    def _fake(self, output: str, code: int = 0) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / "state.json"
        log = Path(tmp.name) / "log.jsonl"
        state.write_text(json.dumps({"fail": {
            "-s emulator-5580 shell dumpsys package com.example.app": [code, output]}}),
            encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(state), AUTONOM_FAKE_LOG=str(log))
        return log

    def test_an_installed_app_is_read_with_one_dumpsys_call(self) -> None:
        log = self._fake(DUMPSYS_PACKAGE)
        entries = device_state.app_runtime_permissions(CANVAS_EMU, "com.example.app")
        self.assertEqual(entries[2], {"name": "android.permission.CAMERA", "alias": "camera",
                                      "granted": True})
        calls = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
        self.assertEqual(calls, [["-s", "emulator-5580", "shell", "dumpsys", "package",
                                  "com.example.app"]])

    def test_a_missing_app_is_app_not_installed(self) -> None:
        self._fake("Unable to find package: com.example.app\n")
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.app_runtime_permissions(CANVAS_EMU, "com.example.app")
        self.assertEqual(caught.exception.code, errors.APP_NOT_INSTALLED)

    def test_an_adb_failure_is_a_device_error_not_app_not_installed(self) -> None:
        for output in ("error: device offline\n", "error: device unauthorized.\n", ""):
            with self.subTest(output=output):
                self._fake(output, code=1)
                with self.assertRaises(errors.AutonomError) as caught:
                    device_state.app_runtime_permissions(CANVAS_EMU, "com.example.app")
                self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
                self.assertNotEqual(caught.exception.code, errors.APP_NOT_INSTALLED)
                if output:
                    self.assertIn(output.strip(), str(caught.exception))

    def test_ios_cannot_read_permissions(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.app_runtime_permissions(IOS_TARGET, "com.example.app")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("simctl", caught.exception.hint)


class PlatformRefusalTests(unittest.TestCase):
    """A verb with no equivalent must refuse, never silently no-op."""

    def test_crash_show_is_refused_on_android_and_names_the_alternative(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.crash_show(ANDROID_TARGET, "whatever")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("crash list", caught.exception.hint)

    def test_android_permissions_require_a_package(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.permissions(ANDROID_TARGET, "grant", "android.permission.CAMERA", None)
        self.assertEqual(caught.exception.code, errors.UNKNOWN_PRIVACY_SERVICE)

    def test_unknown_ios_privacy_service_lists_the_valid_ones(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.permissions(IOS_TARGET, "grant", "telepathy", "com.example.app")
        self.assertEqual(caught.exception.code, errors.UNKNOWN_PRIVACY_SERVICE)
        self.assertIn("photos", caught.exception.hint)


if __name__ == "__main__":
    unittest.main()
