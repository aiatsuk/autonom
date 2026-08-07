from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "plugins/autonom/skills/android-debugger-agent/scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from ui_common import filter_nodes, parse_bounds, parse_nodes, summarize  # noqa: E402
from ui_query import select_matches  # noqa: E402


class UiCommonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = (ROOT / "tests/fixtures/ui_dump.xml").read_text(encoding="utf-8")
        cls.nodes = parse_nodes(cls.fixture)

    def test_trims_adb_noise_and_parses_nodes(self) -> None:
        self.assertEqual(len(self.nodes), 6)
        self.assertEqual(self.nodes[1].text, "Settings")
        self.assertEqual(self.nodes[3].description, "Flutter Save Button")

    def test_bounds_helpers(self) -> None:
        bounds = parse_bounds("[10,20][110,220]")
        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertEqual(bounds.center, (60, 120))
        self.assertEqual(bounds.width, 100)
        self.assertEqual(bounds.height, 200)

    def test_exact_matches_are_case_insensitive_by_default(self) -> None:
        matches = filter_nodes(self.nodes, {"text": "settings"})
        self.assertEqual(len(matches), 2)

    def test_contains_semantics_description(self) -> None:
        matches = filter_nodes(self.nodes, {"desc": "save"}, mode="contains")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].bounds.center, (300, 360))

    def test_regex_and_boolean_selectors(self) -> None:
        matches = filter_nodes(
            self.nodes,
            {"resource_id": r"settings(_secondary)?$", "clickable": True},
            mode="regex",
        )
        self.assertEqual(len(matches), 2)
        disabled = filter_nodes(self.nodes, {"enabled": False})
        self.assertEqual([node.text for node in disabled], ["Search input"])

    def test_duplicate_requires_index(self) -> None:
        matches = filter_nodes(self.nodes, {"text": "Settings"})
        with self.assertRaises(RuntimeError):
            select_matches(matches, None, False)
        selected = select_matches(matches, -1, False)
        self.assertEqual(selected[0].resource_id, "com.example.app:id/settings_secondary")

    def test_summary_contains_flutter_semantics(self) -> None:
        rendered = "\n".join(summarize(self.nodes))
        self.assertIn('desc="Flutter Save Button"', rendered)
        self.assertIn("flags=clickable,focusable", rendered)

    def test_cli_returns_center_and_json(self) -> None:
        script = SCRIPT_DIR / "ui_query.py"
        fixture = ROOT / "tests/fixtures/ui_dump.xml"
        center = subprocess.run(
            [sys.executable, str(script), str(fixture), "--desc", "Flutter Save Button"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        self.assertEqual(center.stdout.strip(), "300 360")

        result = subprocess.run(
            [
                sys.executable,
                str(script),
                str(fixture),
                "--text",
                "Settings",
                "--index",
                "1",
                "--json",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["match_count"], 2)
        self.assertEqual(payload["nodes"][0]["bounds"]["center_x"], 740)


if __name__ == "__main__":
    unittest.main()
