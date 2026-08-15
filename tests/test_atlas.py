"""Atlas-lite (§10): fingerprint invariance and the observed graph cycle."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib.atlas import fingerprint, graph as atlas_graph  # noqa: E402


def _node(**kwargs) -> dict:
    base = {"ref": "n", "role": "text", "depth": 1, "enabled": True}
    base.update(kwargs)
    return base


class FingerprintTests(unittest.TestCase):
    def test_volatile_text_does_not_change_identity_or_variant(self) -> None:
        stable = [_node(ref="n1", resource_id="app:id/title", text="Inbox"),
                  _node(ref="n2", resource_id="app:id/clock", text="12:04")]
        later = [_node(ref="n1", resource_id="app:id/title", text="Inbox"),
                 _node(ref="n2", resource_id="app:id/clock", text="18:37")]
        first, second = fingerprint.fingerprint(stable), fingerprint.fingerprint(later)
        self.assertEqual(first["structure"], second["structure"])
        self.assertEqual(first["state"], second["state"],
                         "a clock tick must not create a variant")

    def test_stable_text_change_is_a_variant_not_a_new_screen(self) -> None:
        logged_out = [_node(ref="n1", resource_id="app:id/cta", text="Sign in")]
        logged_in = [_node(ref="n1", resource_id="app:id/cta", text="Sign out")]
        first = fingerprint.fingerprint(logged_out)
        second = fingerprint.fingerprint(logged_in)
        self.assertEqual(first["structure"], second["structure"])
        self.assertNotEqual(first["state"], second["state"])

    def test_list_length_does_not_change_identity(self) -> None:
        def item(index: int) -> dict:
            return _node(ref=f"i{index}", resource_id="app:id/row",
                         role="text", text=f"Order #{1000 + index}", depth=2)
        short = [_node(ref="n1", resource_id="app:id/list", role="list")] + \
            [item(i) for i in range(3)]
        long = [_node(ref="n1", resource_id="app:id/list", role="list")] + \
            [item(i) for i in range(30)]
        self.assertEqual(fingerprint.fingerprint(short)["structure"],
                         fingerprint.fingerprint(long)["structure"])

    def test_system_ui_is_excluded(self) -> None:
        app_only = [_node(ref="n1", resource_id="app:id/x", text="Hello")]
        with_bar = app_only + [_node(ref="s1", package="com.android.systemui",
                                     text="Battery 97%")]
        self.assertEqual(fingerprint.fingerprint(app_only)["structure"],
                         fingerprint.fingerprint(with_bar)["structure"])

    def test_labels_prefer_stable_text(self) -> None:
        nodes = [_node(ref="n1", text="12:04"),
                 _node(ref="n2", text="Checkout")]
        self.assertEqual(fingerprint.fingerprint(nodes)["labels"], ["Checkout"])


class AtlasCycleTests(unittest.TestCase):
    """Two flow runs → update → show/coverage/paths/export/diff."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        state = self.root / "state.json"
        state.write_text(json.dumps({"ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(self.root / "home"),
            "AUTONOM_FAKE_STATE": str(state),
            "AUTONOM_FAKE_LOG": str(self.root / "log.jsonl"),
        })
        result = self._cli("session", "start", "--app-id", "com.example.app")
        assert result.returncode == 0, result.stderr
        flow = self.root / "flow.yaml"
        flow.write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: nav\n---\n"
            "- assertVisible:\n    selector:\n      text: Settings\n"
            "      match: contains\n"
            "- tapOn:\n    selector:\n      description: Open settings\n"
            "      match: exact\n"
            "- assertVisible:\n    selector:\n"
            "      id: com.example.app:id/search\n", encoding="utf-8")
        for _ in range(2):  # a repeated visit must not duplicate screens
            result = self._cli("flow", "run", str(flow))
            assert result.returncode == 0, result.stderr

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554",
             "--adb", str(ROOT / "tests/fakes/fake_adb.py"), *args],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=120,
        )

    def test_update_show_coverage_export_diff(self) -> None:
        result = self._cli("atlas", "update")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["runs_ingested"], 2)
        self.assertEqual(payload["screens"], 1,
                         "the static fixture is one screen — twice ingested, "
                         "never duplicated")

        shown = json.loads(self._cli("atlas", "show").stdout)
        self.assertEqual(shown["screens"], 1)
        self.assertTrue(shown["screen_list"][0]["labels"])

        covered = json.loads(self._cli("atlas", "coverage").stdout)
        self.assertIn("note", covered)
        self.assertEqual(covered["observed_screens"], 1)

        base = self.root / "base.json"
        exported = self._cli("atlas", "export", "--out", str(base))
        self.assertEqual(exported.returncode, 0, exported.stderr)
        diffed = json.loads(self._cli("atlas", "diff", "--base",
                                      str(base)).stdout)
        self.assertEqual(diffed["screens_added"], [])
        self.assertEqual(diffed["screens_removed"], [])

    def test_manual_session_details_also_ingest(self) -> None:
        result = self._cli("ui", "tap", "--desc", "Open settings",
                           "--mode", "exact")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._cli("atlas", "update")
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["screens"], 1)


if __name__ == "__main__":
    unittest.main()
