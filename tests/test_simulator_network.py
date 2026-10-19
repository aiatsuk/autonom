"""`simulator network online|offline|shape` reports `verified` from a read-back.

- online/offline read Wi-Fi and mobile data back with `settings get global`;
  a match is `verified: true`, a mismatch `verified: false` with a
  `verified_reason` naming what the read-back showed, an unreadable value
  `verified: false` with "no read-back available";
- shape reads `emu network status` back: named profiles and numeric
  `<up>:<down>` / `<min>:<max>` ranges match, others are a mismatch with a
  reason, a console without the command is "no read-back available";
- a value refused before any device command is still refused that way;
- iOS keeps its refusal.

Every adb call goes to an in-process fake; no device.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, simulator  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

SERIAL = "emulator-5554"
TARGET = Target(ANDROID, SERIAL, "/fake/adb", {"serial": SERIAL})

STATUS = ("Current network status:\r\n"
          "  download speed:   {down:8d} bits/s (0.0 KB/s)\r\n"
          "  upload speed:     {up:8d} bits/s (0.0 KB/s)\r\n"
          "  minimum latency:  {low:4d} ms\r\n"
          "  maximum latency:  {high:4d} ms\r\n"
          "OK\r\n")


def status(up: int, down: int, low: int, high: int) -> str:
    return STATUS.format(up=up, down=down, low=low, high=high)


class FakeAdb:
    """Answers each adb argv from a table; records every call."""

    def __init__(self, answers: dict[tuple[str, ...], object] | None = None) -> None:
        self.answers = dict(answers or {})
        self.calls: list[list[str]] = []

    def __call__(self, adb, args, *, serial=None, timeout=30, check=True, binary=False):
        argv = list(args)
        self.calls.append(argv)
        answer = self.answers.get(tuple(argv), "")
        if callable(answer):
            answer = answer()
        code = 0
        if isinstance(answer, tuple):
            answer, code = answer
        return subprocess.CompletedProcess([adb, *argv], code, answer, "")


def switches(wifi, data) -> dict[tuple[str, ...], object]:
    return {("shell", "settings", "get", "global", "wifi_on"): wifi,
            ("shell", "settings", "get", "global", "mobile_data"): data}


class NetworkCase(unittest.TestCase):
    def run_control(self, fake: FakeAdb, action: str, values: dict | None = None) -> dict:
        with mock.patch.object(simulator.adb_mod, "run_adb", fake), \
                mock.patch.object(simulator.time, "sleep") as sleep:
            self.sleep = sleep
            return simulator.apply(TARGET, "network", action, values or {}, session={})


class OnlineOfflineTests(NetworkCase):
    def test_offline_read_back_matching_is_verified(self) -> None:
        fake = FakeAdb(switches("0\n", "0\n"))
        result = self.run_control(fake, "offline")
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "read_back")
        self.assertNotIn("verified_reason", result)
        self.assertEqual(result["observed_network"], {"wifi_on": "0", "mobile_data": "0"})
        self.assertIn(["shell", "svc", "wifi", "disable"], fake.calls)
        self.assertIn(["shell", "svc", "data", "disable"], fake.calls)
        # the commands run before the read-back
        self.assertLess(fake.calls.index(["shell", "svc", "data", "disable"]),
                        fake.calls.index(["shell", "settings", "get", "global", "wifi_on"]))

    def test_online_accepts_wifi_on_in_airplane_mode(self) -> None:
        result = self.run_control(FakeAdb(switches("2", "1")), "online")
        self.assertTrue(result["verified"])

    def test_mismatch_is_reread_then_reported_with_a_reason(self) -> None:
        fake = FakeAdb(switches("1", "0"))
        result = self.run_control(fake, "offline")
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "mismatch")
        self.assertEqual(result["verified_reason"],
                         "read-back showed Wi-Fi on, mobile data off, expected off")
        reads = [c for c in fake.calls if c[-1] == "wifi_on"]
        self.assertEqual(len(reads), simulator.NETWORK_READ_BACK_TRIES)
        self.assertEqual(self.sleep.call_count, simulator.NETWORK_READ_BACK_TRIES - 1)

    def test_a_late_switch_is_verified_on_a_later_read(self) -> None:
        answers = iter(["1", "0", "0"])
        fake = FakeAdb({("shell", "settings", "get", "global", "wifi_on"): lambda: next(answers),
                        ("shell", "settings", "get", "global", "mobile_data"): "0"})
        result = self.run_control(fake, "offline")
        self.assertTrue(result["verified"])
        self.assertEqual(len([c for c in fake.calls if c[-1] == "wifi_on"]), 2)

    def test_missing_read_back_is_unverified_with_a_reason(self) -> None:
        for wifi, data, missing in (("null", "0", "wifi_on"), ("0", "null", "mobile_data"),
                                    ("", "", "wifi_on/mobile_data"),
                                    (("boom", 1), "0", "wifi_on")):
            with self.subTest(wifi=wifi, data=data):
                fake = FakeAdb(switches(wifi, data))
                result = self.run_control(fake, "online")
                self.assertFalse(result["verified"])
                self.assertEqual(result["verification"], "unavailable")
                self.assertTrue(result["verified_reason"].startswith("no read-back available"))
                self.assertIn(missing, result["verified_reason"])
                # no retries when nothing can be read
                self.assertEqual(len([c for c in fake.calls if c[-1] == "wifi_on"]), 1)


class ShapeTests(NetworkCase):
    def shape(self, answer, **values) -> tuple[dict, FakeAdb]:
        fake = FakeAdb({("emu", "network", "status"): answer})
        return self.run_control(fake, "shape", values), fake

    def test_named_profiles_read_back_match(self) -> None:
        for speed, delay, up, down, low, high in (
                ("full", "none", 0, 0, 0, 0),
                ("edge", "edge", 473600, 473600, 80, 400),
                ("edge", "gprs", 118400, 236800, 150, 550),
                ("gsm", "umts", 14400, 14400, 35, 200),
                ("lte", "none", 58000000, 173000000, 0, 0)):
            with self.subTest(speed=speed, delay=delay):
                result, fake = self.shape(status(up, down, low, high), speed=speed, delay=delay)
                self.assertTrue(result["verified"], result)
                self.assertEqual(result["verification"], "read_back")
                self.assertNotIn("verified_reason", result)
                self.assertEqual(result["observed_network"]["download_bps"], down)
                self.assertEqual(fake.calls[:2], [["emu", "network", "speed", speed],
                                                  ["emu", "network", "delay", delay]])
                self.assertEqual(fake.calls[2], ["emu", "network", "status"])

    def test_numeric_ranges_match(self) -> None:
        result, _ = self.shape(status(100000, 200000, 100, 200), speed="100:200",
                               delay="100:200")
        self.assertTrue(result["verified"], result)
        result, _ = self.shape(status(64000, 64000, 50, 50), speed="64", delay="50")
        self.assertTrue(result["verified"], result)

    def test_mismatch_names_what_the_read_back_showed(self) -> None:
        result, _ = self.shape(status(0, 0, 0, 0), speed="edge", delay="edge")
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "mismatch")
        self.assertEqual(result["verified_reason"],
                         "read-back showed speed up 0 / down 0 bits/s, not 'edge' and "
                         "latency 0-0 ms, not 'edge'")
        result, _ = self.shape(status(0, 0, 80, 400), speed="full", delay="none")
        self.assertEqual(result["verified_reason"], "read-back showed latency 80-400 ms, not 'none'")

    def test_missing_read_back_is_unverified_with_a_reason(self) -> None:
        for answer in ("", "KO: unknown command\r\n", (status(0, 0, 0, 0), 1),
                       "Current network status:\r\n  download speed: 0 bits/s\r\n"):
            with self.subTest(answer=answer):
                result, _ = self.shape(answer, speed="full", delay="none")
                self.assertFalse(result["verified"])
                self.assertEqual(result["verification"], "unavailable")
                self.assertEqual(result["verified_reason"],
                                 "no read-back available: adb 'emu network status' printed "
                                 "no speeds and latencies")
                self.assertNotIn("observed_network", result)

    def test_invalid_values_are_refused_before_any_device_command(self) -> None:
        fake = FakeAdb()
        with self.assertRaises(errors.AutonomError) as caught:
            self.run_control(fake, "shape", {"speed": "warp"})
        self.assertEqual(caught.exception.code, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(fake.calls, [])

    def test_parse_network_status(self) -> None:
        self.assertEqual(simulator.parse_network_status(status(1, 2, 3, 4)),
                         {"upload_bps": 1, "download_bps": 2, "min_latency_ms": 3,
                          "max_latency_ms": 4})
        self.assertIsNone(simulator.parse_network_status(""))
        self.assertIsNone(simulator.parse_network_status(None))  # type: ignore[arg-type]


class IosTests(unittest.TestCase):
    def test_ios_network_is_still_refused(self) -> None:
        target = Target(IOS, "UDID", "/fake/xcrun", {"udid": "UDID"})
        with mock.patch.object(simulator.adb_mod, "run_adb") as run_adb:
            with self.assertRaises(errors.AutonomError) as caught:
                simulator.apply(target, "network", "offline", {}, session={})
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        run_adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
