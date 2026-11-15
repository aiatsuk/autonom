"""What a slimmed simulator tells the verbs that need a switched-off service:
`media add`, `open <http url>`, `simulator push send`, and the doctor row.

Hermetic like test_ios_slim.py (fake `simctl`, temporary override store).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import device_state, doctor, errors, ios_simctl, ios_slim  # noqa: E402
from autonom_lib import simulator  # noqa: E402

try:
    from test_ios_slim import FAKE_SIMCTL, UDID, SlimBase  # noqa: E402
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.test_ios_slim import FAKE_SIMCTL, UDID, SlimBase  # noqa: E402

ASSETSD, SWCD, APSD = "com.apple.assetsd", "com.apple.swcd", "com.apple.apsd"
ADDMEDIA_FAILS = {"simctl addmedia": [1, "PHPhotosErrorDomain error 3301"]}


class GuardBase(SlimBase):
    def setUp(self) -> None:
        super().setUp()
        self.set_device("Booted")
        self.photo = self.root / "photo.png"
        self.photo.write_bytes(b"\x89PNG")

    def ios_cli(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID, *argv)

    def break_store(self) -> None:
        self.store_path().parent.mkdir(parents=True, exist_ok=True)
        self.store_path().write_bytes(b"not a plist")


class LookupTests(GuardBase):
    def test_lookup_reads_the_store_only(self) -> None:
        self.assertIs(ios_slim.service_disabled(UDID, ASSETSD), False)  # no store yet
        self.write_store({ASSETSD: True, SWCD: False})
        self.assertIs(ios_slim.service_disabled(UDID, ASSETSD), True)
        self.assertIs(ios_slim.service_disabled(UDID, SWCD), False)
        self.assertIs(ios_slim.service_disabled(UDID, APSD), False)
        self.assertEqual(self.calls(), [], "no device call")

    def test_any_lookup_trouble_is_unknown(self) -> None:
        self.break_store()
        self.assertIsNone(ios_slim.service_disabled(UDID, ASSETSD))
        self.assertIsNone(ios_slim.service_disabled("booted", ASSETSD))
        self.assertIsNone(ios_slim.service_disabled("", ASSETSD))
        self.assertIsNone(ios_slim.service_warning(UDID, SWCD, "c", "e"))
        with mock.patch.object(ios_slim, "read_store", side_effect=RuntimeError("x")):
            self.assertIsNone(ios_slim.service_disabled(UDID, ASSETSD))

    def test_warning_shape(self) -> None:
        self.write_store({SWCD: True})
        warning = ios_slim.service_warning(UDID, SWCD, "slim_universal_links_off", "x")
        self.assertEqual(set(warning), {"code", "error", "hint", "service"})
        self.assertIn("autonom simulator slim enable --value ids=com.apple.swcd",
                      warning["hint"])


class MediaTests(GuardBase):
    def test_failed_import_with_assetsd_off_names_the_service(self) -> None:
        self.write_store({ASSETSD: True})
        self.patch_fake(simctl_fail=ADDMEDIA_FAILS)
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.add_media(self.target, self.photo)
        error = caught.exception
        self.assertEqual(error.code, errors.SIMULATOR_SERVICE_DISABLED)
        self.assertEqual(error.code, "simulator_service_disabled")
        self.assertEqual(error.extra["service"], ASSETSD)
        self.assertIn("autonom simulator slim enable --value ids=com.apple.assetsd",
                      error.hint)
        self.assertIn("3301", error.extra["backend_error"])
        self.assertTrue(error.message)

    def test_failed_import_with_assetsd_on_keeps_the_backend_error(self) -> None:
        self.write_store({SWCD: True})
        self.patch_fake(simctl_fail=ADDMEDIA_FAILS)
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.add_media(self.target, self.photo)
        self.assertNotEqual(caught.exception.code, errors.SIMULATOR_SERVICE_DISABLED)

    def test_failed_import_with_an_unreadable_store_keeps_the_backend_error(self) -> None:
        self.break_store()
        self.patch_fake(simctl_fail=ADDMEDIA_FAILS)
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.add_media(self.target, self.photo)
        self.assertNotEqual(caught.exception.code, errors.SIMULATOR_SERVICE_DISABLED)

    def test_working_import_is_untouched_even_with_assetsd_off(self) -> None:
        self.write_store({ASSETSD: True})
        self.assertEqual(device_state.add_media(self.target, self.photo),
                         {"added": str(self.photo)})

    def test_missing_file_is_still_its_own_error(self) -> None:
        self.write_store({ASSETSD: True})
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.add_media(self.target, self.root / "nope.png")
        self.assertEqual(caught.exception.code, errors.INSTALL_PATH_NOT_FOUND)

    def test_cli_payload_has_code_message_and_hint(self) -> None:
        self.write_store({ASSETSD: True})
        self.patch_fake(simctl_fail=ADDMEDIA_FAILS)
        code, payload = self.ios_cli("media", "add", str(self.photo))
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], "simulator_service_disabled")
        self.assertTrue(payload["error"] and payload["hint"])
        self.assertEqual(payload["service"], ASSETSD)


class OpenTests(GuardBase):
    def test_http_url_with_swcd_off_opens_with_a_warning(self) -> None:
        self.write_store({SWCD: True})
        code, payload = self.ios_cli("open", "https://example.com/item/1")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["opened"], "https://example.com/item/1")
        self.assertEqual([item["code"] for item in payload["warnings"]],
                         ["slim_universal_links_off"])
        self.assertEqual(payload["warnings"][0]["service"], SWCD)
        self.assertIn(["simctl", "openurl", UDID, "https://example.com/item/1"],
                      self.calls())

    def test_no_warning_when_on_unknown_or_a_custom_scheme(self) -> None:
        code, payload = self.ios_cli("open", "https://example.com/")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("warnings", payload)
        self.write_store({SWCD: True})
        code, payload = self.ios_cli("open", "myapp://item/1")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("warnings", payload)
        self.break_store()
        code, payload = self.ios_cli("open", "https://example.com/")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("warnings", payload)


class PushTests(GuardBase):
    VALUES = {"app_id": "com.example.app", "payload": {"aps": {"alert": "hi"}}}

    def test_push_with_apsd_off_is_sent_with_a_warning(self) -> None:
        self.write_store({APSD: True})
        result = simulator.apply(self.target, "push", "send", dict(self.VALUES))
        self.assertEqual([item["code"] for item in result["warnings"]], ["slim_push_off"])
        self.assertEqual(result["warnings"][0]["service"], APSD)
        self.assertTrue(result["warnings"][0]["hint"])
        self.assertTrue(any(argv[1:2] == ["push"] for argv in self.calls()))

    def test_a_composed_push_with_apsd_off_keeps_the_warning(self) -> None:
        self.write_store({APSD: True})
        result = simulator.apply(self.target, "push", "send",
                                 {"app_id": "com.example.app", "title": "hi"}, session={})
        self.assertTrue(result["composed"])
        self.assertEqual([item["code"] for item in result["warnings"]], ["slim_push_off"])

    def test_push_with_apsd_on_or_unknown_has_no_warning(self) -> None:
        self.assertNotIn("warnings",
                         simulator.apply(self.target, "push", "send", dict(self.VALUES)))
        self.break_store()
        self.assertNotIn("warnings",
                         simulator.apply(self.target, "push", "send", dict(self.VALUES)))


class DoctorTests(GuardBase):
    def test_row_per_booted_simulator_from_the_store(self) -> None:
        self.assertEqual(ios_slim.doctor_row(UDID),
                         {"udid": UDID, "slim_state": "stock", "disabled": 0})
        minimal = ios_slim.resolve({"preset": "minimal"})
        self.write_store({label: True for label in minimal})
        self.assertEqual(ios_slim.doctor_row(UDID),
                         {"udid": UDID, "slim_state": "slim", "disabled": len(minimal)})
        rows = doctor._booted_simulators({"state": "ok", "path": str(FAKE_SIMCTL)})
        self.assertEqual(rows, [{"udid": UDID, "slim_state": "slim",
                                 "disabled": len(minimal)}])
        self.assertEqual(self.launchctl_calls(), [], "no spawn into the simulator")

    def test_shutdown_simulators_and_broken_tools_give_no_rows(self) -> None:
        self.set_device("Shutdown")
        self.assertEqual(doctor._booted_simulators({"state": "ok",
                                                    "path": str(FAKE_SIMCTL)}), [])
        self.assertEqual(doctor._booted_simulators({"state": "missing", "path": None}), [])
        self.assertEqual(ios_simctl.booted_udids("/nonexistent/xcrun"), [])
        self.patch_fake(simctl_fail={"simctl list devices": [1, "boom"]})
        self.assertEqual(ios_simctl.booted_udids(str(FAKE_SIMCTL)), [])

    def test_unreadable_store_is_unknown(self) -> None:
        self.break_store()
        self.assertEqual(ios_slim.doctor_row(UDID),
                         {"udid": UDID, "slim_state": "unknown", "disabled": None})

    def test_doctor_reports_it_outside_health(self) -> None:
        self.write_store({"com.apple.apsd": True})
        code, payload = self.run_cli("--simctl", str(FAKE_SIMCTL), "doctor")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ios"]["booted_simulators"],
                         [{"udid": UDID, "slim_state": "partial", "disabled": 1}])
        self.assertNotIn("booted_simulators", payload["tools"])
        self.assertFalse(any("slim" in json.dumps(check) for check in payload["checks"]))


if __name__ == "__main__":
    unittest.main()
