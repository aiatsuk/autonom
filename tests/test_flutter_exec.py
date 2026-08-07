from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "flutter_exec.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("flutter_exec_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


class ResolveFlutterTests(unittest.TestCase):
    def test_local_fvm_sdk_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / ".fvm" / "flutter_sdk" / "bin" / "flutter"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertEqual(MODULE.resolve_flutter(root), [str(binary)])

    def test_fvm_wrapper_when_repo_is_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            MODULE.shutil, "which"
        ) as which:
            root = Path(tmp)
            (root / ".fvmrc").write_text('{"flutter":"stable"}\n', encoding="utf-8")
            which.side_effect = lambda name: "/usr/bin/fvm" if name == "fvm" else None
            self.assertEqual(MODULE.resolve_flutter(root), ["fvm", "flutter"])

    def test_no_silent_system_flutter_for_fvm_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            MODULE.shutil, "which"
        ) as which:
            root = Path(tmp)
            (root / ".fvmrc").write_text('{"flutter":"stable"}\n', encoding="utf-8")
            which.side_effect = (
                lambda name: "/usr/bin/flutter" if name == "flutter" else None
            )
            with self.assertRaisesRegex(RuntimeError, "FVM-pinned"):
                MODULE.resolve_flutter(root)
            self.assertEqual(
                MODULE.resolve_flutter(root, allow_system_with_fvm=True),
                ["/usr/bin/flutter"],
            )


if __name__ == "__main__":
    unittest.main()
