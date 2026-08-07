from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/autonom/skills/flutter-performance-audit/scripts/frame_timings_summary.py"
SPEC = importlib.util.spec_from_file_location("frame_timings_summary", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FrameTimingTests(unittest.TestCase):
    def test_percentile_interpolates(self) -> None:
        self.assertEqual(MODULE.percentile([0.0, 10.0], 0.5), 5.0)
        self.assertAlmostEqual(MODULE.percentile([1.0, 2.0, 3.0, 4.0], 0.9), 3.7)

    def test_summary_counts_budget_violations(self) -> None:
        result = MODULE.summarize([4.0, 8.0, 20.0, 40.0], 16.67)
        self.assertEqual(result["frames"], 4)
        self.assertEqual(result["over_budget"], 2)
        self.assertEqual(result["over_budget_percent"], 50.0)
        self.assertEqual(result["worst_ms"], 40.0)

    def test_cli_finds_nested_flutter_timing_arrays(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), str(ROOT / "tests/fixtures/frame_timings.json"), "--json"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["build"]["frames"], 5)
        self.assertEqual(payload["build"]["over_budget"], 2)
        self.assertEqual(payload["raster"]["worst_ms"], 30.0)


if __name__ == "__main__":
    unittest.main()
