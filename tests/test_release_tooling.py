"""Release tooling contracts.

`scripts/build_release.sh` used to re-parse the version with its own grep — a
second parser of the same file that breaks the moment any quoted version-like
triple precedes `__version__`. It now resolves the version by importing
`autonom_lib`, the same answer `validate_plugin.py` trusts. These tests pin
that the two resolvers agree, so the build can never tag a different version
than validation checks.
"""
from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import autonom_lib  # noqa: E402


def _load_validate_plugin():
    spec = importlib.util.spec_from_file_location(
        "validate_plugin", ROOT / "scripts/validate_plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclass resolution looks the module up in sys.modules on 3.14+.
    sys.modules["validate_plugin"] = module
    spec.loader.exec_module(module)
    return module


class VersionResolverTests(unittest.TestCase):
    def test_import_resolver_agrees_with_validate_plugin(self) -> None:
        validate_plugin = _load_validate_plugin()
        problems: list = []
        regex_version = validate_plugin.read_lib_version(ROOT, problems)
        self.assertEqual(problems, [])
        self.assertEqual(regex_version, autonom_lib.__version__)

    def test_build_release_print_version_is_the_single_resolver(self) -> None:
        """release.yml calls this flag; it must agree with the library."""
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/build_release.sh"), "--print-version"],
            cwd=ROOT, text=True, capture_output=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), autonom_lib.__version__)
        script = (ROOT / "scripts/build_release.sh").read_text(encoding="utf-8")
        self.assertNotIn("grep -oE", script, "the brittle grep parser must not return")
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("--print-version", workflow,
                      "release.yml must use the shared resolver, not its own copy")


class BundlePreflightTests(unittest.TestCase):
    def test_required_release_files_exist(self) -> None:
        """build_release.sh pre-flights these; keep them present in the repo."""
        for name in ("LICENSE", "CHANGELOG.md", "README.md", "install.sh"):
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).exists(), f"{name} is required by build_release.sh")

    def test_directory_png_icon_is_bundled(self) -> None:
        """The directory reads this image once, at the first saved listing."""
        plugin = ROOT / "plugins/autonom"
        manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
        reference = manifest["icon"]
        self.assertTrue(reference.startswith("./"))
        icon = (plugin / reference).resolve()
        self.assertTrue(icon.is_relative_to(plugin.resolve()))
        self.assertTrue(icon.is_file())
        data = icon.read_bytes()
        self.assertLess(len(data), 2 * 1024 * 1024)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data[12:16], b"IHDR")
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 512)
        self.assertLessEqual(width, 2048)

    def test_directory_links_use_https(self) -> None:
        plugin = ROOT / "plugins/autonom"
        manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
        for field in ("documentationUrl", "supportUrl", "privacyPolicyUrl"):
            with self.subTest(field=field):
                url = urlsplit(manifest[field])
                self.assertEqual(url.scheme, "https")
                self.assertTrue(url.hostname)
                self.assertIsNone(url.username)
                self.assertIsNone(url.password)


if __name__ == "__main__":
    unittest.main()
