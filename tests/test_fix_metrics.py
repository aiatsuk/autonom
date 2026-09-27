"""Metrics defects found by live device testing (2026-09-26).

1. `metrics trace --preset simpleperf` asked for the default `cpu-cycles`
   event with `-p <pid>`; an emulator has no PMU and refused it. The preset
   now records `--app <pkg> -e <event>`, taking `cpu-cycles` only when
   `simpleperf list hw` offers it and `cpu-clock` otherwise.
2. `metrics snapshot/series` reported a `dumpsys cpuinfo` figure whose
   window ended minutes before the snapshot, so a series repeated one stale
   number. A fresh two-sample `/proc` measurement is preferred; the cpuinfo
   fallback reports its window and is marked stale when old.
3. gfxinfo for a Flutter app counted 0 frames yet reported 4950 ms
   percentiles with `parsed: true`. Zero frames now means no percentiles and
   a `no_frames` warning pointing at flutter-summary.
4. `metrics list-presets` offered `hitches` on the Simulator, where
   Instruments refuses it; it is now unavailable there with a reason.

Library tests stub `adb.run_adb`; CLI tests drive the shared fakes read-only.
"""
from __future__ import annotations

import json
import os
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
PKG = "com.example.app"
PID = 4321

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib.metrics import frames, meminfo, presets, series, snapshot  # noqa: E402
from autonom_lib.metrics import process as process_mod  # noqa: E402
from autonom_lib.metrics import trace  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

ANDROID = Target("android", "emulator-5554", "/fake/adb", {"serial": "emulator-5554"})

# What a Flutter app on an API 36 emulator printed (package anonymised,
# buffer and histogram lines trimmed): HWUI never saw a frame, and the
# percentile lines fall back to the histogram's top bucket.
ZERO_FRAMES_GFXINFO = """Applications Graphics Acceleration Info:
Uptime: 5611841 Realtime: 5611841

** Graphics info for pid 4321 [com.example.app] **

Stats since: 5599329787703ns
Total frames rendered: 0
Janky frames: 0 (0.00%)
Janky frames (legacy): 0 (0.00%)
50th percentile: 4950ms
90th percentile: 4950ms
95th percentile: 4950ms
99th percentile: 4950ms
Number Missed Vsync: 0
Number High input latency: 0
Number Slow UI thread: 0
HISTOGRAM: 5ms=0 6ms=0 7ms=0 4950ms=0
50th gpu percentile: 4950ms
90th gpu percentile: 4950ms

Pipeline=Skia (OpenGL)
GraphicBufferAllocator buffers:
0xb400007703901930 |      unknown | 1080 (   0) x 2424 |      1 |        1 | 0x     b00 | a0c3cb3 SurfaceView[com.example.app/com.example.app.MainActivity]#5(BLAST Consumer)5

Profile data in ms:

\tcom.example.app/com.example.app.MainActivity/android.view.ViewRootImpl@1d5ddd6 (visibility=0)
Window: com.example.app/com.example.app.MainActivity
Stats since: 5599329787245ns
Total frames rendered: 0
Janky frames: 0 (0.00%)
50th percentile: 4950ms
90th percentile: 4950ms
95th percentile: 4950ms
99th percentile: 4950ms
"""

# The header an API 36 emulator printed during the live run: the averaging
# window closed more than three minutes before the snapshot was taken.
STALE_CPUINFO = (
    "Load: 6.15 / 6.47 / 6.63\n"
    "CPU usage from 221640ms to 186792ms ago "
    "(2026-09-26 12:05:25.525 to 2026-09-26 12:06:00.373):\n"
    "  3.9% 4321/com.example.app: 2.9% user + 1% kernel / faults: 12 minor\n"
    "  9.6% 449/android.hardware.graphics.composer3-service.ranchu: 0.5% user\n"
)


def _pid_stat(utime: int, stime: int, comm: str = "com.example.app") -> str:
    # fields 1..17 of /proc/<pid>/stat; utime/stime are fields 14/15
    return (f"{PID} ({comm}) S 1 {PID} 0 0 -1 1077952832 1000 0 0 0 "
            f"{utime} {stime} 0 0 10 -10 40 0 5599329\n")


def _proc_stat(user: int, system: int, idle: int, cores: int = 4,
               steal: int = 0) -> str:
    lines = [f"cpu  {user} 0 {system} {idle} 0 0 0 {steal} 0 0"]
    lines += [f"cpu{n} 1 0 1 1 0 0 0 0 0 0" for n in range(cores)]
    lines += ["intr 1 2 3", "ctxt 99"]
    return "\n".join(lines) + "\n"


def _cpuinfo_for(d_utime: int, d_stime: int, d_machine: int, cores: int,
                 age_ms: int = 0, ms_per_tick: int = 10) -> str:
    """What `dumpsys cpuinfo` prints for the same tick deltas.

    ProcessCpuTracker.printProcessCPU divides the process' CPU ms by its
    wall uptime ms (`st.rel_uptime`), not by the machine's all-core ticks;
    the machine delta over the core count is that wall time in ticks."""
    wall_ms = d_machine // cores * ms_per_tick
    percent = 100 * (d_utime + d_stime) * ms_per_tick / wall_ms
    user = 100 * d_utime * ms_per_tick / wall_ms
    kernel = 100 * d_stime * ms_per_tick / wall_ms
    total = 100 * (d_utime + d_stime) / d_machine  # the all-cores TOTAL line
    return (f"CPU usage from {wall_ms + age_ms}ms to {age_ms}ms ago:\n"
            f"  {percent:g}% {PID}/{PKG}: {user:g}% user + {kernel:g}% kernel\n"
            f"  {total:g}% TOTAL: {total:g}% user + 0% kernel\n")


class ScriptedAdb:
    """Stands in for `adb.run_adb`: answers by argv prefix, records calls.

    An answer is `(returncode, stdout)` or an exception to raise (a hung or
    wedged adb), or a list of them consumed in order (the last one repeats)."""

    def __init__(self, rules: list[tuple[tuple[str, ...], object]]) -> None:
        self.rules = rules
        self.calls: list[list[str]] = []

    def __call__(self, adb, args, *, serial=None, timeout=30, check=True,
                 binary=False):
        argv = list(args)
        self.calls.append(argv)
        code, out = 0, ""
        for prefix, answer in self.rules:
            if argv[:len(prefix)] == list(prefix):
                if isinstance(answer, list):
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                if isinstance(answer, BaseException):
                    raise answer
                code, out = answer  # type: ignore[misc]
                break
        if argv[:1] == ["pull"] and code == 0:
            Path(argv[2]).write_bytes(b"PERFDATA")
        return subprocess.CompletedProcess([adb, *argv], code, out, "")

    def matching(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def _resolved(target, app_id):
    return {"pid": PID, "sources_tried": ["stub"]}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# --- 1. simpleperf -----------------------------------------------------------


class SimpleperfEventTests(unittest.TestCase):
    def _run(self, fake: ScriptedAdb) -> dict:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(adb_mod, "run_adb", fake), \
                mock.patch.object(process_mod, "resolve", _resolved):
            return trace.run_preset(ANDROID, PKG, "simpleperf", duration=1,
                                    out_dir=Path(tmp), label="sp")

    def _rules(self, list_hw: tuple[int, str]) -> list:
        return [
            (("shell", "which", "simpleperf"), (0, "/system/bin/simpleperf\n")),
            (("shell", "simpleperf", "list", "hw"), list_hw),
        ]

    def test_emulator_without_a_pmu_records_cpu_clock_for_the_app(self) -> None:
        # the emulator lists no hardware events at all
        fake = ScriptedAdb(self._rules((0, "List of hardware events:\n"
                                           "  # More hardware events are available "
                                           "in 'simpleperf list raw'.\n")))
        payload = self._run(fake)
        record = fake.matching("shell", "simpleperf", "record")
        self.assertEqual(len(record), 1, fake.calls)
        argv = record[0]
        self.assertEqual(argv[argv.index("--app") + 1], PKG)
        self.assertEqual(argv[argv.index("-e") + 1], "cpu-clock")
        self.assertNotIn("-p", argv, "pid attach used the default hardware event")
        self.assertEqual(payload["event"], "cpu-clock")
        self.assertEqual(payload["pid"], PID)

    def test_a_device_that_lists_cpu_cycles_keeps_the_hardware_event(self) -> None:
        fake = ScriptedAdb(self._rules((0, "List of hardware events:\n  cpu-cycles\n"
                                           "  instructions\n  branch-misses\n")))
        payload = self._run(fake)
        argv = fake.matching("shell", "simpleperf", "record")[0]
        self.assertEqual(argv[argv.index("-e") + 1], "cpu-cycles")
        self.assertEqual(payload["event"], "cpu-cycles")

    def test_an_unanswered_event_list_falls_back_to_cpu_clock(self) -> None:
        fake = ScriptedAdb(self._rules((1, "simpleperf: unknown command list\n")))
        self._run(fake)
        argv = fake.matching("shell", "simpleperf", "record")[0]
        self.assertEqual(argv[argv.index("-e") + 1], "cpu-clock")

    def test_record_failure_names_the_profileable_requirement(self) -> None:
        rules = self._rules((0, "List of hardware events:\n"))
        rules.insert(0, (("shell", "simpleperf", "record"),
                         (1, "simpleperf E app is not debuggable or profileable")))
        fake = ScriptedAdb(rules)
        from autonom_lib import errors
        with self.assertRaises(errors.AutonomError) as caught:
            self._run(fake)
        self.assertEqual(caught.exception.code, errors.TRACE_FAILED)
        self.assertIn("profileable", caught.exception.hint)
        self.assertTrue(fake.matching("shell", "rm", "-f"),
                        "the remote perf.data must still be cleaned up")


# --- 2. CPU freshness --------------------------------------------------------


class CpuinfoWindowParserTests(unittest.TestCase):
    def test_millisecond_window_reports_width_and_age(self) -> None:
        self.assertEqual(meminfo.parse_cpuinfo_window(STALE_CPUINFO),
                         {"cpu_window_ms": 34848, "cpu_window_age_ms": 186792})

    def test_second_window_is_converted(self) -> None:
        self.assertEqual(
            meminfo.parse_cpuinfo_window("CPU usage from 10s to 0s ago:\n"),
            {"cpu_window_ms": 10000, "cpu_window_age_ms": 0})

    def test_no_header_is_none(self) -> None:
        self.assertIsNone(meminfo.parse_cpuinfo_window("Load: 1.0\n"))

    def test_pid_stat_survives_a_comm_with_spaces_and_parentheses(self) -> None:
        self.assertEqual(
            meminfo.parse_proc_pid_stat(_pid_stat(70, 30, comm="my (app) x"), PID),
            100)
        self.assertIsNone(meminfo.parse_proc_pid_stat("Threads:\t42\n", PID))
        self.assertIsNone(meminfo.parse_proc_pid_stat(_pid_stat(1, 1), 999),
                          "another pid's stat line must not be credited")

    def test_proc_stat_total_matches_cpuinfo_semantics(self) -> None:
        # user+nice+system+idle+iowait+irq+softirq+steal(7); guest(9) and
        # guest_nice(0) are already inside user and nice
        text = "cpu  100 5 50 800 10 3 2 7 9 0\ncpu0 1 0 0 0 0 0 0\ncpu1 1 0 0 0\n"
        self.assertEqual(meminfo.parse_proc_stat(text),
                         {"total_ticks": 977, "cpu_count": 2})

    def test_proc_stat_of_an_old_kernel_sums_the_fields_it_has(self) -> None:
        # 2.6-era kernels print four (pre-iowait) or seven (pre-steal) values
        self.assertEqual(meminfo.parse_proc_stat("cpu  100 5 50 800\ncpu0 1 1 1 1\n"),
                         {"total_ticks": 955, "cpu_count": 1})
        self.assertEqual(
            meminfo.parse_proc_stat("cpu  100 5 50 800 10 3 2\ncpu0 1 1 1 1 1 1 1\n"),
            {"total_ticks": 970, "cpu_count": 1})
        self.assertIsNone(meminfo.parse_proc_stat("cpu\ncpu0 1\n"))


class FreshCpuSnapshotTests(unittest.TestCase):
    def _meminfo(self) -> str:
        return (ROOT / "tests/fixtures/meminfo-1.txt").read_text(encoding="utf-8")

    def _take(self, fake: ScriptedAdb) -> dict:
        clock = Clock()
        with mock.patch.object(adb_mod, "run_adb", fake), \
                mock.patch.object(process_mod, "resolve", _resolved):
            payload, _raw = snapshot.take(ANDROID, PKG, sleep=clock.sleep,
                                          clock=clock)
        return payload

    def _rules(self, stat_samples: list, cpuinfo: str = STALE_CPUINFO) -> list:
        return [
            (("shell", "dumpsys", "meminfo"), (0, self._meminfo())),
            (("shell", "cat", f"/proc/{PID}/stat", "/proc/stat"), stat_samples),
            (("shell", "cat", f"/proc/{PID}/status"),
             (0, "Threads:\t42\nVmRSS:\t8500 kB\nVmSize:\t120000 kB\n")),
            (("shell", "dumpsys", "cpuinfo"), (0, cpuinfo)),
        ]

    def test_two_proc_samples_give_a_current_figure(self) -> None:
        fake = ScriptedAdb(self._rules([
            (0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000)),
            (0, _pid_stat(1100, 550) + _proc_stat(10400, 5200, 85400)),
        ]))
        payload = self._take(fake)
        # 150 process ticks; 1000 machine ticks over 4 cores = 250 wall ticks
        self.assertEqual(payload["cpu"]["process_percent"], 60.0)
        self.assertTrue(payload["cpu"]["available"])
        sampling = payload["cpu_sampling"]
        self.assertEqual(sampling["source"], "proc_stat_delta")
        self.assertFalse(sampling["stale"])
        self.assertEqual(sampling["cpu_window_ms"], 500)
        self.assertEqual(sampling["cpu_window_age_ms"], 0)
        self.assertEqual(sampling["cpu_count"], 4)
        self.assertEqual(sampling["scale"], "percent_of_one_core")
        self.assertNotIn("warnings", payload)
        self.assertEqual(fake.matching("shell", "dumpsys", "cpuinfo"), [],
                         "the stale cpuinfo average is not needed")

    def test_a_series_of_fresh_samples_varies_with_the_load(self) -> None:
        fake = ScriptedAdb(self._rules([
            (0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000)),
            (0, _pid_stat(1100, 550) + _proc_stat(10400, 5200, 85400)),
            (0, _pid_stat(1100, 550) + _proc_stat(10400, 5200, 85400)),
            (0, _pid_stat(1110, 560) + _proc_stat(10800, 5400, 85800)),
        ]))
        clock = Clock()
        with mock.patch.object(adb_mod, "run_adb", fake), \
                mock.patch.object(process_mod, "resolve", _resolved):
            samples = series.capture(
                lambda: snapshot.take(ANDROID, PKG, sleep=clock.sleep,
                                      clock=clock)[0],
                count=2, interval=0, sleep=clock.sleep)
        values = [s["metrics"]["process_percent"] for s in samples]
        self.assertEqual(values, [60.0, 8.0])
        self.assertEqual([s["cpu_stale"] for s in samples], [False, False])
        for sample in samples:
            self.assertFalse(any(k.startswith("cpu_window") for k in sample["metrics"]),
                             "window bookkeeping must never become a series metric")

    def test_unreadable_proc_falls_back_to_cpuinfo_and_marks_it_stale(self) -> None:
        fake = ScriptedAdb(self._rules([(1, "cat: /proc/4321/stat: Permission denied\n")]))
        payload = self._take(fake)
        self.assertEqual(payload["cpu"]["process_percent"], 3.9)
        sampling = payload["cpu_sampling"]
        self.assertEqual(sampling["source"], "dumpsys_cpuinfo")
        self.assertEqual(sampling["cpu_window_ms"], 34848)
        self.assertEqual(sampling["cpu_window_age_ms"], 186792)
        self.assertTrue(sampling["stale"])
        self.assertIn("fresh_sample_error", sampling)
        codes = [w["code"] for w in payload.get("warnings", [])]
        self.assertIn("cpu_stale", codes)
        self.assertIn("186792", payload["cpu"]["note"])

    def test_a_recent_cpuinfo_window_is_not_stale(self) -> None:
        recent = ("CPU usage from 4000ms to 1000ms ago:\n"
                  "  12.5% 4321/com.example.app: 8% user + 4.5% kernel\n")
        fake = ScriptedAdb(self._rules([(0, "garbage\n")], cpuinfo=recent))
        payload = self._take(fake)
        self.assertEqual(payload["cpu"]["process_percent"], 12.5)
        self.assertFalse(payload["cpu_sampling"]["stale"])
        self.assertNotIn("cpu_stale", [w["code"] for w in payload.get("warnings", [])])

    def test_a_cpuinfo_figure_without_a_window_is_not_vouched_for(self) -> None:
        bare = "  12.5% 4321/com.example.app: 8% user + 4.5% kernel\n"
        fake = ScriptedAdb(self._rules([(0, "garbage\n")], cpuinfo=bare))
        payload = self._take(fake)
        self.assertTrue(payload["cpu_sampling"]["stale"])
        self.assertNotIn("cpu_window_age_ms", payload["cpu_sampling"])
        self.assertIn("cpu_stale", [w["code"] for w in payload["warnings"]])

    def test_a_restarted_process_between_samples_is_not_measured(self) -> None:
        # the second read comes from a different process: no delta to trust
        fake = ScriptedAdb(self._rules([
            (0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000)),
            (1, "cat: /proc/4321/stat: No such file or directory\n"
                + _proc_stat(10400, 5200, 85400)),
        ]))
        payload = self._take(fake)
        self.assertEqual(payload["cpu_sampling"]["source"], "dumpsys_cpuinfo")

    def test_fresh_and_fallback_paths_agree_on_scale_for_the_same_load(self) -> None:
        # one load, two sources: the fixture tick deltas (utime +100, stime +50,
        # machine +1000 on 4 cores) and the line cpuinfo prints for them
        samples = [(0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000)),
                   (0, _pid_stat(1100, 550) + _proc_stat(10400, 5200, 85400))]
        same_load = _cpuinfo_for(100, 50, 1000, 4)
        fresh = self._take(ScriptedAdb(self._rules(list(samples), cpuinfo=same_load)))
        fallback = self._take(ScriptedAdb(self._rules(
            [(1, "cat: /proc/4321/stat: Permission denied\n")], cpuinfo=same_load)))
        self.assertEqual(fresh["cpu_sampling"]["source"], "proc_stat_delta")
        self.assertEqual(fallback["cpu_sampling"]["source"], "dumpsys_cpuinfo")
        self.assertEqual(fresh["cpu"]["process_percent"],
                         fallback["cpu"]["process_percent"])
        self.assertEqual(fresh["cpu"]["process_percent"], 60.0)
        self.assertEqual(fresh["cpu_sampling"]["scale"],
                         fallback["cpu_sampling"]["scale"])

    def test_steal_time_counts_as_wall_time_and_lowers_the_figure(self) -> None:
        # the same process deltas as the 60.0 case, but the VM host stole 250
        # machine ticks: wall is (1000 + 250) / 4 cores, so 150 * 4 / 1250
        fake = ScriptedAdb(self._rules([
            (0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000, steal=100)),
            (0, _pid_stat(1100, 550) + _proc_stat(10400, 5200, 85400, steal=350)),
        ]))
        payload = self._take(fake)
        self.assertEqual(payload["cpu_sampling"]["source"], "proc_stat_delta")
        self.assertEqual(payload["cpu"]["process_percent"], 48.0,
                         "without steal the denominator shrinks and reads 60.0")

    def test_one_pegged_core_reads_100_and_two_read_200(self) -> None:
        for process_ticks, expected in ((250, 100.0), (500, 200.0)):
            fake = ScriptedAdb(self._rules([
                (0, _pid_stat(1000, 0) + _proc_stat(10000, 5000, 85000)),
                (0, _pid_stat(1000 + process_ticks, 0)
                    + _proc_stat(10000 + process_ticks, 5000, 86000 - process_ticks)),
            ]))
            payload = self._take(fake)
            self.assertEqual(payload["cpu"]["process_percent"], expected,
                             "a busy core must not read as 1/N of the device")

    def test_a_hung_proc_read_falls_back_instead_of_losing_the_snapshot(self) -> None:
        for failure in (subprocess.TimeoutExpired(["adb"], 15),
                        adb_mod.AdbError("error: device offline")):
            fake = ScriptedAdb(self._rules([failure]))
            payload = self._take(fake)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["memory"]["total_pss_kb"], 9000)
            self.assertEqual(payload["cpu_sampling"]["source"], "dumpsys_cpuinfo")
            self.assertEqual(payload["cpu"]["process_percent"], 3.9)
            error = payload["cpu_sampling"]["fresh_sample_error"]
            self.assertIn("timed out" if isinstance(failure, subprocess.TimeoutExpired)
                          else "device offline", error)

    def test_a_hung_cpuinfo_too_costs_only_the_cpu_figure(self) -> None:
        rules = self._rules([subprocess.TimeoutExpired(["adb"], 15)])
        rules[-1] = (("shell", "dumpsys", "cpuinfo"),
                     subprocess.TimeoutExpired(["adb"], 60))
        payload = self._take(ScriptedAdb(rules))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cpu"], {"available": False})
        self.assertNotIn("cpu_sampling", payload)
        warning = [w for w in payload["warnings"] if w["code"] == "cpu_unavailable"]
        self.assertIn("timed out", warning[0]["error"])

    def test_a_reused_pid_between_samples_falls_back(self) -> None:
        # same pid number, new process: its ticks restart below the first read
        fake = ScriptedAdb(self._rules([
            (0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000)),
            (0, _pid_stat(3, 1) + _proc_stat(10400, 5200, 85400)),
        ]))
        payload = self._take(fake)
        self.assertEqual(payload["cpu_sampling"]["source"], "dumpsys_cpuinfo")
        self.assertIn("backwards", payload["cpu_sampling"]["fresh_sample_error"])

    def test_no_elapsed_machine_time_falls_back(self) -> None:
        same = (0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000))
        payload = self._take(ScriptedAdb(self._rules([same, same])))
        self.assertEqual(payload["cpu_sampling"]["source"], "dumpsys_cpuinfo")
        self.assertIn("no elapsed", payload["cpu_sampling"]["fresh_sample_error"])

    def test_a_proc_stat_without_core_lines_is_not_scaled_by_guess(self) -> None:
        fake = ScriptedAdb(self._rules([
            (0, _pid_stat(1000, 500) + _proc_stat(10000, 5000, 85000, cores=0)),
            (0, _pid_stat(1100, 550) + _proc_stat(10400, 5200, 85400, cores=0)),
        ]))
        payload = self._take(fake)
        self.assertEqual(payload["cpu_sampling"]["source"], "dumpsys_cpuinfo")
        self.assertIn("cpuN", payload["cpu_sampling"]["fresh_sample_error"])

    def test_series_summary_warns_when_samples_carry_stale_cpu(self) -> None:
        samples = [
            {"metrics": {"process_percent": 3.9}, "cpu_stale": True},
            {"metrics": {"process_percent": 3.9}, "cpu_stale": True},
            {"metrics": {"process_percent": 7.0}, "cpu_stale": False},
        ]
        report = series.summarize(samples, 1024)
        warning = [w for w in report["warnings"] if w["code"] == "cpu_stale"]
        self.assertEqual(len(warning), 1)
        self.assertIn("2 of 3", warning[0]["error"])
        self.assertNotIn("warnings", series.summarize(
            [{"metrics": {"x": 1}}, {"metrics": {"x": 2}}], 1024))

    def test_offline_series_keeps_the_stale_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1-x-snapshot.json"
            path.write_text(json.dumps({
                "cpu": {"available": True, "process_percent": 3.9},
                "cpu_sampling": {"source": "dumpsys_cpuinfo", "stale": True},
            }), encoding="utf-8")
            samples = series.from_dir(Path(tmp), "*-snapshot.json")
        self.assertTrue(samples[0]["cpu_stale"])
        self.assertEqual(samples[0]["cpu_source"], "dumpsys_cpuinfo")


# --- 3. frames ---------------------------------------------------------------


class ZeroFramesTests(unittest.TestCase):
    def test_zero_frames_report_no_percentiles_and_explain_flutter(self) -> None:
        summary = frames.parse_gfxinfo(ZERO_FRAMES_GFXINFO)
        self.assertEqual(summary["total_frames"], 0)
        for key in ("percentile_50_ms", "percentile_90_ms", "percentile_95_ms",
                    "percentile_99_ms", "janky_percent"):
            self.assertNotIn(key, summary, f"{key} over zero frames is invented")
        warning = [w for w in summary["warnings"] if w["code"] == "no_frames"]
        self.assertEqual(len(warning), 1)
        self.assertIn("Flutter", warning[0]["error"])
        self.assertIn("HWUI", warning[0]["error"])
        self.assertIn("flutter-summary", warning[0]["hint"])

    def test_rendered_frames_keep_their_percentiles(self) -> None:
        summary = frames.parse_gfxinfo(
            "Total frames rendered: 120\nJanky frames: 5 (4.17%)\n"
            "50th percentile: 6ms\n90th percentile: 9ms\n")
        self.assertEqual(summary["percentile_50_ms"], 6)
        self.assertEqual(summary["janky_percent"], 4.17)
        self.assertNotIn("warnings", summary)

    def test_gfxinfo_flow_preset_surfaces_the_warning(self) -> None:
        fake = ScriptedAdb([(("shell", "dumpsys", "gfxinfo", PKG, "framestats"),
                             (0, ZERO_FRAMES_GFXINFO))])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(adb_mod, "run_adb", fake):
            payload = trace.run_preset(ANDROID, PKG, "gfxinfo-flow", duration=0,
                                       out_dir=Path(tmp), label="flow",
                                       sleep=lambda _s: None)
        self.assertNotIn("percentile_99_ms", payload["summary"])
        self.assertIn("no_frames", [w["code"] for w in payload["warnings"]])

    def test_ios_frames_hint_does_not_send_the_simulator_to_hitches(self) -> None:
        hint = frames.IOS_FRAMES_HINT
        self.assertIn("flutter-summary", hint)
        self.assertIn("physical device", hint)
        self.assertNotEqual(hint, "On iOS use 'metrics trace --preset hitches'.")


# --- 4. hitches on the Simulator ---------------------------------------------


class HitchesPresetTests(unittest.TestCase):
    def _rows(self, platform: str | None) -> dict:
        with mock.patch.object(presets, "xctrace_available", lambda _x: True):
            listing = presets.listing(platform, adb="/fake/adb", xcrun="/fake/xcrun")
        return {row["id"]: row for row in listing["presets"]}

    def test_hitches_is_unavailable_on_the_simulator_with_a_reason(self) -> None:
        for platform in ("ios", None):
            rows = self._rows(platform)
            self.assertFalse(rows["hitches"]["available"], platform)
            self.assertEqual(rows["hitches"]["reason"], "unsupported_on_simulator")
            self.assertIn("physical device", rows["hitches"]["note"])
            for other in ("allocations", "time-profiler", "leaks"):
                self.assertTrue(rows[other]["available"], (platform, other))

    def test_android_listing_still_calls_hitches_ios_only(self) -> None:
        rows = self._rows("android")
        self.assertEqual(rows["hitches"]["reason"], "ios_only")


class MetricsFixCliTests(unittest.TestCase):
    """End to end through the CLI with the shared fakes (read-only use)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(self.state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
        })

    def _state(self, **payload) -> None:
        self.state.write_text(json.dumps(payload), encoding="utf-8")

    def _android(self, *args: str) -> tuple[int, dict]:
        result = subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554", "--adb", str(FAKE_ADB), *args],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=120)
        body = result.stdout if result.returncode == 0 else result.stderr
        return result.returncode, json.loads(body)

    def _argv_log(self) -> list[list[str]]:
        log = Path(self.env["AUTONOM_FAKE_LOG"])
        return [json.loads(line)["argv"]
                for line in log.read_text(encoding="utf-8").splitlines()]

    def test_simpleperf_preset_records_the_app_with_a_software_event(self) -> None:
        self._state(pidof={PKG: str(PID)},
                    which={"simpleperf": "/system/bin/simpleperf"})
        code, payload = self._android("metrics", "trace", "--preset", "simpleperf",
                                      "--duration", "1", "--app-id", PKG,
                                      "--out", str(Path(self.tmp.name) / "t"))
        self.assertEqual(code, 0, payload)
        record = [a for a in self._argv_log() if "record" in a and "simpleperf" in a]
        self.assertEqual(len(record), 1)
        self.assertIn("--app", record[0])
        self.assertIn("cpu-clock", record[0])
        self.assertEqual(payload["event"], "cpu-clock")

    def test_snapshot_reports_the_stale_cpuinfo_window(self) -> None:
        self._state(pidof={PKG: str(PID)}, dumpsys_cpuinfo=STALE_CPUINFO)
        code, payload = self._android("metrics", "snapshot", "--app-id", PKG)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["cpu"]["process_percent"], 3.9)
        self.assertEqual(payload["cpu_sampling"]["cpu_window_age_ms"], 186792)
        self.assertTrue(payload["cpu_sampling"]["stale"])
        self.assertIn("cpu_stale", [w["code"] for w in payload["warnings"]])

    def test_frames_capture_of_a_flutter_app_has_no_invented_percentiles(self) -> None:
        self._state(pidof={PKG: str(PID)}, dumpsys_gfxinfo=ZERO_FRAMES_GFXINFO)
        code, payload = self._android("metrics", "frames", "capture", "--app-id", PKG)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["summary"]["total_frames"], 0)
        self.assertNotIn("percentile_50_ms", payload["summary"])
        self.assertIn("no_frames", [w["code"] for w in payload["summary"]["warnings"]])

    def test_list_presets_on_ios_marks_hitches_unavailable(self) -> None:
        self._state(xctrace=True)
        result = subprocess.run(
            [sys.executable, str(CLI), "--platform", "ios",
             "--simctl", str(FAKE_SIMCTL), "metrics", "list-presets"],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row["id"]: row for row in json.loads(result.stdout)["presets"]}
        self.assertTrue(rows["time-profiler"]["available"])
        self.assertFalse(rows["hitches"]["available"])
        self.assertEqual(rows["hitches"]["reason"], "unsupported_on_simulator")


if __name__ == "__main__":
    unittest.main()
