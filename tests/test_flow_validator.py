"""Flow v1 validator: containment, cycles, discovery, and the check verbs.

The containment check is built on ``Path.resolve()`` — symlinks are followed
*before* the workspace comparison, so a symlink inside the workspace pointing
outside it is an escape, not a loophole.
"""
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
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import validator  # noqa: E402

MINIMAL = "schema: autonom.dev/flow/v1\nname: {name}\n---\n- back\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _flow_with_sub(name: str, sub_rel: str) -> str:
    return ("schema: autonom.dev/flow/v1\n"
            f"name: {name}\n---\n- runFlow: {sub_rel}\n")


class WorkspaceRootTests(unittest.TestCase):
    def test_nearest_dot_autonom_ancestor_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".autonom").mkdir(parents=True)
            flow = _write(repo / ".autonom/flows/auth/login.yaml",
                          MINIMAL.format(name="x"))
            self.assertEqual(validator.workspace_root(flow), repo.resolve())

    def test_falls_back_to_the_flow_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow = _write(Path(tmp) / "flows/login.yaml", MINIMAL.format(name="x"))
            self.assertEqual(validator.workspace_root(flow),
                             (Path(tmp) / "flows").resolve())


class TreeValidationTests(unittest.TestCase):
    def test_valid_graph_passes_and_diamonds_are_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".autonom").mkdir()
            shared = "../subflows/shared.yaml"
            _write(repo / ".autonom/subflows/shared.yaml", MINIMAL.format(name="s"))
            _write(repo / ".autonom/subflows/a.yaml", _flow_with_sub("a", "shared.yaml"))
            root_text = ("schema: autonom.dev/flow/v1\nname: root\n---\n"
                         f"- runFlow: ../subflows/a.yaml\n- runFlow: {shared}\n")
            root = _write(repo / ".autonom/flows/root.yaml", root_text)
            flow = validator.validate_tree(root)
            self.assertEqual(flow.name, "root")

    def test_cycle_is_refused_with_the_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".autonom").mkdir()
            a = _write(repo / ".autonom/flows/a.yaml", _flow_with_sub("a", "b.yaml"))
            _write(repo / ".autonom/flows/b.yaml", _flow_with_sub("b", "a.yaml"))
            with self.assertRaises(errors.AutonomError) as caught:
                validator.validate_tree(a)
            self.assertEqual(caught.exception.code, errors.FLOW_CYCLE_DETECTED)
            chain = caught.exception.extra["chain"]
            self.assertEqual(chain[0], chain[-1])
            self.assertGreaterEqual(len(chain), 3)

    def test_self_recursion_is_a_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow = _write(Path(tmp) / "a.yaml", _flow_with_sub("a", "a.yaml"))
            with self.assertRaises(errors.AutonomError) as caught:
                validator.validate_tree(flow)
            self.assertEqual(caught.exception.code, errors.FLOW_CYCLE_DETECTED)

    def test_escape_by_dotdot_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".autonom").mkdir(parents=True)
            _write(Path(tmp) / "outside.yaml", MINIMAL.format(name="out"))
            flow = _write(repo / ".autonom/flows/a.yaml",
                          _flow_with_sub("a", "../../../outside.yaml"))
            with self.assertRaises(errors.AutonomError) as caught:
                validator.validate_tree(flow)
            self.assertEqual(caught.exception.code,
                             errors.FLOW_PATH_ESCAPES_WORKSPACE)
            self.assertIn("line", caught.exception.extra)

    def test_escape_by_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".autonom").mkdir(parents=True)
            outside = _write(Path(tmp) / "outside.yaml", MINIMAL.format(name="out"))
            link = repo / ".autonom/subflows/link.yaml"
            link.parent.mkdir(parents=True)
            os.symlink(outside, link)
            flow = _write(repo / ".autonom/flows/a.yaml",
                          _flow_with_sub("a", "../subflows/link.yaml"))
            with self.assertRaises(errors.AutonomError) as caught:
                validator.validate_tree(flow)
            self.assertEqual(caught.exception.code,
                             errors.FLOW_PATH_ESCAPES_WORKSPACE)

    def test_missing_subflow_names_the_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".autonom").mkdir()
            flow = _write(repo / ".autonom/flows/a.yaml",
                          _flow_with_sub("a", "gone.yaml"))
            with self.assertRaises(errors.AutonomError) as caught:
                validator.validate_tree(flow)
            exc = caught.exception
            self.assertEqual(exc.code, errors.FLOW_FILE_NOT_FOUND)
            self.assertEqual(exc.extra["line"], 4)

    def test_subflow_error_is_attributed_to_the_subflow_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".autonom").mkdir()
            _write(repo / ".autonom/flows/bad.yaml",
                   "schema: autonom.dev/flow/v1\nname: bad\n---\n- frobnicate\n")
            flow = _write(repo / ".autonom/flows/a.yaml",
                          _flow_with_sub("a", "bad.yaml"))
            with self.assertRaises(errors.AutonomError) as caught:
                validator.validate_tree(flow)
            self.assertEqual(caught.exception.code, errors.FLOW_UNKNOWN_COMMAND)
            self.assertIn("bad.yaml", caught.exception.extra["file"])


class CliTests(unittest.TestCase):
    def _run(self, *args: str, cwd: str | None = None):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ)
            env["AUTONOM_HOME"] = home
            return subprocess.run(
                [sys.executable, str(CLI), *args],
                cwd=cwd or ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )

    def test_check_dir_aggregates_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "good.yaml", MINIMAL.format(name="g"))
            _write(Path(tmp) / "bad.yaml", "not a flow\n")
            result = self._run("flow", "check", tmp)
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stderr)
            self.assertEqual(payload["error_code"], "flow_check_failed")
            self.assertEqual(payload["checked"], 2)
            self.assertEqual(len(payload["errors"]), 1)
            self.assertEqual(payload["errors"][0]["error_code"], "flow_parse_error")

    def test_check_single_file_reports_the_error_directly(self) -> None:
        result = self._run("flow", "check", "/nonexistent/x.yaml")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "flow_file_not_found")

    def test_fmt_check_exits_one_when_reformatting_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file = _write(Path(tmp) / "a.yaml",
                          "schema: autonom.dev/flow/v1\nname: x\n---\n- tapOn: Login\n")
            result = self._run("flow", "fmt", str(file), "--check")
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["changed"])
            # --write then converges
            self._run("flow", "fmt", str(file), "--write")
            result = self._run("flow", "fmt", str(file), "--check")
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_list_reports_invalid_files_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "good.yaml", MINIMAL.format(name="g"))
            _write(Path(tmp) / "bad.yaml", "nope\n")
            result = self._run("flow", "list", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["invalid"][0]["error_code"], "flow_parse_error")

    def test_repo_smoke_flow_checks_clean(self) -> None:
        result = self._run("flow", "check",
                           str(ROOT / "tests/fixtures/flows/settings_smoke.yaml"))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["flows"][0]["platforms"], ["android"])


if __name__ == "__main__":
    unittest.main()
