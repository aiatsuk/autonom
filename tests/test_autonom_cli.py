from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"


class AutonomCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_version(self) -> None:
        result = self._run("version")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "autonom")
        self.assertIn("version", payload)

    def test_ui_tree_from_fixture(self) -> None:
        result = self._run("ui", "tree", "--dump", str(FIXTURE))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 4)
        texts = {node.get("text") for node in payload["nodes"]}
        self.assertIn("Settings", texts)
        refs = {node["ref"] for node in payload["nodes"]}
        self.assertTrue(any(ref.startswith("n") for ref in refs))

    def test_ui_find_desc(self) -> None:
        result = self._run(
            "ui",
            "find",
            "--dump",
            str(FIXTURE),
            "--desc",
            "Flutter Save Button",
            "--mode",
            "exact",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 1)
        node = payload["matches"][0]
        self.assertEqual(node["bounds"], [100, 300, 500, 420])
        self.assertTrue(node["clickable"])

    def test_ui_find_duplicate_requires_index(self) -> None:
        result = self._run("ui", "find", "--dump", str(FIXTURE), "--text", "Settings", "--mode", "exact")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("matched", result.stderr.lower() + result.stdout.lower())

    def test_compact_module_filters_meaningful(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        from autonom_lib.ui import parse_compact_tree  # noqa: WPS433

        nodes = parse_compact_tree(FIXTURE.read_text(encoding="utf-8"), meaningful_only=True)
        self.assertTrue(any(n.get("desc") == "Flutter Save Button" for n in nodes))
        # FrameLayout root with no labels should be omitted when meaningful_only
        self.assertFalse(any(n.get("role") == "framelayout" and not n.get("text") for n in nodes))

    def test_session_artifacts_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            # Sessions are global by default; an explicit cwd forces the legacy
            # project-local layout, which is what this roundtrip exercises.
            sys.path.insert(0, str(ROOT / "scripts"))
            from autonom_lib import session as session_mod  # noqa: WPS433

            record = session_mod.start_session("adb", serial="emulator-5554", app_id="com.example", cwd=cwd)
            self.assertTrue(Path(record["artifacts_dir"]).is_dir())
            self.assertTrue((cwd / ".autonom" / "current.json").exists())
            loaded = session_mod.load_current(cwd)
            self.assertEqual(loaded["serial"], "emulator-5554")
            stopped = session_mod.stop_session(cwd)
            self.assertIsNotNone(stopped)
            self.assertFalse((cwd / ".autonom" / "current.json").exists())


if __name__ == "__main__":
    unittest.main()
