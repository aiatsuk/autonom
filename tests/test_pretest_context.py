"""Pre-test diff context (tp-002): hunks, renames, binaries, truncation,
flow selection and Runtime Map hints."""
from __future__ import annotations

import json
import os
import signal
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
                         {"changed_files": [], "diff_truncated": False,
                          "skipped_untracked": []})
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
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertTrue(caught.exception.extra["reason"])

    def test_git_failures_are_invalid_value_with_gits_own_text(self) -> None:
        """fu-016: an unknown ref and a directory that is not a repository
        are invalid_value naming git's first stderr line; never empty."""
        repo = self.init()
        self.write(repo, "a.txt", "x\n")
        self.commit(repo, "only")
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.diff_files(repo, "no-such-ref", None)
        error = caught.exception
        self.assertEqual(error.code, errors.INVALID_VALUE)
        self.assertTrue(error.hint)
        self.assertIn("--base no-such-ref is not a commit", error.message)
        self.assertTrue(error.extra["reason"].strip())
        self.assertIn(error.extra["reason"], error.message)
        self.assertNotIn("not on PATH", error.message)
        json.dumps(error.as_dict())
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.diff_files(repo, "main", "no-such-head")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertIn("--head no-such-head", caught.exception.message)
        outside = self.root / "not-a-repo"
        outside.mkdir()
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.diff_files(outside, "main", None)
        error = caught.exception
        self.assertEqual(error.code, errors.INVALID_VALUE)
        self.assertTrue(error.extra["reason"].strip())
        self.assertIn(error.extra["reason"].splitlines()[0], error.message)
        self.assertNotIn("not on PATH", error.message)

    def test_git_error_without_stderr_still_has_a_reason(self) -> None:
        repo = self.init()
        failed = subprocess.CompletedProcess(["git"], 129, stdout=b"", stderr=b"\n  \n")
        with mock.patch.object(context_mod.subprocess, "run", return_value=failed):
            with self.assertRaises(errors.AutonomError) as caught:
                context_mod._git_bytes(repo, ["status"], "status")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(caught.exception.extra["reason"], "git exited with status 129")
        self.assertTrue(caught.exception.message.endswith("git exited with status 129"))
        # a multi-line stderr keeps only its first non-empty line
        failed = subprocess.CompletedProcess(["git"], 128, stdout=b"",
                                             stderr=b"\nfatal: first\nhint: second\n")
        with mock.patch.object(context_mod.subprocess, "run", return_value=failed):
            with self.assertRaises(errors.AutonomError) as caught:
                context_mod._git_bytes(repo, ["status"], "status")
        self.assertEqual(caught.exception.extra["reason"], "fatal: first")

    def test_missing_git_binary_is_git_missing(self) -> None:
        """fu-016: only a missing git executable says 'git is not on PATH'."""
        repo = self.init()
        with mock.patch.object(context_mod.subprocess, "run",
                               side_effect=FileNotFoundError(2, "No such file", "git")):
            with self.assertRaises(errors.AutonomError) as caught:
                context_mod.diff_files(repo, "main", None)
        self.assertEqual(caught.exception.code, errors.GIT_MISSING)
        self.assertEqual(caught.exception.message, "git is not on PATH")
        self.assertTrue(caught.exception.hint)
        json.dumps(caught.exception.as_dict())
        # the streamed -U0 diff reports the same
        with mock.patch.object(context_mod.subprocess, "Popen",
                               side_effect=FileNotFoundError(2, "No such file", "git")):
            with self.assertRaises(errors.AutonomError) as caught:
                context_mod._hunks(repo, "main", 1000)
        self.assertEqual(caught.exception.code, errors.GIT_MISSING)
        # and with git really absent from PATH
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty-bin")}):
            with self.assertRaises(errors.AutonomError) as caught:
                context_mod.diff_files(repo, "main", None)
        self.assertEqual(caught.exception.code, errors.GIT_MISSING)

    def test_missing_or_file_repo_is_invalid_value_not_git_missing(self) -> None:
        """fu-016: a missing --repo or a file as --repo is never 'git is not on PATH'."""
        missing = self.root / "no-such-repo"
        a_file = self.root / "a-file"
        a_file.write_text("x\n", encoding="utf-8")
        for repo in (missing, a_file):
            for call in (lambda r: context_mod.diff_files(r, "main", None),
                         lambda r: context_mod.build(r, "main", None, None, None, None),
                         lambda r: context_mod._hunks(r, "main", 1000)):
                with self.subTest(repo=repo.name):
                    with self.assertRaises(errors.AutonomError) as caught:
                        call(repo)
                    error = caught.exception
                    self.assertEqual(error.code, errors.INVALID_VALUE)
                    self.assertIn("is not a directory", error.message)
                    self.assertIn(str(repo), error.message)
                    self.assertTrue(error.hint)
                    self.assertNotIn("PATH", error.message)
                    json.dumps(error.as_dict())


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

    def test_pr_flow_for_ios_on_android_session_is_warned(self) -> None:
        """A.10: the context passes the session platform to the selection and
        keeps its warning in ``flows.warnings``; the flow stays selected."""
        repo = self.feature_repo()
        self.write(repo, ".autonom/flows/ios.yaml",
                   "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: iPhone only\n"
                   "tags: [pull-request]\nrequires:\n  platform:\n    - ios\n---\n"
                   "- assertVisible:\n    selector:\n      id: x\n")
        expected = [{"code": "flow_platform_mismatch",
                     "message": "flow iPhone only is for ios",
                     "flow": ".autonom/flows/ios.yaml", "platforms": ["ios"],
                     "session_platform": "android"}]
        for kwargs, session in (({}, {"platform": "android"}),
                                ({"platform": "android"}, None),
                                ({"platform": "android"}, {"platform": "ios"})):
            with self.subTest(kwargs=kwargs, session=session):
                context = context_mod.build(repo, "main", "feature", None, session,
                                            "com.example.app", **kwargs)
                self.assertTrue(any(item["flow"].endswith("ios.yaml")
                                    for item in context["flows"]["selected"]))
                self.assertEqual(context["flows"]["warnings"], expected)
        context = context_mod.build(repo, "main", "feature", None, {"platform": "ios"},
                                    "com.example.app")
        self.assertEqual(context["flows"]["warnings"], [])
        self.assertEqual(context_mod.build(None, None, None, None, None, None)
                         ["flows"]["warnings"], [])

    def test_missing_flows_dir_is_invalid_value(self) -> None:
        """fu-021: an explicit --flows that does not exist is refused."""
        repo = self.feature_repo()
        missing = repo / "nope"
        for repo_arg in (repo, None):
            with self.subTest(repo=repo_arg):
                with self.assertRaises(errors.AutonomError) as caught:
                    context_mod.build(repo_arg, "main", None, missing, None, "com.none")
                error = caught.exception
                self.assertEqual(error.code, errors.INVALID_VALUE)
                self.assertEqual(error.message, f"--flows {missing} does not exist")
                self.assertTrue(error.hint)
                json.dumps(error.as_dict())
        # an existing directory (even an empty one) is fine
        empty = repo / "empty-flows"
        empty.mkdir()
        context = context_mod.build(repo, "main", None, empty, None, "com.none")
        self.assertEqual(context["flows"]["selected"], [])

    def test_no_flows_dir_and_no_map(self) -> None:
        repo = self.feature_repo()
        context = context_mod.build(repo, "main", None, None, None, "com.none")
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
        self.assertEqual(context["skipped_untracked"], [])
        self.assertEqual(context["flows"]["selected"], [])
        self.assertFalse(context["diff_truncated"])

    def test_unreadable_map_gives_no_hints(self) -> None:
        graph_dir = self.root / "home/apps/com.bad/atlas"
        graph_dir.mkdir(parents=True)
        (graph_dir / "graph.json").write_text("{", encoding="utf-8")
        self.assertEqual(context_mod.runtime_map_hints("com.bad", ["lib/cart/a.dart"],
                                                       ["lib/cart"]), [])


class UntrackedReadTests(RepoCase):
    """fu-017: untracked files are read without following a symlink and
    without blocking; anything else is skipped and listed."""

    def alarm(self, seconds: int = 5) -> None:
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("no SIGALRM on this platform")

        def fire(_signum, _frame):
            raise TimeoutError("reading untracked files blocked")

        previous = signal.signal(signal.SIGALRM, fire)
        signal.alarm(seconds)

        def restore() -> None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

        self.addCleanup(restore)

    def repo_with_specials(self) -> Path:
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        repo = self.init()
        self.write(repo, "a.txt", "x\n")
        self.commit(repo, "only")
        os.mkfifo(repo / "pipe")
        os.symlink("pipe", repo / "pipe-link")
        self.write(repo, "real.txt", "one\ntwo\n")
        os.symlink("real.txt", repo / "real-link")
        return repo

    def test_fifo_and_symlinks_do_not_block_and_are_listed(self) -> None:
        repo = self.repo_with_specials()
        self.alarm()
        # git itself does not list a FIFO; name it as if it did, so the
        # reader meets both kinds
        listed = ["pipe", "pipe-link", "real-link", "real.txt"]
        with mock.patch.object(context_mod, "_untracked", return_value=listed):
            result = context_mod.diff_files(repo, "main", None)
        self.assertEqual(sorted(result["skipped_untracked"]),
                         ["pipe", "pipe-link", "real-link"])
        files = {item["path"]: item for item in result["changed_files"]}
        self.assertEqual(files["real.txt"]["ranges"], [[1, 2]])
        for name in ("pipe", "pipe-link", "real-link"):
            self.assertEqual(files[name]["ranges"], [], name)
            self.assertEqual(files[name]["status"], "A")

    def test_build_lists_skipped_untracked_in_context(self) -> None:
        repo = self.repo_with_specials()
        self.alarm()
        context = context_mod.build(repo, "main", None, None, None, None)
        # git lists the symlinks, not the FIFO
        self.assertIn("pipe-link", context["skipped_untracked"])
        self.assertIn("real-link", context["skipped_untracked"])
        self.assertNotIn("real.txt", context["skipped_untracked"])
        json.dumps(context)

    def test_a_symlink_swapped_in_after_lstat_is_not_followed(self) -> None:
        repo = self.repo_with_specials()
        self.alarm()
        regular = os.lstat(repo / "real.txt")
        real_lstat = os.lstat

        def lying_lstat(path, *args, **kwargs):
            if str(path).endswith("pipe-link"):
                return regular  # as if it was a regular file a moment ago
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(context_mod.os, "lstat", side_effect=lying_lstat):
            with self.assertRaises(OSError):
                context_mod.read_untracked(repo / "pipe-link")
            with mock.patch.object(context_mod, "_untracked", return_value=["pipe-link"]):
                result = context_mod.diff_files(repo, "main", None)
        self.assertEqual(result["skipped_untracked"], ["pipe-link"])

    def test_large_and_binary_untracked_files_keep_their_rules(self) -> None:
        repo = self.init()
        self.write(repo, "a.txt", "x\n")
        self.commit(repo, "only")
        self.write(repo, "blob.bin", b"\x00\x01")
        with mock.patch.object(context_mod, "UNTRACKED_READ_LIMIT", 4):
            self.write(repo, "big.txt", "123456789\n")
            result = context_mod.diff_files(repo, "main", None)
        files = {item["path"]: item for item in result["changed_files"]}
        self.assertTrue(files["blob.bin"]["binary"])
        self.assertEqual(files["big.txt"]["ranges"], [])
        self.assertTrue(result["diff_truncated"])
        self.assertEqual(result["skipped_untracked"], [])


if __name__ == "__main__":
    unittest.main()
