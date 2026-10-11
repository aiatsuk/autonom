"""Pre-test diff context (tp-002): hunks, renames, binaries, truncation,
flow selection and Runtime Map hints."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.pretest import context as context_mod  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x00IEND\xaeB`\x82")

FLOW_COVERS = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Cart covered
properties:
  covers: "lib/cart/*"
---
- assertVisible:
    selector:
      text: Cart
"""

FLOW_BROKEN = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Broken
---
- notACommand: {}
"""


def a_dart(changed: bool) -> str:
    lines = ["// header", "", "class Cart {", "  int total() {"]
    lines += [f"    var v{i} = {i};" for i in range(5, 15)]
    lines += ["  }", "}"]
    if changed:
        for number in (10, 11, 12):
            lines[number - 1] = f"    var v{number - 1} = {number * 100};"
    return "\n".join(lines) + "\n"


def b_dart() -> str:
    return "".join(f"// line {i} of a file that keeps its content when moved\n"
                   for i in range(30))


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.root / "home")})
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})
        return completed.stdout

    def init(self, name: str = "repo") -> Path:
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        return repo

    def write(self, repo: Path, rel: str, content: str | bytes) -> None:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

    def commit(self, repo: Path, message: str) -> None:
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message)

    def feature_repo(self) -> Path:
        repo = self.init()
        self.write(repo, "lib/cart/a.dart", a_dart(False))
        self.write(repo, "lib/b.dart", b_dart())
        self.write(repo, "lib/old.dart", "gone\n")
        self.commit(repo, "base")
        self.git(repo, "checkout", "-q", "-b", "feature")
        self.write(repo, "lib/cart/a.dart", a_dart(True))
        self.git(repo, "mv", "lib/b.dart", "lib/c.dart")
        self.write(repo, "assets/icon.png", PNG)
        self.git(repo, "rm", "-q", "lib/old.dart")
        self.commit(repo, "feature")
        return repo


class DiffFilesTests(RepoCase):
    def test_tp002_s01_hunks_rename_binary(self) -> None:
        repo = self.feature_repo()
        for head in ("feature", None):
            with self.subTest(head=head):
                result = context_mod.diff_files(repo, "main", head)
                files = {item["path"]: item for item in result["changed_files"]}
                self.assertFalse(result["diff_truncated"])
                self.assertEqual(files["lib/cart/a.dart"]["status"], "M")
                self.assertEqual(files["lib/cart/a.dart"]["ranges"], [[10, 12]])
                self.assertFalse(files["lib/cart/a.dart"]["binary"])
                self.assertEqual(files["lib/cart/a.dart"]["functions"], ["class Cart {"])
                self.assertEqual(files["lib/c.dart"]["status"], "R")
                self.assertEqual(files["lib/c.dart"]["renamed_from"], "lib/b.dart")
                self.assertNotIn("lib/b.dart", files)
                self.assertTrue(files["assets/icon.png"]["binary"])
                self.assertEqual(files["assets/icon.png"]["status"], "A")
                self.assertEqual(files["assets/icon.png"]["ranges"], [])
                self.assertEqual(files["lib/old.dart"]["status"], "D")
                self.assertEqual(files["lib/old.dart"]["ranges"], [])
                self.assertNotIn("renamed_from", files["lib/cart/a.dart"])

    def test_tp002_s01_empty_diff(self) -> None:
        repo = self.init()
        self.write(repo, "a.txt", "x\n")
        self.commit(repo, "only")
        self.assertEqual(context_mod.diff_files(repo, "main", None),
                         {"changed_files": [], "diff_truncated": False})
        self.assertEqual(context_mod.diff_files(repo, "main", "main")["changed_files"], [])

    def test_untracked_files_count_against_the_working_tree_only(self) -> None:
        repo = self.feature_repo()
        self.write(repo, "lib/new.dart", "a\nb\nc\n")
        self.write(repo, "lib/new.bin", b"\x00\x01\x02")
        files = {item["path"]: item
                 for item in context_mod.diff_files(repo, "main", None)["changed_files"]}
        self.assertEqual(files["lib/new.dart"]["status"], "A")
        self.assertEqual(files["lib/new.dart"]["ranges"], [[1, 3]])
        self.assertTrue(files["lib/new.bin"]["binary"])
        with_head = {item["path"] for item in
                     context_mod.diff_files(repo, "main", "feature")["changed_files"]}
        self.assertNotIn("lib/new.dart", with_head)

    def test_tp002_s02_truncation_keeps_every_file(self) -> None:
        repo = self.init()
        for index in range(6):
            self.write(repo, f"src/f{index}.txt", "".join(f"old {n}\n" for n in range(200)))
        self.commit(repo, "base")
        for index in range(6):
            self.write(repo, f"src/f{index}.txt", "".join(f"new {n}\n" for n in range(200)))
        self.write(repo, "src/untracked.txt", "u\n" * 50)
        result = context_mod.diff_files(repo, "main", None, cap_bytes=2000)
        self.assertTrue(result["diff_truncated"])
        names = [item["path"] for item in result["changed_files"]]
        self.assertEqual(sorted(names),
                         sorted([f"src/f{i}.txt" for i in range(6)] + ["src/untracked.txt"]))
        self.assertEqual(result["changed_files"][-1]["ranges"], [])
        full = context_mod.diff_files(repo, "main", None)
        self.assertFalse(full["diff_truncated"])
        self.assertEqual(full["changed_files"][0]["ranges"], [[1, 200]])

    def test_unusual_paths(self) -> None:
        repo = self.init()
        self.write(repo, "lib/корзина экран.dart", "a\n")
        self.commit(repo, "base")
        self.write(repo, "lib/корзина экран.dart", "b\n")
        files = context_mod.diff_files(repo, "main", None)["changed_files"]
        self.assertEqual(files, [{"path": "lib/корзина экран.dart", "status": "M",
                                  "binary": False, "ranges": [[1, 1]], "functions": []}])
        self.assertEqual(context_mod._unquote('"b/a\\tb\\303\\251"'), "b/a\tbé")  # noqa: SLF001
        self.assertEqual(context_mod._unquote("plain"), "plain")  # noqa: SLF001

    def test_diff_prefix_settings_do_not_change_the_ranges(self) -> None:
        repo = self.feature_repo()
        expected = context_mod.diff_files(repo, "main", None)
        settings = [("diff.mnemonicPrefix", "true"), ("diff.noprefix", "true"),
                    ("diff.dstPrefix", "new/"), ("diff.srcPrefix", "old/")]
        for key, value in settings:
            with self.subTest(setting=key):
                self.git(repo, "config", key, value)
                try:
                    for head in ("feature", None):
                        result = context_mod.diff_files(repo, "main", head)
                        files = {item["path"]: item for item in result["changed_files"]}
                        self.assertEqual(files["lib/cart/a.dart"]["ranges"], [[10, 12]])
                        self.assertEqual(files["lib/cart/a.dart"]["functions"],
                                         ["class Cart {"])
                    self.assertEqual(context_mod.diff_files(repo, "main", None), expected)
                finally:
                    self.git(repo, "config", "--unset", key)

    def test_an_added_line_starting_with_plus_plus_stays_content(self) -> None:
        repo = self.init()
        self.write(repo, "a.c", "one\ntwo\nthree\n")
        self.write(repo, "b.c", "x\n")
        self.commit(repo, "base")
        # line 2 becomes "++ count;" (shown as "+++ count;" in the hunk), line 4 is added
        self.write(repo, "a.c", "one\n++ count;\nthree\nfour\n")
        self.write(repo, "b.c", "x\n+++ b/b.c\n")
        result = context_mod.diff_files(repo, "main", None)
        files = {item["path"]: item for item in result["changed_files"]}
        self.assertEqual(files["a.c"]["ranges"], [[2, 2], [4, 4]])
        self.assertEqual(files["b.c"]["ranges"], [[2, 2]])
        self.assertEqual(sorted(files), ["a.c", "b.c"])

    def test_function_context_is_masked_before_it_is_kept(self) -> None:
        """Contract 8.3 item 3: git's function context is the nearest top-level
        line above a hunk, which in a .env or settings file is a credential."""
        repo = self.init()
        long_header = "def handler_" + "x" * 300 + "(password=None):"
        self.write(repo, ".env", "DB_PASSWORD=hunter2\nX=1\n")
        self.write(repo, "settings.py",
                   'SECRET_KEY = "django-insecure-abc123xyz"\n    DEBUG = False\n')
        self.write(repo, "auth.txt", "Authorization: Bearer abc.def\n  a = 1\n")
        self.write(repo, "long.py", long_header + "\n    a = 1\n")
        self.commit(repo, "base")
        self.write(repo, ".env", "DB_PASSWORD=hunter2\nX=2\n")
        self.write(repo, "settings.py",
                   'SECRET_KEY = "django-insecure-abc123xyz"\n    DEBUG = True\n')
        self.write(repo, "auth.txt", "Authorization: Bearer abc.def\n  a = 2\n")
        self.write(repo, "long.py", long_header + "\n    a = 2\n")
        result = context_mod.diff_files(repo, "HEAD", None)
        files = {item["path"]: item for item in result["changed_files"]}
        # data and text files keep no function context at all (fix round 3)
        self.assertEqual(files[".env"]["functions"], [])
        self.assertEqual(files["auth.txt"]["functions"], [])
        self.assertEqual(files["settings.py"]["functions"], ["SECRET_KEY = <redacted>"])
        # git cuts the header itself; what is kept stays within the limit
        [function] = files["long.py"]["functions"]
        self.assertTrue(long_header.startswith(function))
        self.assertLessEqual(len(function), context_mod.FUNCTION_LIMIT)
        context = context_mod.build(repo, "HEAD", None, None, None, None)
        dumped = json.dumps(context)
        for leak in ("hunter2", "django-insecure", "abc.def"):
            self.assertNotIn(leak, dumped)

    def test_functions_only_for_code_and_never_imports(self) -> None:
        """Fix round 3, item 13: hunk-header context of docs and data files is
        just a nearby line, and an import names a dependency, not a function."""
        repo = self.init()
        dart = ("import 'package:flutter/material.dart';\n"
                "import '../utils/text_style_utils.dart';\n\n" + "\n" * 3
                + "class AppVersionWidget extends StatelessWidget {\n" + "  // a\n" * 4
                + "  Widget build() => Text('v1');\n}\n")
        files = {
            "lib/app_version_widget.dart": dart,
            "docs/screens.md": "# Screens\nerror events with level, seq\n" + "x\n" * 6,
            "lib/l10n/app_en.arb": '{\n  "title": "A",\n' + '  "k": "v",\n' * 6 + "}\n",
            "config/app.yaml": "server:\n  url: a\n" + "  k: v\n" * 6,
            "data/x.json": '{\n  "a": 1,\n' + '  "k": 2,\n' * 6 + "}\n",
            "notes.txt": "Heading\n" + "line\n" * 6,
            "src/Main.kt": "package com.example.app\n\nimport android.os.Bundle\n\n"
                           + "\n" * 4 + "x = 1\n",
        }
        for path, text in files.items():
            self.write(repo, path, text)
        self.commit(repo, "base")
        for path, text in files.items():
            self.write(repo, path, text.rstrip("\n").rsplit("\n", 1)[0] + "\nchanged\n")
        self.write(repo, "lib/app_version_widget.dart",
                   dart.replace("Text('v1')", "Text('v2')"))
        result = context_mod.diff_files(repo, "HEAD", None)
        got = {item["path"]: item["functions"] for item in result["changed_files"]}
        self.assertEqual(got["lib/app_version_widget.dart"],
                         ["class AppVersionWidget extends StatelessWidget {"])
        for path in ("docs/screens.md", "lib/l10n/app_en.arb", "config/app.yaml",
                     "data/x.json", "notes.txt"):
            self.assertEqual(got[path], [], path)
        self.assertTrue(all(not context_mod.is_import_line(f) for f in got["src/Main.kt"]),
                        got["src/Main.kt"])
        for line in ("import 'package:x/y.dart';", "import java.util.List", "package main",
                     "from a.b import c", "#include <stdio.h>", "using System;",
                     "export * from './a';", "export { a } from './b';", "library foo;",
                     "part 'a.g.dart';", "const fs = require('fs');", "@import url(x);"):
            self.assertTrue(context_mod.is_import_line(line), line)
        for line in ("class Importer {", "def import_data(self):", "export default App;",
                     "partial void Run()", "fun packageName(): String {", ""):
            self.assertFalse(context_mod.is_import_line(line), line)
        for path, keeps in (("a.dart", True), ("a.py", True), ("Makefile", True),
                            ("README.MD", False), (".env.local", False), ("a.ARB", False),
                            ("x.yml", False)):
            self.assertEqual(context_mod.keeps_functions(path), keeps, path)

    def test_refs_that_look_like_options_are_refused_before_any_diff(self) -> None:
        repo = self.init()
        self.write(repo, "a.txt", "x\n")
        self.commit(repo, "only")
        victim = self.root / "victim.txt"
        victim.write_text("keep me\n", encoding="utf-8")
        for base, head in ((f"--output={victim}", None), ("main", f"--output={victim}"),
                           ("-p", None), ("main", "-R"), ("", None), ("ma\nin", None)):
            with self.subTest(base=base, head=head):
                with self.assertRaises(errors.AutonomError) as caught:
                    context_mod.diff_files(repo, base, head)
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
                self.assertTrue(caught.exception.hint)
                json.dumps(caught.exception.as_dict())
                self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.build(repo, f"--output={victim}", None, None, None, None)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
        # a ref that is not a commit (a tree object) is refused like a missing one
        tree = self.git(repo, "rev-parse", "HEAD^{tree}").strip()
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.diff_files(repo, tree, None)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)

    def test_git_failures_are_backend_failed(self) -> None:
        repo = self.init()
        self.write(repo, "a.txt", "x\n")
        self.commit(repo, "only")
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.diff_files(repo, "no-such-ref", None)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertTrue(caught.exception.hint)
        outside = self.root / "not-a-repo"
        outside.mkdir()
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.diff_files(outside, "main", None)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)


class BuildTests(RepoCase):
    def test_flow_selection_and_runtime_map_hints(self) -> None:
        repo = self.feature_repo()
        self.write(repo, ".autonom/flows/cart.yaml", FLOW_COVERS)
        self.write(repo, ".autonom/flows/broken.yaml", FLOW_BROKEN)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "flows")
        graph_dir = self.root / "home/apps/com.example.app/atlas"
        graph_dir.mkdir(parents=True)
        (graph_dir / "graph.json").write_text(json.dumps({
            "schema_version": 1, "app_id": "com.example.app",
            "screens": {
                "scr_cart": {"screen_id": "scr_cart", "labels": ["Your cart", "Checkout"]},
                "scr_home": {"screen_id": "scr_home", "labels": ["Home", "Catalog"]},
            },
            "transitions": {}}), encoding="utf-8")
        context = context_mod.build(repo, "main", "feature", None, None, "com.example.app")
        self.assertEqual(context["schema"], "autonom.pretest-context/v1")
        self.assertEqual(context["base"], "main")
        self.assertEqual(context["head"], "feature")
        self.assertIsNone(context["profile"])
        selected = context["flows"]["selected"]
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["flow"].endswith(".autonom/flows/cart.yaml"))
        self.assertTrue(Path(selected[0]["flow"]).is_absolute())
        self.assertIn("covers: lib/cart/a.dart", " | ".join(selected[0]["reasons"]))
        invalid = [item["flow"] for item in context["flows"]["invalid"]]
        self.assertTrue(any(name.endswith("broken.yaml") for name in invalid))
        uncovered = context["flows"]["uncovered_files"]
        self.assertNotIn("lib/cart/a.dart", uncovered)
        self.assertIn("lib/c.dart", uncovered)
        self.assertIn("lib/cart", context["changed_areas"])
        hints = context["runtime_map_hints"]
        self.assertEqual([hint["screen_id"] for hint in hints], ["scr_cart"])
        self.assertEqual(hints[0], {"screen_id": "scr_cart",
                                    "labels": ["Your cart", "Checkout"],
                                    "matched_area": "lib/cart", "hint": True})
        # hints never count as coverage: the flows block is computed without them
        self.assertNotIn("scr_cart", json.dumps(context["flows"]))

    def test_no_flows_dir_and_no_map(self) -> None:
        repo = self.feature_repo()
        context = context_mod.build(repo, "main", None, repo / "nope", None, "com.none")
        self.assertEqual(context["flows"]["selected"], [])
        self.assertEqual(context["flows"]["invalid"], [])
        self.assertEqual(sorted(context["flows"]["uncovered_files"]),
                         sorted(item["path"] for item in context["changed_files"]))
        self.assertEqual(context["runtime_map_hints"], [])
        # reading the map never creates the app's directory
        self.assertFalse((self.root / "home/apps/com.none").exists())

    def test_without_a_repo_the_context_is_empty(self) -> None:
        context = context_mod.build(None, None, None, None, None, None)
        self.assertEqual(context["changed_files"], [])
        self.assertEqual(context["flows"]["selected"], [])
        self.assertFalse(context["diff_truncated"])

    def test_unreadable_map_gives_no_hints(self) -> None:
        graph_dir = self.root / "home/apps/com.bad/atlas"
        graph_dir.mkdir(parents=True)
        (graph_dir / "graph.json").write_text("{", encoding="utf-8")
        self.assertEqual(context_mod.runtime_map_hints("com.bad", ["lib/cart/a.dart"],
                                                       ["lib/cart"]), [])


if __name__ == "__main__":
    unittest.main()
