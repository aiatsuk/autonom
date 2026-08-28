"""Metrics depth (§2.4–2.6): memory pack, frames, trace presets, stimuli."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib.metrics import frames  # noqa: E402

SKILL_SCRIPT = ROOT / ("plugins/autonom/skills/flutter-performance-audit/"
                       "scripts/frame_timings_summary.py")
_SPEC = importlib.util.spec_from_file_location("skill_frames", SKILL_SCRIPT)
assert _SPEC and _SPEC.loader
SKILL = importlib.util.module_from_spec(_SPEC)
sys.modules["skill_frames"] = SKILL
_SPEC.loader.exec_module(SKILL)

GFXINFO = """Applications Graphics Acceleration Info:
Total frames rendered: 120
Janky frames: 5 (4.17%)
50th percentile: 6ms
90th percentile: 9ms
95th percentile: 12ms
99th percentile: 16ms
---PROFILEDATA---
Flags,IntendedVsync,Vsync
0,100,100
"""


class GfxinfoParserTests(unittest.TestCase):
    def test_parses_the_summary_lines(self) -> None:
        summary = frames.parse_gfxinfo(GFXINFO)
        self.assertTrue(summary["parsed"])
        self.assertEqual(summary["total_frames"], 120)
        self.assertEqual(summary["janky_frames"], 5)
        self.assertEqual(summary["janky_percent"], 4.17)
        self.assertEqual(summary["percentile_90_ms"], 9)

    def test_unknown_shape_is_honest_not_wrong(self) -> None:
        summary = frames.parse_gfxinfo("some future format\n")
        self.assertFalse(summary["parsed"])
        self.assertIn("raw artifact", summary["note"])


class FlutterSummaryEquivalenceTests(unittest.TestCase):
    def test_library_matches_the_skill_script_on_the_fixture(self) -> None:
        payload = json.loads((ROOT / "tests/fixtures/frame_timings.json")
                             .read_text(encoding="utf-8"))
        ours = frames.flutter_summary(payload, 16.67)
        assert ours is not None
        for lane in ("build", "raster"):
            if lane not in ours:
                continue
            values = SKILL.find_key(payload, SKILL._BUILD_KEYS if lane == "build"
                                    else SKILL._RASTER_KEYS)
            theirs = SKILL.summarize(values, 16.67)
            for key, value in theirs.items():
                self.assertAlmostEqual(ours[lane][key], value,
                                       msg=f"{lane}.{key} drifted")


class AndroidDepthCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self._state({"pidof": {"com.example.app": "4321"},
                     "dumpsys_gfxinfo": GFXINFO,
                     "which": {"simpleperf": "/system/bin/simpleperf"}})
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(self.state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
        })
        result = self._cli("session", "start", "--app-id", "com.example.app")
        assert result.returncode == 0, result.stderr
        self.artifacts = Path(json.loads(result.stdout)["session"]["artifacts_dir"])

    def _state(self, payload: dict) -> None:
        self.state.write_text(json.dumps(payload), encoding="utf-8")

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554", "--adb", str(FAKE_ADB), *args],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=120,
        )

    def _argv_log(self) -> list[list[str]]:
        log = Path(self.env["AUTONOM_FAKE_LOG"])
        return [json.loads(line)["argv"]
                for line in log.read_text(encoding="utf-8").splitlines()]

    def test_memory_capture_writes_the_full_pack(self) -> None:
        result = self._cli("metrics", "memory", "capture", "--label", "after-nav")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        names = [Path(f).name for f in payload["files"]]
        for suffix in ("-metadata.txt", "-meminfo.txt", "-proc-status.txt",
                       "-gfxinfo.txt", ".hprof"):
            self.assertTrue(any(n.endswith(suffix) for n in names),
                            f"missing {suffix} in {names}")
        self.assertTrue(all(Path(f).is_file() for f in payload["files"]))
        remote_rm = [a for a in self._argv_log() if a[2:4] == ["shell", "rm"]]
        self.assertTrue(remote_rm, "remote hprof must be cleaned up")

    def test_memory_capture_no_hprof_skips_the_dump(self) -> None:
        result = self._cli("metrics", "memory", "capture", "--no-hprof")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [Path(f).name for f in json.loads(result.stdout)["files"]]
        self.assertFalse(any(n.endswith(".hprof") for n in names))
        dumps = [a for a in self._argv_log() if "dumpheap" in a]
        self.assertEqual(dumps, [])

    def test_memory_analyze_runs_series_math_over_captures(self) -> None:
        capture_dir = Path(self.tmp.name) / "caps"
        capture_dir.mkdir()
        for index in (1, 2, 3):
            dest = capture_dir / f"run-{index}-meminfo.txt"
            shutil.copy(ROOT / f"tests/fixtures/meminfo-{index}.txt", dest)
            stamp = time.time() + index
            os.utime(dest, (stamp, stamp))
        result = self._cli("metrics", "memory", "analyze",
                           "--dir", str(capture_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["sample_count"], 3)
        self.assertTrue(payload["directional_growth_leads"])
        self.assertIn("Directional trend only", payload["interpretation"])

    def test_frames_reset_then_capture_summarizes_gfxinfo(self) -> None:
        reset = self._cli("metrics", "frames", "reset")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        result = self._cli("metrics", "frames", "capture")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["summary"]["total_frames"], 120)
        self.assertEqual(payload["summary"]["janky_percent"], 4.17)
        self.assertTrue(Path(payload["artifacts"][0]).is_file())
        resets = [a for a in self._argv_log() if a[-1:] == ["reset"]]
        self.assertTrue(resets, "reset must reach dumpsys gfxinfo")

    def test_frames_flutter_summary_cli(self) -> None:
        result = self._cli("metrics", "frames", "flutter-summary",
                           str(ROOT / "tests/fixtures/frame_timings.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["budget_ms"], 16.67)
        self.assertIn("build", payload)
        self.assertGreater(payload["build"]["frames"], 0)

    def test_trace_simpleperf_records_pulls_and_cleans_up(self) -> None:
        result = self._cli("metrics", "trace", "--preset", "simpleperf",
                           "--duration", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        artifact = Path(payload["artifacts"][0])
        self.assertTrue(artifact.is_file())
        self.assertTrue(artifact.name.endswith("-perf.data"))
        argvs = self._argv_log()
        self.assertTrue(any("simpleperf" in a and "record" in a for a in argvs))
        self.assertTrue(any(a[2:4] == ["shell", "rm"] for a in argvs))

    def test_trace_simpleperf_missing_is_tool_missing(self) -> None:
        self._state({"pidof": {"com.example.app": "4321"}, "which": {}})
        result = self._cli("metrics", "trace", "--preset", "simpleperf",
                           "--duration", "1")
        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stderr)
        self.assertEqual(envelope["error_code"], "tool_missing")
        self.assertEqual(envelope["tool"], "simpleperf")

    def test_ios_preset_on_android_is_preset_unavailable(self) -> None:
        result = self._cli("metrics", "trace", "--preset", "allocations")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "preset_unavailable")

    def test_memory_warn_on_android_is_unsupported(self) -> None:
        result = self._cli("metrics", "memory", "warn")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "unsupported_on_platform")

    def test_analyze_without_session_points_at_dir_not_out(self) -> None:
        env = dict(self.env)
        env["AUTONOM_HOME"] = str(Path(self.tmp.name) / "empty-home")
        result = subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--adb", str(FAKE_ADB), "metrics", "memory", "analyze"],
            env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=60)
        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stderr)
        self.assertIn("--dir", envelope["hint"])
        self.assertNotIn("--out", envelope["hint"],
                         "analyze does not accept --out; the hint must not "
                         "recommend a flag argparse would reject")


class IosDepthCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.state.write_text(json.dumps({
            "installed": ["com.example.app"],
            "running": ["com.example.app"],
            "xctrace": True,
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(self.state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
        })

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "ios", "--target", UDID,
             "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB), *args],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=120,
        )

    def test_xctrace_preset_produces_a_trace_bundle(self) -> None:
        out = Path(self.tmp.name) / "traces"
        result = self._cli("metrics", "trace", "--preset", "time-profiler",
                           "--duration", "1", "--app-id", "com.example.app",
                           "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        artifact = Path(payload["artifacts"][0])
        self.assertTrue(artifact.name.endswith("-time-profiler.trace"))
        self.assertTrue((artifact / "info.plist").is_file())

    def test_xctrace_missing_lists_an_install_hint(self) -> None:
        self.state.write_text(json.dumps({
            "installed": ["com.example.app"],
            "running": ["com.example.app"],
        }), encoding="utf-8")
        result = self._cli("metrics", "trace", "--preset", "leaks",
                           "--duration", "1", "--app-id", "com.example.app",
                           "--out", str(Path(self.tmp.name) / "t"))
        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stderr)
        self.assertEqual(envelope["error_code"], "tool_missing")
        self.assertEqual(envelope["tool"], "xctrace")
        self.assertIn("Xcode", envelope["hint"])

    def test_memory_warn_posts_the_darwin_notification(self) -> None:
        result = self._cli("metrics", "memory", "warn")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["stimulus"], "memory_warning")
        log = Path(self.env["AUTONOM_FAKE_LOG"])
        argvs = [json.loads(line)["argv"]
                 for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("notifyutil" in a for a in argvs))


if __name__ == "__main__":
    unittest.main()
