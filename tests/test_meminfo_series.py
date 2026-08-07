from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/autonom/skills/android-memory-leaks/scripts/analyze_meminfo_series.py"
SPEC = importlib.util.spec_from_file_location("analyze_meminfo_series", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MeminfoSeriesTests(unittest.TestCase):
    def test_parser_extracts_summary_and_object_metrics(self) -> None:
        text = (ROOT / "tests/fixtures/meminfo-1.txt").read_text(encoding="utf-8")
        metrics = MODULE.parse_meminfo(text)
        self.assertEqual(metrics["total_pss_kb"], 9000)
        self.assertEqual(metrics["java_heap_kb"], 4000)
        self.assertEqual(metrics["activities"], 1)
        self.assertEqual(metrics["views"], 20)

    def test_report_flags_directional_leads_without_claiming_a_leak(self) -> None:
        samples = []
        for index in range(1, 4):
            path = ROOT / f"tests/fixtures/meminfo-{index}.txt"
            samples.append({"path": str(path), "metrics": MODULE.parse_meminfo(path.read_text())})
        report = MODULE.summarize(samples, min_growth_kb=1024)
        self.assertIn("total_pss_kb", report["directional_growth_leads"])
        self.assertIn("java_heap_kb", report["directional_growth_leads"])
        self.assertIn("requires a retained object", report["interpretation"])

    def test_cli_accepts_a_directory_and_emits_json(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(ROOT / "tests/fixtures"),
                "--glob",
                "meminfo-*.txt",
                "--json",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        report = json.loads(completed.stdout)
        self.assertEqual(report["sample_count"], 3)
        self.assertEqual(report["metrics"]["total_pss_kb"]["delta"], 2400)


if __name__ == "__main__":
    unittest.main()
