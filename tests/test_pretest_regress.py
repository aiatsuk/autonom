"""Regression drafts, reproducibility and reruns (tp-020..tp-022).

`pretest flow-draft`, `pretest repro` and `pretest rerun` run end to end
through scripts/autonom.py on the fake adb, with a temporary ``AUTONOM_HOME``
and a temporary git repository; a few cases run ``main()`` in this process so
the flow executor can be wrapped (a device that loses the result on one of
three runs, an executor that stops unexpectedly). The library helpers of
``autonom_lib.pretest.regress`` are covered directly.

Every refusal is checked for its ``error_code``, a ``hint`` and that it wrote
nothing: no draft file, no finding event, no new pre-test directory.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
sys.path.insert(0, str(ROOT / "scripts"))
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
SERIAL = "emulator-5554"
APP = "com.example.app"

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.pretest import regress  # noqa: E402

NODE = ('<node index="{i}" text="{text}" resource-id="com.example.app:id/{rid}" '
        'class="android.widget.Button" package="com.example.app" content-desc="" '
        'clickable="true" enabled="true" focusable="true" scrollable="false" '
        'bounds="{bounds}" />')


def dump(*, results: bool) -> str:
    nodes = [NODE.format(i=0, text="Continue", rid="next", bounds="[100,600][500,720]")]
    if results:
        nodes.append(NODE.format(i=1, text="Results", rid="results",
                                 bounds="[100,900][500,980]"))
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
        <hierarchy rotation="0">
          <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,2400]">
        """) + "\n".join(nodes) + "\n  </node>\n</hierarchy>\n"


TICKET = """# WB-7 Show results after search

The results list must appear after Continue.
"""


def load_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("autonom_cli_regress", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def tree_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class RegressCase(unittest.TestCase):
    """A private home, fake adb state and log, and a git repo whose working
    tree changes `src/search.dart` and `src/[odd]*.dart` against `main`."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.log = self.root / "fake.log"
        self.state = self.root / "state.json"
        self.dump = self.root / "dump.xml"
        self.dump.write_text(dump(results=True), encoding="utf-8")
        self.empty_dump = self.root / "dump-empty.xml"
        self.empty_dump.write_text(dump(results=False), encoding="utf-8")
        self.write_state()
        self.env = {**os.environ, "AUTONOM_HOME": str(self.home),
                    "AUTONOM_FAKE_STATE": str(self.state),
                    "AUTONOM_FAKE_LOG": str(self.log)}
        for key in ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_SESSION_ID"):
            self.env.pop(key, None)
        self.repo = self.root / "repo"
        self.make_repo()
        self.ticket = self.root / "ticket.md"
        self.ticket.write_text(TICKET, encoding="utf-8")

    # --- helpers -------------------------------------------------------------

    def write_state(self, dump_path: Path | None = None) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(dump_path or self.dump)}
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def make_repo(self) -> None:
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".autonom/flows").mkdir(parents=True)

        def git(*argv: str) -> None:
            subprocess.run(["git", *argv], cwd=self.repo, check=True,
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.repo / "src/search.dart").write_text("v1\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        (self.repo / "src/search.dart").write_text("v2\n", encoding="utf-8")
        (self.repo / "src/[odd]*.dart").write_text("new\n", encoding="utf-8")

    def cli(self, *argv: str) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL, *argv],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=180,
            stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def ok(self, *argv: str) -> dict:
        completed = self.cli(*argv)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        return payload

    def refused(self, code: str, *argv: str) -> dict:
        completed = self.cli(*argv)
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], code, payload)
        self.assertTrue(payload.get("hint"), f"{code} has no hint")
        return payload

    def in_process(self, argv: list[str], *patches) -> tuple[int, dict]:
        cli = load_cli()
        env = {key: self.env[key] for key in
               ("AUTONOM_HOME", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG")}
        out, err = StringIO(), StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(contextlib.chdir(self.root))
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = cli.main(["--adb", str(FAKE_ADB), "--serial", SERIAL, *argv])
        text = out.getvalue() if code == 0 else err.getvalue()
        return code, json.loads(text)

    def start_session(self) -> dict:
        self.ok("session", "start", "--app-id", APP)
        found = list((self.home / "sessions").glob("s_*/session.json"))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text(encoding="utf-8"))

    def pretests(self, session: dict) -> list[Path]:
        root = Path(session["artifacts_dir"]) / "pretests"
        return sorted(p for p in root.glob("pt_*") if p.is_dir()) if root.is_dir() else []

    def drafts(self) -> list[Path]:
        return sorted((self.repo / ".autonom/flows/drafts").glob("*.yaml"))

    def drive(self, *start_extra: str) -> tuple[dict, dict]:
        """chk-1 passes by hand; chk-2 taps Continue twice, finds Results and
        fails with finding f-1; a later find outside any check must never
        become the draft's closing assertion."""
        session = self.start_session()
        started = self.ok("pretest", "start", "--ticket", str(self.ticket), "--base", "main",
                          "--repo", str(self.repo), "--ticket-ref", "WB-7", *start_extra)
        self.ok("pretest", "check", "add", "Search shows results", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "pass")
        self.ok("pretest", "check", "add", "Continue twice reaches results", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-2")
        self.ok("ui", "tap", "--text", "Continue")
        self.ok("ui", "tap", "--text", "Continue")
        self.ok("ui", "find", "--text", "Results")
        self.ok("pretest", "check", "end", "chk-2", "--verdict", "fail",
                "--note", "results vanish")
        self.ok("pretest", "finding", "add", "--check", "chk-2", "--severity", "major",
                "--title", "Results vanish", "--actual", "an empty screen",
                "--expected", "the results list")
        self.ok("ui", "find", "--text", "Continue")
        return session, started

    def findings_file(self, session: dict) -> Path:
        return self.pretests(session)[0] / "findings.ndjson"


# --- library helpers ------------------------------------------------------------------


class HelperTests(unittest.TestCase):
    def test_slug_and_default_path(self) -> None:
        self.assertEqual(regress.slug({"ticket_ref": "WB-7", "title": "x"}), "wb-7")
        self.assertEqual(regress.slug({"ticket_ref": None, "title": "Show  Results!"}),
                         "show-results")
        self.assertEqual(regress.slug({"ticket_ref": "///", "title": "Ёж"}), "pretest")
        self.assertLessEqual(len(regress.slug({"title": "a" * 300})), regress.SLUG_LIMIT)
        path = regress.default_draft_path({"repo": "/r", "ticket_ref": "WB-7"}, "f-2")
        self.assertEqual(path, Path("/r/.autonom/flows/drafts/wb-7-f-2.yaml"))
        self.assertEqual(regress.default_draft_path({"title": "t"}, "f-1").parent,
                         Path.cwd() / ".autonom/flows/drafts")

    def test_covers_escapes_globs_and_skips_commas(self) -> None:
        patterns, warnings = regress._covers({"changed_files": [  # noqa: SLF001
            {"path": "src/a.dart"}, {"path": "src/[odd]*?.dart"}, {"path": "a,b.dart"},
            {"path": "src/a.dart"}, {"nopath": 1}, "junk"]})
        self.assertEqual(patterns, ["src/a.dart", "src/[[]odd][*][?].dart"])
        self.assertEqual([item["code"] for item in warnings], ["covers_path_skipped"])
        import fnmatch
        self.assertTrue(fnmatch.fnmatch("src/[odd]*?.dart", patterns[1]))
        self.assertFalse(fnmatch.fnmatch("src/oxd.dart", patterns[1]))

    def test_execution_stability_and_times(self) -> None:
        skipped = type("S", (), {"status": "skipped"})()
        self.assertEqual(regress.execution("passed", None, []), "passed")
        self.assertEqual(regress.execution("passed", None, [skipped]), "skipped")
        self.assertEqual(regress.execution("failed", {"failure_class": "test_failure"}, []),
                         "failed")
        self.assertEqual(regress.execution("failed", {"failure_class": "infrastructure"}, []),
                         "broken")
        self.assertEqual(regress.stability(0, 3), "not_reproduced")
        self.assertEqual(regress.stability(1, 3), "intermittent")
        self.assertEqual(regress.stability(3, 3), "stable")
        for bad in (0, 11, -1, True, "3", 2.0, None):
            with self.assertRaises(errors.AutonomError) as caught:
                regress.check_times(bad)
            self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
            self.assertTrue(caught.exception.hint)
        self.assertEqual(regress.check_times(10), 10)

    def test_source_session_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"AUTONOM_HOME": home}):
            for session_id in ("s_missing", "../etc", "", None):
                with self.assertRaises(errors.AutonomError) as caught:
                    regress._source_session({"session_id": session_id}, None)  # noqa: SLF001
                self.assertEqual(caught.exception.code, errors.DRAFT_UNAVAILABLE)
                self.assertTrue(caught.exception.hint)
            broken = Path(home) / "sessions/s_broken"
            broken.mkdir(parents=True)
            (broken / "session.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(errors.AutonomError) as caught:
                regress._source_session({"session_id": "s_broken"}, None)  # noqa: SLF001
            self.assertEqual(caught.exception.code, errors.DRAFT_UNAVAILABLE)

    def test_write_draft_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new/dir/d.yaml"
            self.assertTrue(regress._write_draft(path, "a: 1\n"))  # noqa: SLF001
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertFalse(regress._write_draft(path, "a: 1\n"))  # noqa: SLF001
            for target, code in ((path, errors.OUTPUT_NOT_WRITABLE),
                                 (Path(tmp), errors.OUTPUT_NOT_WRITABLE)):
                with self.assertRaises(errors.AutonomError) as caught:
                    regress._write_draft(target, "b: 2\n")  # noqa: SLF001
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.hint)
            self.assertEqual(path.read_text(encoding="utf-8"), "a: 1\n")
            link = Path(tmp) / "link.yaml"
            link.symlink_to(path)
            with self.assertRaises(errors.AutonomError):
                regress._write_draft(link, "a: 1\n")  # noqa: SLF001
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["d.yaml"])

    def test_write_draft_never_follows_a_planted_scratch_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim"
            victim.write_text("keep me\n", encoding="utf-8")
            victim.chmod(0o644)
            out = Path(tmp) / "drafts" / "d.yaml"
            out.parent.mkdir()
            # the old predictable scratch name, pre-created as a link to another file
            planted = out.with_name(f".{out.name}.{os.getpid()}.tmp")
            planted.symlink_to(victim)
            self.assertTrue(regress._write_draft(out, "name: x\n"))  # noqa: SLF001
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
            self.assertFalse(out.is_symlink())
            self.assertEqual(out.read_text(encoding="utf-8"), "name: x\n")
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            self.assertTrue(planted.is_symlink())  # left alone, not reused
            self.assertEqual(sorted(p.name for p in out.parent.iterdir()),
                             sorted(["d.yaml", planted.name]))

    def test_write_draft_removes_its_scratch_when_writing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "d.yaml"
            with mock.patch.object(regress.os, "replace",
                                   side_effect=PermissionError(13, "Permission denied")):
                with self.assertRaises(errors.AutonomError) as caught:
                    regress._write_draft(out, "name: x\n")  # noqa: SLF001
            self.assertEqual(caught.exception.code, errors.OUTPUT_NOT_WRITABLE)
            self.assertIn("Permission denied", caught.exception.message)
            self.assertTrue(caught.exception.hint)
            self.assertEqual(list(Path(tmp).iterdir()), [])


# --- flow-draft --------------------------------------------------------------------------


class FlowDraftTests(RegressCase):
    def test_draft_holds_both_taps_covers_and_passes_flow_check(self) -> None:
        session, started = self.drive()
        payload = self.ok("pretest", "flow-draft", "f-1")
        path = self.repo / ".autonom/flows/drafts/wb-7-f-1.yaml"
        self.assertEqual(Path(payload["draft_flow"]), path)
        self.assertTrue(payload["written"])
        self.assertTrue(payload["recorded"])
        self.assertEqual(payload["check_id"], "chk-2")
        self.assertEqual(sorted(payload["covers"]), ["src/[[]odd][*].dart", "src/search.dart"])
        self.assertIn("draft", payload["tags"])
        self.assertEqual(payload["flow_check"]["ok"], True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("tapOn"), 2, text)
        self.assertIn("covers:", text)
        self.assertIn("src/search.dart", text)
        self.assertIn(f"pretest: {started['pretest_id']}", text)
        # the closing assertion is chk-2's own find, not the later one
        self.assertIn("assertVisible", text)
        closing = text.split("assertVisible", 1)[1]
        self.assertIn("Results", closing)
        self.assertNotIn("Continue", closing)
        checked = self.ok("flow", "check", str(path))
        self.assertEqual(checked["checked"], 1)
        found = self.ok("pretest", "finding", "list")["findings"][0]
        self.assertEqual(found["draft_flow"], str(path))
        # a second draft of the same finding changes nothing
        events = self.findings_file(session).read_bytes()
        again = self.ok("pretest", "flow-draft", "f-1")
        self.assertFalse(again["written"])
        self.assertFalse(again["recorded"])
        self.assertEqual(self.findings_file(session).read_bytes(), events)
        # --out writes elsewhere and records that path
        out = self.root / "custom/draft.yaml"
        moved = self.ok("pretest", "flow-draft", "f-1", "--out", str(out))
        self.assertTrue(out.is_file())
        self.assertTrue(moved["recorded"])
        self.assertEqual(self.ok("pretest", "finding", "list")["findings"][0]["draft_flow"],
                         str(out))

    def test_refusals_write_nothing(self) -> None:
        self.refused("no_active_session", "pretest", "flow-draft", "f-1")
        session, _ = self.drive()
        # a check that only looked (one find) has no replayable action
        self.ok("pretest", "check", "add", "Only looks", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-3")
        self.ok("ui", "find", "--text", "Results")
        self.ok("pretest", "check", "end", "chk-3", "--verdict", "pass")
        self.ok("pretest", "finding", "add", "--check", "chk-3", "--severity", "minor",
                "--title", "Looks only", "--actual", "a")
        # a check that never began
        self.ok("pretest", "check", "add", "Never begun", "--kind", "ticket")
        self.ok("pretest", "finding", "add", "--check", "chk-4", "--severity", "minor",
                "--title", "Not begun", "--actual", "a")
        events = self.findings_file(session).read_bytes()
        payload = self.refused("unknown_finding", "pretest", "flow-draft", "f-9")
        self.assertEqual(payload["finding_id"], "f-9")
        payload = self.refused("draft_unavailable", "pretest", "flow-draft", "f-2")
        self.assertEqual(payload["check_id"], "chk-3")
        payload = self.refused("draft_unavailable", "pretest", "flow-draft", "f-3")
        self.assertIn("never began", payload["error"])
        busy = self.root / "busy.yaml"
        busy.write_text("mine\n", encoding="utf-8")
        payload = self.refused("output_not_writable", "pretest", "flow-draft", "f-1",
                               "--out", str(busy))
        self.assertIn("already exists", payload["error"])
        self.assertEqual(busy.read_text(encoding="utf-8"), "mine\n")
        self.refused("output_not_writable", "pretest", "flow-draft", "f-1",
                     "--out", str(self.root))
        self.refused("output_not_writable", "pretest", "flow-draft", "f-1",
                     "--out", "~no-such-user-here-x9/d.yaml")
        plain = self.root / "plain"
        plain.mkdir()
        self.refused("not_a_pretest_pack", "pretest", "flow-draft", "f-1", "--pack",
                     str(plain))
        self.refused("not_a_pretest_pack", "pretest", "flow-draft", "f-1", "--pack",
                     str(self.root / "missing"))
        self.assertEqual(self.findings_file(session).read_bytes(), events)
        self.assertEqual(self.drafts(), [])
        # no pre-test, then a packed one
        self.ok("pretest", "stop")
        self.refused("no_active_pretest", "pretest", "flow-draft", "f-1")
        self.ok("pretest", "pack")
        self.refused("pretest_finalized", "pretest", "flow-draft", "f-1")
        self.assertEqual(self.drafts(), [])

    def test_finding_without_a_check_is_unavailable(self) -> None:
        from autonom_lib.pretest import findings as findings_mod
        from autonom_lib.pretest import store

        session, started = self.drive()
        record = store.load(session, started["pretest_id"])
        findings_mod.add(session, record, check_id=None, severity="info", title="t",
                         actual="a")
        with self.assertRaises(errors.AutonomError) as caught:
            regress.flow_draft(session, record, "f-2")
        self.assertEqual(caught.exception.code, errors.DRAFT_UNAVAILABLE)
        self.assertTrue(caught.exception.hint)
        with self.assertRaises(errors.AutonomError) as caught:
            regress.flow_draft(None, record, "f-1")
        self.assertEqual(caught.exception.code, errors.NO_ACTIVE_SESSION)
        self.assertEqual(self.drafts(), [])

    def test_draft_from_a_pack_records_nothing(self) -> None:
        session, _ = self.drive()
        pack = Path(self.ok("pretest", "pack")["pack"])
        before = tree_digest(pack)
        payload = self.ok("pretest", "flow-draft", "f-1", "--pack", str(pack))
        self.assertFalse(payload["recorded"])
        self.assertEqual(Path(payload["pack"]), pack.resolve())
        self.assertTrue(Path(payload["draft_flow"]).is_file())
        self.assertEqual(payload["draft_flow"],
                         str(self.repo / ".autonom/flows/drafts/wb-7-f-1.yaml"))
        self.assertEqual(tree_digest(pack), before)
        self.ok("report", "verify", str(pack))
        # the session is gone from this machine: no journal to compile
        copy = self.root / "pack-copy"
        shutil.copytree(pack, copy)
        session_dir = self.home / "sessions" / session["session_id"]
        self.assertTrue(session_dir.is_dir())
        session_dir.rename(self.home / "gone")
        payload = self.refused("draft_unavailable", "pretest", "flow-draft", "f-1",
                               "--pack", str(copy), "--out", str(self.root / "x.yaml"))
        self.assertEqual(payload.get("session_id"), session["session_id"], payload)
        self.assertFalse((self.root / "x.yaml").exists())


# --- repro -------------------------------------------------------------------------------


def flaky_executor(state: Path, dumps: list[Path]):
    """The real executor on the fake adb, whose screen for run n is dumps[n]."""
    runs = {"n": 0}

    class Flaky(flow_executor.Executor):
        def __init__(self, target, session, config, **kwargs):
            config.default_timeout_ms = 800
            config.interval_ms = 100
            super().__init__(target, session, config, **kwargs)

        def run(self, flow):
            current = json.loads(state.read_text(encoding="utf-8"))
            current["ui_dump"] = str(dumps[min(runs["n"], len(dumps) - 1)])
            state.write_text(json.dumps(current), encoding="utf-8")
            runs["n"] += 1
            return super().run(flow)

    return Flaky


class ReproTests(RegressCase):
    def test_intermittent_one_of_three(self) -> None:
        session, _ = self.drive()
        self.ok("pretest", "flow-draft", "f-1")
        flaky = flaky_executor(self.state, [self.dump, self.empty_dump, self.dump])
        code, payload = self.in_process(
            ["pretest", "repro", "f-1", "--times", "3"],
            mock.patch.object(flow_executor, "Executor", flaky))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["reproduced"], "1/3")
        self.assertEqual(payload["stability"], "intermittent")
        self.assertEqual(len(payload["repro_runs"]), 3)
        self.assertEqual(len(set(payload["repro_runs"])), 3)
        self.assertEqual([run["status"] for run in payload["runs"]],
                         ["passed", "failed", "passed"])
        found = self.ok("pretest", "finding", "list")["findings"]
        self.assertEqual(len(found), 1)  # an intermittent finding stays a finding
        self.assertEqual(found[0]["reproduced"], "1/3")
        self.assertEqual(found[0]["stability"], "intermittent")
        self.assertEqual(found[0]["repro_runs"], payload["repro_runs"])
        flows = Path(session["artifacts_dir"]) / "flows"
        for run_id in payload["repro_runs"]:
            self.assertTrue((flows / run_id).is_dir(), run_id)
        # the repro runs are nested into the pack
        pack = Path(self.ok("pretest", "pack")["pack"])
        summary = json.loads((pack / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["findings"][0]["reproduced"], "1/3")
        for run_id in payload["repro_runs"]:
            self.assertTrue((pack / "runs" / run_id).is_dir(), run_id)

    def test_stable_and_not_reproduced(self) -> None:
        self.drive()
        self.ok("pretest", "flow-draft", "f-1")
        stable = flaky_executor(self.state, [self.empty_dump])
        code, payload = self.in_process(["pretest", "repro", "f-1", "--times", "2"],
                                        mock.patch.object(flow_executor, "Executor", stable))
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["reproduced"], payload["stability"]), ("2/2", "stable"))
        self.write_state(self.dump)
        payload = self.ok("pretest", "repro", "f-1", "--times", "1")
        self.assertEqual((payload["reproduced"], payload["stability"]),
                         ("0/1", "not_reproduced"))
        self.assertEqual(len(payload["repro_runs"]), 1)

    def test_refusals_record_nothing(self) -> None:
        session, _ = self.drive()
        events = self.findings_file(session).read_bytes()
        for times in ("0", "11"):
            payload = self.refused("invalid_value", "pretest", "repro", "f-1",
                                   "--times", times)
            self.assertIn("--times", payload["error"])
        self.refused("draft_unavailable", "pretest", "repro", "f-1", "--times", "1")
        self.refused("unknown_finding", "pretest", "repro", "f-9", "--times", "1")
        plain = self.root / "plain"
        plain.mkdir()
        self.refused("not_a_pretest_pack", "pretest", "repro", "f-1", "--times", "1",
                     "--pack", str(plain))
        self.ok("pretest", "flow-draft", "f-1")
        events = self.findings_file(session).read_bytes()
        self.ok("pretest", "check", "add", "Open one", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-3")
        payload = self.refused("check_already_open", "pretest", "repro", "f-1",
                               "--times", "1")
        self.assertEqual(payload["open_check_id"], "chk-3")
        self.ok("pretest", "check", "end", "chk-3", "--verdict", "pass")
        self.assertEqual(self.findings_file(session).read_bytes(), events)
        self.ok("pretest", "stop")
        self.refused("no_active_pretest", "pretest", "repro", "f-1", "--times", "1")
        pack = Path(self.ok("pretest", "pack")["pack"])
        before = tree_digest(pack)
        payload = self.refused("pretest_finalized", "pretest", "repro", "f-1", "--times",
                               "1", "--pack", str(pack))
        self.assertIn("rerun", payload["hint"])
        self.refused("pretest_finalized", "pretest", "repro", "f-1", "--times", "1")
        self.assertEqual(tree_digest(pack), before)
        self.ok("report", "verify", str(pack))

    def test_a_spent_budget_records_nothing(self) -> None:
        session, _ = self.drive("--max-actions", "2")
        self.ok("pretest", "flow-draft", "f-1")
        events = self.findings_file(session).read_bytes()
        taps = sum(1 for line in self.log.read_text(encoding="utf-8").splitlines()
                   if "tap" in json.loads(line)["argv"])
        payload = self.refused("budget_exhausted", "pretest", "repro", "f-1", "--times", "3")
        self.assertEqual(len(payload["runs"]), 1)  # a guard refusal is not repeated
        self.assertEqual(self.findings_file(session).read_bytes(), events)
        after = sum(1 for line in self.log.read_text(encoding="utf-8").splitlines()
                    if "tap" in json.loads(line)["argv"])
        self.assertEqual(after, taps)

    def test_an_aborted_run_stops_the_loop(self) -> None:
        self.drive()
        self.ok("pretest", "flow-draft", "f-1")

        class Aborting(flow_executor.Executor):
            def run(self, flow):
                self._writer_run_id = "fr_aborted01"
                raise errors.AutonomError(errors.BACKEND_FAILED, "adb went away",
                                          hint="reconnect")

        code, payload = self.in_process(["pretest", "repro", "f-1", "--times", "3"],
                                        mock.patch.object(flow_executor, "Executor", Aborting))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "backend_failed")
        self.assertTrue(payload["hint"])
        self.assertEqual([run["run_id"] for run in payload["runs"]], ["fr_aborted01"])
        found = self.ok("pretest", "finding", "list")["findings"][0]
        self.assertIsNone(found["reproduced"])

    def assert_stopped_early(self, session: dict, events: bytes, payload: dict,
                             statuses: list[str], times: int) -> None:
        """repro stopped before N verdicts: the cause is returned, nothing recorded."""
        self.assertEqual([run["status"] for run in payload["runs"]], statuses, payload)
        self.assertEqual(payload["times"], times)
        self.assertIn(f"run {len(statuses)} of {times}", payload["error"])
        self.assertIn("Nothing was recorded", payload["hint"])
        self.assertEqual(self.findings_file(session).read_bytes(), events)
        found = self.ok("pretest", "finding", "list")["findings"][0]
        self.assertIsNone(found["reproduced"])
        self.assertIsNone(found["stability"])

    def test_a_guard_refusal_after_a_reproduced_run_records_nothing(self) -> None:
        """Run 1 reproduces, run 2 is refused: never "1/1 stable" for --times 3."""
        session, _ = self.drive()
        self.ok("pretest", "flow-draft", "f-1")
        events = self.findings_file(session).read_bytes()
        calls = {"n": 0}
        base = flaky_executor(self.state, [self.empty_dump])

        class SpentAfterOne(base):
            def run(self, flow):
                calls["n"] += 1
                if calls["n"] == 2:
                    self._writer_run_id = "fr_refused02"
                    raise errors.AutonomError(errors.BUDGET_EXHAUSTED, "budget spent",
                                              hint="raise --max-actions")
                return super().run(flow)

        code, payload = self.in_process(
            ["pretest", "repro", "f-1", "--times", "3"],
            mock.patch.object(flow_executor, "Executor", SpentAfterOne))
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], "budget_exhausted")
        self.assertEqual(calls["n"], 2)  # the refusal is not repeated
        self.assert_stopped_early(session, events, payload, ["failed", "broken"], 3)

    def test_a_broken_run_in_the_middle_records_nothing(self) -> None:
        """A finished run whose failure is not the app's is broken, not a verdict."""
        session, _ = self.drive()
        self.ok("pretest", "flow-draft", "f-1")
        events = self.findings_file(session).read_bytes()
        calls = {"n": 0}
        base = flaky_executor(self.state, [self.empty_dump])

        class BrokenSecond(base):
            def run(self, flow):
                calls["n"] += 1
                result = super().run(flow)
                if calls["n"] == 2:
                    result.status = "failed"
                    result.failure = {"failure_class": "infrastructure",
                                      "error_code": "backend_failed", "error": "adb lost"}
                return result

        code, payload = self.in_process(
            ["pretest", "repro", "f-1", "--times", "3"],
            mock.patch.object(flow_executor, "Executor", BrokenSecond))
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], "backend_failed")
        self.assertIn("adb lost", payload["error"])
        self.assertEqual(calls["n"], 2)
        self.assertTrue(all(run["run_id"] for run in payload["runs"]))
        self.assert_stopped_early(session, events, payload, ["failed", "broken"], 3)

    def test_a_guard_refusal_is_never_a_verdict(self) -> None:
        """Whatever status a refused run carries, it does not count as reproduced."""
        refused = {"run_id": "fr_x", "status": "failed", "aborted": False,
                   "failure": {"error_code": "forbidden_action", "failure_class": None}}
        self.assertFalse(regress._conclusive(refused))
        self.assertTrue(regress._conclusive({**refused, "failure": None}))
        self.assertFalse(regress._conclusive({**refused, "failure": None, "aborted": True}))
        for status in ("broken", "skipped"):
            self.assertFalse(regress._conclusive({**refused, "failure": None,
                                                  "status": status}))

    def test_a_damaged_record_refusal_is_a_guard_refusal(self) -> None:
        """A step refused because pretest.json is damaged is never a verdict."""
        self.assertIn(errors.PRETEST_DAMAGED, regress._GUARD_REFUSALS)
        for status in ("failed", "passed"):
            run = {"run_id": "fr_d", "status": status, "aborted": False,
                   "failure": {"error_code": errors.PRETEST_DAMAGED,
                               "failure_class": None}}
            self.assertFalse(regress._conclusive(run), status)


# --- rerun -------------------------------------------------------------------------------


class RerunTests(RegressCase):
    def packed(self) -> tuple[dict, Path]:
        session, _ = self.drive()
        self.ok("pretest", "flow-draft", "f-1")
        return session, Path(self.ok("pretest", "pack")["pack"])

    def test_fail_becomes_pass_and_the_parent_is_unchanged(self) -> None:
        session, pack = self.packed()
        before = tree_digest(pack)
        payload = self.ok("pretest", "rerun", str(pack), "--check", "chk-2")
        self.assertEqual(payload["kind"], "rerun")
        self.assertEqual(payload["rerun_of"], {"pack": str(pack.resolve()),
                                               "checks": ["chk-2"]})
        self.assertEqual(payload["budget"]["max_actions"], 300)
        self.assertEqual(payload["app_id"], APP)
        run = payload["runs"][0]
        self.assertEqual((run["parent_check_id"], run["before"], run["after"]),
                         ("chk-2", "fail", "pass"))
        self.assertEqual(run["source"], "draft:f-1")
        self.assertTrue(run["run_id"])
        status = self.ok("pretest", "status")
        self.assertEqual(status["kind"], "rerun")
        self.assertEqual(status["pretest_id"], payload["pretest_id"])
        child = Path(self.ok("pretest", "pack")["pack"])
        summary = json.loads((child / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["kind"], "rerun")
        check = summary["checks"][0]
        self.assertEqual(check["title"], "Continue twice reaches results")
        self.assertEqual(check["before"]["verdict"], "fail")
        self.assertEqual(check["after"]["verdict"], "pass")
        self.assertEqual(summary["verdict"], "pass")
        self.ok("report", "verify", str(child))
        self.ok("report", "verify", str(pack))
        self.assertEqual(tree_digest(pack), before)

    def test_default_chooses_checks_with_a_flow(self) -> None:
        session, _ = self.drive()
        pack = Path(self.ok("pretest", "pack")["pack"])
        self.ok("pretest", "flow-draft", "f-1", "--pack", str(pack))  # nothing recorded
        payload = self.ok("pretest", "rerun", str(pack))
        self.assertEqual([run["parent_check_id"] for run in payload["runs"]], ["chk-2"])
        self.assertEqual(payload["runs"][0]["after"], "pass")

    def test_a_regression_flow_check_reruns_its_own_flow(self) -> None:
        self.start_session()
        self.ok("pretest", "start", "--ticket", str(self.ticket), "--base", "main",
                "--repo", str(self.repo))
        flow = self.root / "results.yaml"
        flow.write_text("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                        "name: Results visible\n---\n- assertVisible:\n    selector:\n"
                        "      text: Results\n", encoding="utf-8")
        self.ok("pretest", "check", "add", "Results regression", "--kind", "regression",
                "--flow", str(flow))
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "fail")
        pack = Path(self.ok("pretest", "pack")["pack"])
        payload = self.ok("pretest", "rerun", str(pack))
        run = payload["runs"][0]
        self.assertEqual((run["source"], run["flow"]), ("flow", str(flow)))
        self.assertEqual((run["before"], run["after"]), ("fail", "pass"))
        listed = self.ok("pretest", "check", "list")["checks"][0]
        self.assertEqual((listed["kind"], listed["title"]),
                         ("regression", "Results regression"))
        self.assertEqual(listed["run_ids"], [run["run_id"]])

    def race_pack(self, case: str) -> Path:
        self.start_session()
        self.ok("pretest", "start", "--ticket", str(self.ticket), "--base", "main",
                "--repo", str(self.repo))
        flow = self.root / "results.yaml"
        flow.write_text("schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                        "name: Results visible\n---\n- assertVisible:\n    selector:\n"
                        "      text: Results\n", encoding="utf-8")
        self.ok("pretest", "check", "add", "Pay twice", "--kind", "edge", "--case", case,
                "--flow", str(flow))
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "fail")
        return Path(self.ok("pretest", "pack")["pack"])

    def test_a_replayed_race_case_needs_a_count_to_pass(self) -> None:
        pack = self.race_pack("double-tap")
        payload = self.ok("pretest", "rerun", str(pack))
        run = payload["runs"][0]
        self.assertEqual((run["status"], run["before"], run["after"]),
                         ("passed", "fail", "inconclusive"))
        listed = self.ok("pretest", "check", "list")["checks"][0]
        self.assertEqual(listed["verdict"], "inconclusive")
        self.assertIn("--observed-count", listed["note"])
        self.assertIn(f"pretest check amend {listed['check_id']}", listed["note"])
        # the count turns it into a pass, as the note says
        amended = self.ok("pretest", "check", "amend", listed["check_id"], "--verdict", "pass",
                          "--observed-count", "1", "--note", "one request")
        self.assertEqual((amended["verdict"], amended["observed_count"]), ("pass", 1))

    def test_other_kit_cases_still_pass_on_replay(self) -> None:
        pack = self.race_pack("empty-input")
        run = self.ok("pretest", "rerun", str(pack))["runs"][0]
        self.assertEqual(run["after"], "pass")
        self.assertNotIn("--observed-count",
                         self.ok("pretest", "check", "list")["checks"][0].get("note") or "")

    def test_refusals_create_nothing(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.refused("no_active_session", "pretest", "rerun", str(plain))
        session, _ = self.drive()
        pack = Path(self.ok("pretest", "pack")["pack"])
        # no draft yet: nothing to run
        self.refused("draft_unavailable", "pretest", "rerun", str(pack))
        self.refused("draft_unavailable", "pretest", "rerun", str(pack), "--check", "chk-2")
        self.ok("pretest", "flow-draft", "f-1", "--pack", str(pack))
        payload = self.refused("draft_unavailable", "pretest", "rerun", str(pack),
                               "--check", "chk-2", "--check", "chk-1")
        self.assertEqual(payload["checks"], ["chk-1"])
        payload = self.refused("unknown_check", "pretest", "rerun", str(pack),
                               "--check", "chk-9")
        self.assertEqual(payload["check_id"], "chk-9")
        self.refused("not_a_pretest_pack", "pretest", "rerun", str(plain))
        self.refused("not_a_pretest_pack", "pretest", "rerun", str(self.root / "missing"))
        self.assertEqual(len(self.pretests(session)), 1)
        # another pre-test is active
        self.ok("pretest", "start", "--ticket", str(self.ticket), "--base", "main",
                "--repo", str(self.repo))
        self.refused("pretest_already_active", "pretest", "rerun", str(pack))
        self.ok("pretest", "stop")
        self.assertEqual(len(self.pretests(session)), 2)
        # a changed byte in the pack
        target = pack / "summary.md"
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + b"x")
        self.refused("report_integrity_failed", "pretest", "rerun", str(pack))
        self.assertEqual(len(self.pretests(session)), 2)

    def test_an_unexpected_stop_ends_the_check_blocked(self) -> None:
        session, pack = self.packed()

        class Exploding(flow_executor.Executor):
            def run(self, flow):
                raise OSError("disk gone")

        code, payload = self.in_process(
            ["pretest", "rerun", str(pack)],
            mock.patch.object(flow_executor, "Executor", Exploding))
        self.assertEqual(code, 2)
        self.assertIn("disk gone", payload["error"])
        listed = self.ok("pretest", "check", "list")
        self.assertEqual(listed["checks"][0]["state"], "ended")
        self.assertEqual(listed["checks"][0]["verdict"], "blocked")
        self.assertIn("OSError", listed["checks"][0]["note"])

    def test_a_flow_that_no_longer_validates_is_blocked(self) -> None:
        session, pack = self.packed()
        draft = self.repo / ".autonom/flows/drafts/wb-7-f-1.yaml"
        draft.write_text("schema: autonom.dev/flow/v1\nnot: [valid\n", encoding="utf-8")
        payload = self.ok("pretest", "rerun", str(pack))
        run = payload["runs"][0]
        self.assertEqual((run["status"], run["after"]), ("broken", "blocked"))
        self.assertIsNone(run["run_id"])
        self.assertTrue(run["failure"]["error_code"])


if __name__ == "__main__":
    unittest.main()
