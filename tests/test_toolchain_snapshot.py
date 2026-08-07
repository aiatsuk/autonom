from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "toolchain_snapshot.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("toolchain_snapshot_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


class ToolchainSnapshotTests(unittest.TestCase):
    def test_reads_flutter_and_android_pins_without_latest_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pubspec.yaml").write_text(
                "environment:\n"
                "  sdk: '>=3.6.0 <4.0.0'\n"
                "  flutter: '>=3.29.0'\n"
                "dependencies:\n"
                "  flutter:\n"
                "    sdk: flutter\n",
                encoding="utf-8",
            )
            (root / "android" / "app").mkdir(parents=True)
            (root / "android" / "gradle" / "wrapper").mkdir(parents=True)
            (root / "android" / "settings.gradle.kts").write_text(
                'plugins {\n'
                '  id("com.android.application") version "9.2.0" apply false\n'
                '  id("org.jetbrains.kotlin.android") version "2.3.21" apply false\n'
                "}\n",
                encoding="utf-8",
            )
            (root / "android" / "app" / "build.gradle.kts").write_text(
                "android {\n"
                "  compileSdk = 36\n"
                "  defaultConfig {\n"
                "    minSdk = 24\n"
                "    targetSdk = 36\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "android" / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/"
                "gradle-9.4.1-bin.zip\n",
                encoding="utf-8",
            )

            snapshot = MODULE.inspect(root, execute=False)
            self.assertTrue(snapshot["project"]["flutter"])
            self.assertTrue(snapshot["project"]["android"])
            self.assertEqual(snapshot["declared"]["gradle"], "9.4.1")
            self.assertEqual(snapshot["declared"]["android_gradle_plugin"], "9.2.0")
            self.assertEqual(snapshot["declared"]["kotlin"], "2.3.21")
            self.assertEqual(snapshot["declared"]["compile_sdk"], "36")
            self.assertIn("no value is asserted to be latest", snapshot["policy"])

    def test_text_renderer_is_stable(self) -> None:
        snapshot = {
            "root": "/tmp/example",
            "project": {"flutter": True, "dart": True, "android": True},
            "declared": {"dart_sdk": ">=3.6.0", "flutter_sdk": None},
            "policy": "Observed only.",
        }
        text = MODULE.render_text(snapshot)
        self.assertIn("Detected: flutter, dart, android", text)
        self.assertIn("flutter_sdk: <not found>", text)
        self.assertTrue(text.endswith("Observed only."))


if __name__ == "__main__":
    unittest.main()
