"""Metrics foundation (§2.2–2.3, §2.7): snapshots, series math, presets.

The meminfo parser and the series algorithm are ports of the proven
android-memory-leaks skill script; equivalence tests pin library and script
to the same fixtures so they cannot drift apart.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib.metrics import meminfo, series  # noqa: E402

SKILL_SCRIPT = ROOT / ("plugins/autonom/skills/android-memory-leaks/scripts/"
                       "analyze_meminfo_series.py")
_SPEC = importlib.util.spec_from_file_location("skill_meminfo", SKILL_SCRIPT)
assert _SPEC and _SPEC.loader
SKILL = importlib.util.module_from_spec(_SPEC)
sys.modules["skill_meminfo"] = SKILL
_SPEC.loader.exec_module(SKILL)


class MeminfoParserTests(unittest.TestCase):
    def test_library_and_skill_script_agree_on_every_fixture(self) -> None:
        for index in (1, 2, 3):
            text = (ROOT / f"tests/fixtures/meminfo-{index}.txt").read_text(
                encoding="utf-8")
            self.assertEqual(meminfo.parse_meminfo(text),
                             SKILL.parse_meminfo(text),
                             f"meminfo-{index}.txt drifted between ports")

    def test_proc_status_extracts_threads_and_rss(self) -> None:
        parsed = meminfo.parse_proc_status(
            "Name:\tapp\nThreads:\t42\nVmRSS:\t  8500 kB\nVmSize:\t120000 kB\n")
        self.assertEqual(parsed, {"threads": 42, "vm_rss_kb": 8500,
                                  "vm_size_kb": 120000})

    def test_cpuinfo_finds_the_app_line_or_none(self) -> None:
        text = ("Load: 1.2\n"
                "  12.5% 4321/com.example.app: 8% user + 4.5% kernel\n"
                "  3% 100/system_server: 2% user + 1% kernel\n")
        self.assertEqual(meminfo.parse_cpuinfo(text, "com.example.app"), 12.5)
        self.assertIsNone(meminfo.parse_cpuinfo(text, "com.other.app"))

    def test_cpuinfo_never_claims_a_sibling_flavor_package(self) -> None:
        text = "  7% 999/com.example.app.dev: 5% user + 2% kernel\n"
        self.assertIsNone(meminfo.parse_cpuinfo(text, "com.example.app"),
                          "com.example.app must not claim .dev's line")


class SeriesMathTests(unittest.TestCase):
    def _samples(self) -> list[dict]:
        out = []
        for index in (1, 2, 3):
            text = (ROOT / f"tests/fixtures/meminfo-{index}.txt").read_text(
                encoding="utf-8")
            out.append({"metrics": meminfo.parse_meminfo(text)})
        return out

    def test_summary_matches_the_skill_script_on_the_fixtures(self) -> None:
        ours = series.summarize(self._samples(), 1024)
        theirs = SKILL.summarize(self._samples(), 1024)
        self.assertEqual(ours["directional_growth_leads"],
                         theirs["directional_growth_leads"])
        for name, info in ours["metrics"].items():
            twin = theirs["metrics"][name]
            for key in ("samples", "delta", "decreases", "directional_growth"):
                self.assertAlmostEqual(info[key], twin[key], msg=f"{name}.{key}")
            self.assertAlmostEqual(info["slope_per_capture"],
                                   twin["slope_per_capture"])
        self.assertEqual(ours["interpretation"], theirs["interpretation"])

    def test_flatten_snapshot_keeps_numbers_and_drops_the_rest(self) -> None:
        flat = series.flatten_snapshot({
            "memory": {"total_pss_kb": 9000, "source": "x"},
            "cpu": {"available": True, "process_percent": 12.5},
            "proc": {"threads": 42},
            "disk": {},
            "limitations": ["text"],
        })
        self.assertEqual(flat, {"total_pss_kb": 9000.0,
                                "process_percent": 12.5, "threads": 42.0})

    def test_from_dir_orders_by_mtime_and_flags_growth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for index, rss in enumerate((100_000_000, 150_000_000, 220_000_000)):
                path = base / f"{index}-x-snapshot.json"
                path.write_text(json.dumps(
                    {"memory": {"rss_bytes": rss}}), encoding="utf-8")
                stamp = time.time() + index
                os.utime(path, (stamp, stamp))
            samples = series.from_dir(base, "*-snapshot.json")
            report = series.summarize(samples, 1024)
            self.assertIn("rss_bytes", report["directional_growth_leads"])


class AndroidSnapshotCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({
            "ui_dump": str(UI_DUMP),
            "pidof": {"com.example.app": "4321"},
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
        })
        result = self._cli("session", "start", "--app-id", "com.example.app")
        assert result.returncode == 0, result.stderr
        self.artifacts = Path(json.loads(result.stdout)["session"]["artifacts_dir"])

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554", "--adb", str(FAKE_ADB), *args],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=120,
        )

    def test_snapshot_reports_memory_cpu_proc_and_writes_artifacts(self) -> None:
        result = self._cli("metrics", "snapshot", "--label", "baseline")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["pid"], 4321)
        self.assertEqual(payload["memory"]["total_pss_kb"], 9000)
        self.assertEqual(payload["cpu"], {
            "available": True, "process_percent": 12.5,
            "note": payload["cpu"]["note"]})
        self.assertEqual(payload["proc"]["threads"], 42)
        self.assertEqual(payload["metric_semantics"], "android_dumpsys_meminfo_v1")
        self.assertNotIn("raw_meminfo", payload)
        artifacts = [Path(a) for a in payload["artifacts"]]
        self.assertTrue(all(a.is_file() for a in artifacts), artifacts)
        self.assertTrue(any(a.name.endswith("-baseline-snapshot.json")
                            for a in artifacts))
        # `.raw.txt`, never `-meminfo.txt`: `metrics memory analyze` globs
        # `*-meminfo.txt` for capture packs, and a snapshot must not fold in
        self.assertTrue(any(a.name.endswith("-baseline-meminfo.raw.txt")
                            for a in artifacts))
        self.assertTrue(str(artifacts[0]).startswith(str(self.artifacts)))

    def test_snapshot_without_running_app_names_what_was_tried(self) -> None:
        result = self._cli("metrics", "snapshot", "--app-id", "com.dead.app")
        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stderr)
        self.assertEqual(envelope["error_code"], "app_not_running")
        self.assertIn("adb shell pidof -s", envelope["sources_tried"])

    def test_series_live_takes_count_snapshots_and_writes_summary(self) -> None:
        result = self._cli("metrics", "series", "--count", "3",
                           "--interval", "0", "--label", "scroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["sample_count"], 3)
        self.assertEqual(payload["metrics"]["total_pss_kb"]["samples"], 3)
        # identical fixture snapshots can never be a lead
        self.assertEqual(payload["directional_growth_leads"], [])
        self.assertIn("Directional trend only", payload["interpretation"])
        self.assertTrue(Path(payload["artifact"]).is_file())
        # same-second snapshots must not overwrite each other's artifacts
        sample_artifacts = [s["artifact"] for s in payload["samples"]]
        self.assertEqual(len(set(sample_artifacts)), 3, sample_artifacts)
        self.assertTrue(all(Path(a).is_file() for a in sample_artifacts))

    def test_series_from_dir_is_offline(self) -> None:
        src = Path(self.tmp.name) / "snaps"
        src.mkdir()
        for index, pss in enumerate((9000, 12000, 16000)):
            path = src / f"{index}-x-snapshot.json"
            path.write_text(json.dumps({"memory": {"total_pss_kb": pss}}),
                            encoding="utf-8")
            stamp = time.time() + index
            os.utime(path, (stamp, stamp))
        result = self._cli("metrics", "series", "--from-dir", str(src))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["directional_growth_leads"], ["total_pss_kb"])

    def test_list_presets_is_deterministic_with_fakes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--adb", str(FAKE_ADB), "--simctl", str(FAKE_SIMCTL),
             "metrics", "list-presets"],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        rows = {row["id"]: row for row in payload["presets"]}
        self.assertTrue(rows["simpleperf"]["available"])
        self.assertFalse(rows["allocations"]["available"])
        self.assertEqual(rows["allocations"]["reason"], "ios_only")
        self.assertFalse(payload["tools"]["xctrace"])  # fake xcrun refuses


class IosSnapshotCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        container = Path(self.tmp.name) / "container"
        container.mkdir()
        (container / "data.bin").write_bytes(b"x" * 4096)
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({
            "installed": ["com.example.app"],
            "running": ["com.example.app"],
            "container": str(container),
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
        })

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "ios", "--target", UDID,
             "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB), *args],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=120,
        )

    def test_ios_snapshot_is_host_accounting_and_says_so(self) -> None:
        result = self._cli("metrics", "snapshot", "--app-id", "com.example.app")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # fake launchctl reports pid 1, which exists on every host
        self.assertEqual(payload["pid"], 1)
        self.assertEqual(payload["metric_semantics"],
                         "ios_simulator_host_process_v1")
        self.assertGreater(payload["memory"]["rss_bytes"], 0)
        self.assertGreaterEqual(payload["disk"]["data_container_bytes"], 4096)
        self.assertTrue(any("host view" in line
                            for line in payload["limitations"]))

    def test_dead_app_refuses_instead_of_measuring_the_cli_itself(self) -> None:
        # our own argv contains `--app-id com.example.app`, so any free-text
        # process-name fallback would "find" the autonom CLI and measure it
        state = json.loads(Path(self.env["AUTONOM_FAKE_STATE"]).read_text())
        state["running"] = []
        Path(self.env["AUTONOM_FAKE_STATE"]).write_text(json.dumps(state))
        result = self._cli("metrics", "snapshot", "--app-id", "com.example.app")
        self.assertEqual(result.returncode, 2, result.stdout)
        envelope = json.loads(result.stderr)
        self.assertEqual(envelope["error_code"], "app_not_running")
        self.assertEqual(envelope["sources_tried"],
                         ["simctl spawn launchctl list"])


class DoctorMetricsTests(unittest.TestCase):
    def test_doctor_reports_the_metrics_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["AUTONOM_HOME"] = tmp
            result = subprocess.run(
                [sys.executable, str(CLI), "--adb", str(FAKE_ADB),
                 "--simctl", str(FAKE_SIMCTL), "doctor"],
                env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload["metrics"]), {
            "android_meminfo", "ios_host_ps", "ios_xctrace",
            "flutter_frame_summary"})
        self.assertTrue(payload["metrics"]["android_meminfo"])
        self.assertFalse(payload["metrics"]["ios_xctrace"])


if __name__ == "__main__":
    unittest.main()
