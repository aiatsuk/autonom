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
import stat
import subprocess
import sys
import unittest
from pathlib import Path

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


class AndroidSmokeScriptTests(unittest.TestCase):
    def test_script_matches_session_start_contract(self) -> None:
        script_path = ROOT / "scripts/ci/android_smoke.sh"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn('p["session"]["session_id"]', script)
        self.assertNotIn('p["session_id"]', script)

    def test_script_is_executable(self) -> None:
        mode = (ROOT / "scripts/ci/android_smoke.sh").stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
