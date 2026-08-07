"""`logs tail --since` must use the device's clock, and must not invent data.

Found on a live emulator whose clock had drifted 33 seconds behind the host.
`logs tail --grep '[Network]' --since 25` returned a single unrelated line
while 320 matching lines sat in the buffer: the window was computed from the
host clock, so every device-stamped line looked older than it was, the filter
kept nothing, and a silent fallback substituted the last 200 lines of the
buffer — data from an unrelated moment, presented as the requested window.

Both halves of that are covered here: the clock the window is computed from,
and the refusal to fill an empty answer with something else.
"""
from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib import logs  # noqa: E402


def _stamp(offset_seconds: float) -> str:
    return time.strftime("%m-%d %H:%M:%S.000",
                         time.localtime(time.time() + offset_seconds))


def _line(offset_seconds: float, body: str = "I flutter : [Network] GET /x") -> str:
    return f"{_stamp(offset_seconds)}  4577  4577 {body}"


class HostClockFallbackTests(unittest.TestCase):
    """The degraded path, used only when the device clock is unreadable."""

    def test_lines_inside_the_window_are_kept(self) -> None:
        lines = [_line(-5), _line(-2)]
        self.assertEqual(logs._filter_recent(lines, 30), lines)

    def test_an_empty_window_stays_empty(self) -> None:
        """The regression: this used to return `lines[-200:]` instead."""
        old = [_line(-600), _line(-500), _line(-400)]
        self.assertEqual(logs._filter_recent(old, 30), [])

    def test_undated_continuation_lines_are_dropped_not_kept_wholesale(self) -> None:
        kept = logs._filter_recent(["   continuation without a stamp", _line(-1)], 30)
        self.assertEqual(len(kept), 1)


class DeviceClockTests(unittest.TestCase):
    """The window must come from the clock that stamped the lines."""

    SKEW = 33  # seconds the emulator was behind its host when this was found

    def setUp(self) -> None:
        self.calls: list[list[str]] = []
        self._real = adb_mod.run_adb

        def fake_run_adb(_adb, args, **_kwargs):
            self.calls.append(list(args))
            if args[:2] == ["shell", "date"]:
                device_now = time.time() - self.SKEW
                return types.SimpleNamespace(
                    stdout=time.strftime("%m-%d_%H:%M:%S", time.localtime(device_now)),
                    stderr="", returncode=0,
                )
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        adb_mod.run_adb = fake_run_adb
        self.addCleanup(setattr, adb_mod, "run_adb", self._real)

    def test_cutoff_is_measured_from_the_device_clock(self) -> None:
        cutoff = logs._device_cutoff("adb", "emulator-5554", 30)
        self.assertIsNotNone(cutoff)
        parsed = time.mktime(time.strptime(
            f"{time.localtime().tm_year} {cutoff}", "%Y %m-%d %H:%M:%S.%f"))
        # Device now minus 30s — roughly 63s behind the host, not 30.
        self.assertAlmostEqual(time.time() - parsed, self.SKEW + 30, delta=3)

    def test_the_date_format_survives_the_device_shell(self) -> None:
        """A format string containing a space arrives as two arguments and
        `date` answers with only the first half — which is how this returned
        a bare '08-06' and no usable time at all."""
        logs._device_cutoff("adb", "emulator-5554", 30)
        date_call = next(c for c in self.calls if c[:2] == ["shell", "date"])
        self.assertEqual(len(date_call), 3)
        self.assertNotIn(" ", date_call[2])

    def test_windowing_is_delegated_to_logcat(self) -> None:
        """Filtering an 80k-line dump in Python was both slow and skew-prone."""
        logs.tail_logcat("adb", "emulator-5554", since_seconds=30)
        logcat_call = next(c for c in self.calls if c and c[0] == "logcat")
        self.assertIn("-t", logcat_call)
        self.assertRegex(logcat_call[logcat_call.index("-t") + 1],
                         r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")

    def test_an_unreadable_device_clock_falls_back_rather_than_failing(self) -> None:
        adb_mod.run_adb = lambda _a, args, **_k: types.SimpleNamespace(
            stdout="" if args[:2] == ["shell", "date"] else "", stderr="", returncode=1)
        self.assertIsNone(logs._device_cutoff("adb", "emulator-5554", 30))


if __name__ == "__main__":
    unittest.main()
