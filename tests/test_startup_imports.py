"""AL-07: what one CLI call costs before it does any work.

Two causes were measured: every release-launcher call ran `python -B`, so the
library was compiled again on each start, and `scripts/autonom.py` imported
the report, campaign, metrics, network and atlas modules at module level,
which pulled `ssl`, `http.client`, `urllib.request`, `xml.sax`, `zipfile` and
`statistics` into a plain `ui tree`.

These tests install a runtime built from this checkout with the real setup
helper (no network: the download is patched) and check the launcher text,
the bytecode it leaves behind, the runtime digest, and that the installed
launcher really loads that bytecode. Then they read the `-X importtime` log of
`ui tree --dump` to prove the heavy modules stay out of a quick verb.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
HELPER = ROOT / "plugins/autonom/skills/autonom-setup/scripts/install_runtime.py"

# Spec AL-07-S02: none of these may be imported by `ui tree --dump`.
HEAVY_STDLIB = ("ssl", "http.client", "urllib.request", "xml.sax", "zipfile", "statistics")
# The command modules autonom.py now imports inside the commands that use
# them. Not listed: network.store, network.redact and atlas.fingerprint,
# which the library itself loads at start-up (pre-test guard, journal, Flow
# executor) and which import none of HEAVY_STDLIB.
COMMAND_MODULES = (
    "autonom_lib.flow.report", "autonom_lib.report_model", "autonom_lib.report_bundle",
    "autonom_lib.report_export", "autonom_lib.gates", "autonom_lib.campaign",
    "autonom_lib.tour", "autonom_lib.metrics", "autonom_lib.network.proxy",
    "autonom_lib.network.mocks", "autonom_lib.network.har",
    "autonom_lib.network.device_proxy_android", "autonom_lib.atlas.graph",
)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


setup = load("install_runtime_startup", HELPER)


def imported_modules(log: str) -> list[str]:
    """Module names of a `-X importtime` log, in the order they finished."""
    names = []
    for line in log.splitlines():
        if not line.startswith("import time:"):
            continue
        fields = line.split("|")
        if len(fields) != 3 or fields[2].strip() == "imported package":
            continue  # the header line
        names.append(fields[2].strip())
    return names


def verbose_paths(log: str, event: str) -> list[str]:
    """Paths of one `-v` import event: "code object from" (bytecode loads
    print the .pyc path quoted, source compiles print the bare .py path) or
    "created" (bytecode written, quoted path)."""
    prefix = f"# {event} "
    paths = []
    for line in log.splitlines():
        if not line.startswith(prefix):
            continue
        text = line[len(prefix):]
        paths.append(ast.literal_eval(text) if text[:1] in ("'", '"') else text)
    return paths


def library_sources(runtime: Path) -> list[Path]:
    """The library modules plus the skills' helper the Android tree parser imports."""
    sources = sorted((runtime / "scripts/autonom_lib").rglob("*.py"))
    sources.append(next((runtime / "plugins").rglob("ui_common.py")))
    return sources


def without_bytecode(sources: list[Path]) -> list[Path]:
    return [path for path in sources
            if not Path(importlib.util.cache_from_source(str(path))).is_file()]


def child_env(home: Path, **extra: str) -> dict[str, str]:
    return {**os.environ, "AUTONOM_HOME": str(home), **extra}


class ImportLogParserTests(unittest.TestCase):
    def test_header_and_nesting(self):
        log = ("import time: self [us] | cumulative | imported package\n"
               "import time:        12 |         12 |     _ssl\n"
               "import time:       300 |        312 |   ssl\n"
               "unrelated stderr line\n")
        self.assertEqual(imported_modules(log), ["_ssl", "ssl"])

    def test_verbose_log_shows_source_compiles_and_written_bytecode(self):
        # The shapes CPython prints with -v, here for a path with a space.
        log = ("# /r t/__pycache__/a.cpython-314.pyc matches /r t/a.py\n"
               "# code object from '/r t/__pycache__/a.cpython-314.pyc'\n"
               "# code object from /r t/b.py\n"
               "# created '/r t/__pycache__/b.cpython-314.pyc'\n"
               "import 'b' # <SourceFileLoader object>\n")
        self.assertEqual(verbose_paths(log, "code object from"),
                         ["/r t/__pycache__/a.cpython-314.pyc", "/r t/b.py"])
        self.assertEqual(verbose_paths(log, "created"), ["/r t/__pycache__/b.cpython-314.pyc"])

    def test_verbose_log_shapes_of_this_interpreter(self):
        # The parser must read what this CPython really prints: the first
        # import compiles the source and writes bytecode, the second loads it.
        with tempfile.TemporaryDirectory(prefix="autonom verbose ") as tmp:
            source = Path(tmp) / "probe_module.py"
            source.write_text("VALUE = 1\n")
            env = {**os.environ, "PYTHONVERBOSE": "1"}
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            command = [sys.executable, "-c",
                       "import sys; sys.path.insert(0, sys.argv[1]); import probe_module", tmp]
            logs = [subprocess.run(command, env=env, capture_output=True, text=True,
                                   timeout=60, check=True).stderr for _ in range(2)]
        pyc = f"probe_module.{sys.implementation.cache_tag}.pyc"

        def probe(log: str, event: str) -> list[str]:
            return [path for path in verbose_paths(log, event)
                    if Path(path).name.startswith("probe_module.")]

        self.assertEqual(probe(logs[0], "code object from"), [str(source)], logs[0][-2000:])
        self.assertEqual([Path(path).name for path in probe(logs[0], "created")], [pyc])
        self.assertEqual([Path(path).name for path in probe(logs[1], "code object from")],
                         [pyc], logs[1][-2000:])
        self.assertEqual(probe(logs[1], "created"), [])


class InstalledRuntimeTests(unittest.TestCase):
    """AL-07-S01: one real install of this checkout's runtime."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="autonom startup ")
        cls.addClassCleanup(tmp.cleanup)  # also when the install below fails
        root = Path(tmp.name)
        cls.home = root / "home"
        cls.runtimes = root / "runtimes"
        cls.bin = root / "bin space"
        cls.bin.mkdir()
        cls.version = setup.plugin_version()
        name = f"autonom-{cls.version}"
        # What build_release.sh ships for the CLI: scripts/ and the skills
        # (ui_common lives there), without caches.
        cls.source = root / "source" / name
        for part in ("scripts", "plugins"):
            shutil.copytree(ROOT / part, cls.source / part, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "node_modules"))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as tar:
            tar.add(cls.source, arcname=name)
        cls.asset = output.getvalue()
        cls.result = cls.install()
        cls.final = Path(cls.result["runtime"])
        cls.launcher = Path(cls.result["executable"])
        # Read now, before any test runs the launcher (without -B it would
        # write missing bytecode itself) or installs again: these are the
        # installer's own results.
        cls.sources = library_sources(cls.final)
        cls.missing_after_install = without_bytecode(cls.sources)
        cls.bytecode_after_install = frozenset(cls.final.rglob("*.pyc"))

    @classmethod
    def install(cls) -> dict:
        checksum = hashlib.sha256(cls.asset).hexdigest()
        with patch.dict(os.environ, {"AUTONOM_HOME": str(cls.home)}), \
                patch.object(setup, "download", side_effect=[
                    f"{checksum}  autonom-{cls.version}.tgz\n".encode(), cls.asset]):
            return setup.install(cls.version, cls.runtimes, cls.bin)

    def test_launcher_runs_the_runtime_without_dash_b(self):
        lines = self.launcher.read_text().splitlines()
        self.assertEqual(lines[:2], ["#!/bin/sh", setup.MARKER])
        self.assertEqual(len(lines), 3)
        argv = shlex.split(lines[2])
        self.assertNotIn("-B", argv)
        self.assertEqual(argv, ["exec", sys.executable,
                                str(self.final / "scripts/autonom.py"), "$@"])

    def test_runtime_is_precompiled_for_every_library_module(self):
        self.assertGreater(len([path for path in self.sources if "autonom_lib" in path.parts]), 50)
        self.assertEqual(self.sources[-1].name, "ui_common.py")
        self.assertEqual([str(path.relative_to(self.final))
                          for path in self.missing_after_install], [])
        if sys.pycache_prefix is None:
            self.assertIn(self.final / "scripts/autonom_lib/__pycache__"
                          / f"ui.{sys.implementation.cache_tag}.pyc", self.bytecode_after_install)

    def test_digest_ignores_the_bytecode(self):
        digest = setup.tree_digest(self.final)
        self.assertEqual(digest, setup.tree_digest(self.source))
        self.assertFalse([key for key in digest
                          if "__pycache__" in key or key.endswith(".pyc")])
        # a repeat install accepts the precompiled runtime as the same release
        again = self.install()
        self.assertEqual(again["runtime"], self.result["runtime"])
        self.assertEqual([path.name for path in self.runtimes.iterdir()], [self.final.name])

    @unittest.skipIf(sys.pycache_prefix, "bytecode lives under PYTHONPYCACHEPREFIX, not the runtime")
    def test_installed_launcher_loads_the_precompiled_library(self):
        env = child_env(self.home, PYTHONVERBOSE="1")
        env.pop("PYTHONDONTWRITEBYTECODE", None)  # a missing cache would be written
        before = frozenset(self.final.rglob("*.pyc"))
        completed = subprocess.run(
            [str(self.launcher), "ui", "tree", "--dump", str(FIXTURE)],
            capture_output=True, text=True, env=env, timeout=120, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 5)
        # the CLI puts its resolved directory on sys.path; a helper path may not be
        runtimes = tuple({str(self.final) + os.sep, str(self.final.resolve()) + os.sep})

        def in_runtime(paths: list[str]) -> list[str]:
            return [path for path in paths if path.startswith(runtimes)]

        loaded = in_runtime(verbose_paths(completed.stderr, "code object from"))
        self.assertTrue(loaded, "nothing of the runtime was imported")
        # a module compiled from source on this call shows its bare .py path
        self.assertEqual([path for path in loaded if not path.endswith(".pyc")], [])
        self.assertTrue(any(path.endswith(f"/ui.{sys.implementation.cache_tag}.pyc")
                            for path in loaded))
        self.assertEqual(in_runtime(verbose_paths(completed.stderr, "created")), [])
        self.assertEqual(sorted(map(str, frozenset(self.final.rglob("*.pyc")) - before)), [])


class UiTreeImportTests(unittest.TestCase):
    """AL-07-S02: `ui tree --dump` with a temporary AUTONOM_HOME."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="autonom-startup-")
        cls.addClassCleanup(tmp.cleanup)
        # PYTHONDONTWRITEBYTECODE keeps the checkout free of __pycache__.
        completed = subprocess.run(
            [sys.executable, "-X", "importtime", str(CLI), "ui", "tree", "--dump", str(FIXTURE)],
            capture_output=True, text=True, timeout=120, check=False,
            env=child_env(Path(tmp.name), PYTHONDONTWRITEBYTECODE="1"))
        cls.completed = completed
        cls.modules = imported_modules(completed.stderr)

    def test_command_succeeds_and_log_is_read(self):
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-2000:])
        payload = json.loads(self.completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 5)
        # the parse saw the CLI's own imports, so an empty result means nothing
        for name in ("json", "autonom_lib", "autonom_lib.ui", "autonom_lib.ui_android"):
            self.assertIn(name, self.modules)

    def test_heavy_stdlib_modules_are_not_imported(self):
        self.assertEqual([name for name in HEAVY_STDLIB if name in self.modules], [])

    def test_command_modules_are_not_imported(self):
        self.assertEqual([name for name in COMMAND_MODULES if name in self.modules], [])


class LazyNamesTests(unittest.TestCase):
    """The old module-level names keep working for callers of the CLI module."""

    @classmethod
    def setUpClass(cls):
        cls.cli = load("autonom_cli_startup", CLI)

    def test_old_names_resolve_to_the_imported_modules(self):
        for name, module in self.cli._LAZY_MODULES.items():  # noqa: SLF001
            with self.subTest(name=name):
                self.assertIs(getattr(self.cli, name), importlib.import_module(module))
        with self.assertRaises(AttributeError):
            self.cli.no_such_module_name  # noqa: B018

    def test_patching_an_old_name_reaches_the_command(self):
        from autonom_lib.network import store

        parser = self.cli.build_parser()
        record = {"session_id": "s_test", "artifacts_dir": "/nonexistent"}
        for argv, expected in ((["network", "requests", "list"], store.DEFAULT_MAX),
                               (["network", "requests", "list", "--max", "3"], 3)):
            args = parser.parse_args(argv)
            with self.subTest(argv=argv), \
                    patch.object(self.cli.session_mod, "require_current", return_value=record), \
                    patch.object(self.cli.store_mod, "listing",
                                 return_value={"requests": []}) as listing, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(self.cli.cmd_network_requests_list(args), 0)
            self.assertEqual(listing.call_args.kwargs["max_items"], expected)
            self.assertEqual(json.loads(out.getvalue()), {"ok": True, "requests": []})

    def test_frames_refusal_keeps_its_hint(self):
        from autonom_lib.metrics import frames

        self.assertEqual(self.cli._frames_android_only(),  # noqa: SLF001
                         ("gfxinfo frame stats are Android-only", frames.IOS_FRAMES_HINT))


if __name__ == "__main__":
    unittest.main()
