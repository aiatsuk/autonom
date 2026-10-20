"""The directory's per-file scan limit must fail locally, before submission."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_plugin import check_file_sizes


class PluginFileSizeTests(unittest.TestCase):
    def test_boundary_and_nested_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "skills").mkdir()
            for name, size in [("below.mjs", 262143), ("equal.mjs", 262144), ("above.md", 262145)]:
                (root / "skills" / name).write_bytes(b"x" * size)
            problems = []
            check_file_sizes(root, problems)
            self.assertEqual([p.path.name for p in problems], ["above.md", "equal.mjs"])
            self.assertTrue(all("256 KiB" in p.message for p in problems))

    def test_media_exemptions_and_empty_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            problems = []
            check_file_sizes(root, problems)
            self.assertEqual(problems, [])
            for extension in [".png", ".JPEG", ".gif", ".webp", ".woff2", ".ttf", ".svg"]:
                (root / ("asset" + extension)).write_bytes(b"x" * 262144)
            check_file_sizes(root, problems)
            # SVG is readable source and still needs to fit the text scan limit.
            self.assertEqual([p.path.name for p in problems], ["asset.svg"])

    def test_unreadable_file_is_reported_without_skipping_other_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bad = root / "bad.mjs"
            bad.write_text("source")
            (root / "large.mjs").write_bytes(b"x" * 262144)
            original = Path.stat

            def stat(path, *args, **kwargs):
                if path == bad:
                    raise PermissionError("fixture denied")
                return original(path, *args, **kwargs)

            problems = []
            with patch.object(Path, "stat", stat):
                check_file_sizes(root, problems)
            self.assertEqual([p.path.name for p in problems], ["bad.mjs", "large.mjs"])
            self.assertIn("cannot inspect", problems[0].message)


if __name__ == "__main__":
    unittest.main()
