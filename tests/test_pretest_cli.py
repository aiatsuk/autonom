"""`autonom pretest` end to end through scripts/autonom.py on the fake devices.

Every case runs the real CLI in a subprocess against the fake adb, with a
temporary ``AUTONOM_HOME`` and a temporary git repository, and reads back only
what the CLI printed and what it wrote under the session: the pre-test files,
the journal, the screenshot index, saved trees and the pack. The fake adb
records every argv it receives, so a refusal is proven to have sent nothing to
the device.

Covered: start (file and stdin ticket, redaction, the 1 MiB cap, refusals
that create nothing), status/context/stop/list, attribution of journal
entries, screenshots and saved trees (and none without a pre-test, tp-028),
the budget refusal, checks and their refusals, findings and evidence refs,
`pretest flows` (pass, a budget-blocked flow, pathless and invalid selected
flows, refusals), health, pack and `report verify` (idempotent re-pack,
tamper, finalized refusals), the QA review (approve, return-to-dev, the
more-checks child, its refusals and its rules taken from the reviewer, never
from a relaxed pack), the kit, the guarded Android `open`, and
the exempt `ui tree` accessibility recovery.
"""
from __future__ import annotations

import contextlib
import errno
import importlib.util
import json
import os
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

UI_DUMP = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,2400]">
        <node index="0" text="" resource-id="com.example.app:id/buy" class="android.view.View" package="com.example.app" content-desc="Купить" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,300][500,420]" />
        <node index="1" text="Continue" resource-id="com.example.app:id/next" class="android.widget.Button" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,600][500,720]" />
        <node index="2" text="Results" resource-id="com.example.app:id/results" class="android.widget.TextView" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[100,900][500,980]" />
      </node>
    </hierarchy>
    """)

SPARSE_DUMP = ('<hierarchy rotation="0"><node class="android.widget.FrameLayout" '
               'package="com.example.app" resource-id="android:id/content" '
               'bounds="[0,0][1080,2400]"><node class="android.view.View" '
               'package="com.example.app" bounds="[0,0][1080,2400]"/>'
               '</node></hierarchy>')

FLOW_PASS = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Results visible
properties:
  covers: "src/*"
---
- assertVisible:
    selector:
      text: Results
"""

FLOW_FIVE_TAPS = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Five taps
properties:
  covers: "src/*"
---
""" + "".join("- tapOn:\n    selector:\n      text: Continue\n" for _ in range(5))

TICKET = """# WB-7 Show results after search

The results list must appear after a search.

Debug: Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123
"""


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_pretest", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PretestCliCase(unittest.TestCase):
    """A private home, fake adb state and log, and a git repo whose working
    tree changes `src/search.dart` against `main`."""

    flow_text: str | None = FLOW_PASS

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.log = self.root / "fake.log"
        self.state = self.root / "state.json"
        self.dump = self.root / "dump.xml"
        self.dump.write_text(UI_DUMP, encoding="utf-8")
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

    def write_state(self, **extra) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(self.dump)}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def make_repo(self) -> None:
        (self.repo / "src").mkdir(parents=True)
        flows = self.repo / ".autonom/flows"
        flows.mkdir(parents=True)

        def git(*argv: str) -> None:
            subprocess.run(["git", *argv], cwd=self.repo, check=True,
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.repo / "src/search.dart").write_text("v1\n", encoding="utf-8")
        if self.flow_text is not None:
            (flows / "results.yaml").write_text(self.flow_text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        (self.repo / "src/search.dart").write_text("v2\n", encoding="utf-8")

    def cli(self, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL, *argv],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=180,
            input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def ok(self, *argv: str, stdin: str | None = None) -> dict:
        completed = self.cli(*argv, stdin=stdin)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        return payload

    def refused(self, code: str, *argv: str, stdin: str | None = None,
                hint: bool = True) -> dict:
        completed = self.cli(*argv, stdin=stdin)
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], code, payload)
        if hint:
            self.assertTrue(payload.get("hint"), f"{code} has no hint")
        return payload

    def in_process(self, cli, argv: list[str], *patches) -> tuple[int, str, str]:
        """``main()`` in this process (so a library function can be patched),
        on the same home, fake state and fake log as the subprocess calls."""
        env = {key: self.env[key] for key in
               ("AUTONOM_HOME", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG")}
        out, err = StringIO(), StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = cli.main(["--adb", str(FAKE_ADB), "--serial", SERIAL, *argv])
        return code, out.getvalue(), err.getvalue()

    def start_session(self) -> dict:
        self.ok("session", "start", "--app-id", APP)
        found = list((self.home / "sessions").glob("s_*/session.json"))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text(encoding="utf-8"))

    def start_pretest(self, *extra: str) -> dict:
        return self.ok("pretest", "start", "--ticket", str(self.ticket), "--base", "main",
                       "--repo", str(self.repo), *extra)

    def artifacts(self, session: dict) -> Path:
        return Path(session["artifacts_dir"])

    def pretests(self, session: dict) -> list[Path]:
        root = self.artifacts(session) / "pretests"
        return sorted(p for p in root.glob("pt_*") if p.is_dir()) if root.is_dir() else []

    def journal(self, session: dict) -> list[dict]:
        path = self.artifacts(session) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def input_taps(self) -> int:
        return sum(1 for argv in self.calls() if "input" in argv and "tap" in argv)

    def am_starts(self) -> list[list[str]]:
        return [argv for argv in self.calls() if "am" in argv and "start" in argv]

    def budget(self, pretest_dir: Path) -> dict:
        return json.loads((pretest_dir / "budget.json").read_text(encoding="utf-8"))


# --- start, status, context, stop, list -------------------------------------------


class StartTests(PretestCliCase):
    def test_refusals_without_a_session_create_nothing(self) -> None:
        for argv in (("pretest", "start", "--ticket", str(self.ticket), "--base", "main",
                      "--repo", str(self.repo)),
                     ("pretest", "status"), ("pretest", "context"), ("pretest", "stop"),
                     ("pretest", "list"), ("pretest", "check", "list"),
                     ("pretest", "finding", "list"), ("pretest", "health"),
                     ("pretest", "pack"), ("pretest", "flows")):
            self.refused("no_active_session", *argv)
        self.assertFalse(list(self.home.rglob("pretests")))

    def test_start_writes_the_four_files_and_redacts_the_ticket(self) -> None:
        session = self.start_session()
        payload = self.start_pretest("--ticket-ref", "WB-7", "--max-actions", "5")
        self.assertEqual(payload["status"], "active")
        self.assertTrue(payload["pretest_id"].startswith("pt_"))
        self.assertEqual(payload["ticket_ref"], "WB-7")
        self.assertEqual(payload["title"], "WB-7 Show results after search")
        self.assertEqual(payload["budget"]["max_actions"], 5)
        self.assertEqual(payload["budget"]["max_minutes"], 60)
        self.assertEqual(payload["budget"]["allow_apps"], [APP])
        self.assertEqual(payload["changed_files_count"], 1)
        self.assertEqual([Path(p).name for p in payload["selected_flows"]], ["results.yaml"])
        directory = Path(payload["dir"])
        self.assertEqual(self.pretests(session), [directory])
        for name in ("pretest.json", "ticket.md", "context.json", "baseline.json"):
            path = directory / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        ticket = (directory / "ticket.md").read_text(encoding="utf-8")
        self.assertIn("The results list must appear", ticket)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123", ticket)
        self.assertIn("<redacted>", ticket)
        active = json.loads((directory.parent / "active.json").read_text(encoding="utf-8"))
        self.assertEqual(active, {"pretest_id": payload["pretest_id"], "check_id": None})

    def test_ticket_from_stdin(self) -> None:
        session = self.start_session()
        payload = self.ok("pretest", "start", "--ticket", "-", "--base", "main",
                          "--repo", str(self.repo), "--title", "From stdin",
                          "--max-minutes", "1.5", "--forbid-text", "danger",
                          "--allow-app", APP, "--allow-app", "com.example.other",
                          stdin="Ticket via pipe\npassword: hunter2\n")
        self.assertEqual(payload["title"], "From stdin")
        self.assertEqual(payload["budget"]["max_minutes"], 1.5)
        self.assertEqual(payload["budget"]["forbid_text"], ["danger"])
        self.assertEqual(payload["budget"]["allow_apps"], [APP, "com.example.other"])
        ticket = (self.pretests(session)[0] / "ticket.md").read_text(encoding="utf-8")
        self.assertIn("Ticket via pipe", ticket)
        self.assertNotIn("hunter2", ticket)

    def test_second_start_and_bad_tickets_create_nothing(self) -> None:
        session = self.start_session()
        self.refused("invalid_value", "pretest", "start", "--ticket",
                     str(self.root / "missing.md"), "--base", "main", "--repo", str(self.repo))
        empty = self.root / "empty.md"
        empty.write_text("  \n", encoding="utf-8")
        self.refused("invalid_value", "pretest", "start", "--ticket", str(empty),
                     "--base", "main", "--repo", str(self.repo))
        self.refused("invalid_value", "pretest", "start", "--ticket", str(self.ticket),
                     "--base", "main", "--repo", str(self.repo), "--max-actions", "0")
        self.refused("invalid_value", "pretest", "start", "--ticket", str(self.ticket),
                     "--base", "main", "--repo", str(self.repo), "--forbid-text", "(")
        for minutes in ("nan", "inf", "-inf", "0", "-1", "abc"):
            payload = self.refused("invalid_value", "pretest", "start", "--ticket",
                                   str(self.ticket), "--base", "main", "--repo", str(self.repo),
                                   f"--max-minutes={minutes}")
            self.assertEqual(payload["flag"], "--max-minutes")
            self.assertIn("finite number > 0", payload["error"])
            self.assertIn("--max-minutes 60", payload["hint"])
        # fix round 5, item 3: --max-actions is refused as invalid_value too
        for actions in ("nan", "inf", "-inf", "0", "-1", "1.5", "abc"):
            payload = self.refused("invalid_value", "pretest", "start", "--ticket",
                                   str(self.ticket), "--base", "main", "--repo", str(self.repo),
                                   f"--max-actions={actions}")
            self.assertEqual(payload["flag"], "--max-actions")
            self.assertIn("--max-actions 300", payload["hint"])
        self.refused("invalid_value", "pretest", "start", "--ticket", str(self.root),
                     "--base", "main", "--repo", str(self.repo))
        locked = self.root / "locked.md"
        locked.write_text("secret ticket", encoding="utf-8")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        if not os.access(locked, os.R_OK):  # running as root reads anything
            payload = self.refused("invalid_value", "pretest", "start", "--ticket",
                                   str(locked), "--base", "main", "--repo", str(self.repo))
            self.assertIn("cannot read", payload["error"])
        self.refused("invalid_value", "pretest", "start", "--ticket", "-", "--base", "main",
                     "--repo", str(self.repo), stdin="")
        # one byte over the 1 MiB cap, from a file and from stdin
        huge = "x" * (1_048_576 + 1)
        big = self.root / "big.md"
        big.write_text(huge, encoding="utf-8")
        payload = self.refused("invalid_value", "pretest", "start", "--ticket", str(big),
                               "--base", "main", "--repo", str(self.repo))
        self.assertIn("larger than 1 MiB", payload["error"])
        payload = self.refused("invalid_value", "pretest", "start", "--ticket", "-",
                               "--base", "main", "--repo", str(self.repo), stdin=huge)
        self.assertIn("larger than 1 MiB", payload["error"])
        self.assertEqual(self.pretests(session), [])
        first = self.start_pretest()
        payload = self.refused("pretest_already_active", "pretest", "start", "--ticket",
                               str(self.ticket), "--base", "main", "--repo", str(self.repo))
        self.assertEqual(payload["pretest_id"], first["pretest_id"])
        self.assertEqual(len(self.pretests(session)), 1)

    def test_verbs_without_a_pretest_are_no_active_pretest(self) -> None:
        self.start_session()
        for argv in (("pretest", "status"), ("pretest", "context"), ("pretest", "stop"),
                     ("pretest", "check", "list"), ("pretest", "check", "add", "x",
                                                      "--kind", "ticket"),
                     ("pretest", "check", "begin", "chk-1"),
                     ("pretest", "check", "end", "chk-1", "--verdict", "pass"),
                     ("pretest", "finding", "list"), ("pretest", "health"),
                     ("pretest", "flows"), ("pretest", "pack")):
            self.refused("no_active_pretest", *argv)
        self.assertEqual(self.ok("pretest", "list")["pretests"], [])

    def test_damaged_context_is_invalid_value_not_a_traceback(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest()["dir"])
        (directory / "context.json").write_text("[1, 2", encoding="utf-8")
        self.refused("invalid_value", "pretest", "context")
        self.refused("invalid_value", "pretest", "flows")
        (directory / "context.json").write_text("[]", encoding="utf-8")
        self.refused("invalid_value", "pretest", "context")
        self.assertEqual(self.ok("pretest", "status")["checks"], 0)

    def test_status_counts_taps_and_context_stop_list(self) -> None:
        session = self.start_session()
        started = self.start_pretest("--max-actions", "5")
        for _ in range(2):
            self.ok("ui", "tap", "--text", "Continue")
        status = self.ok("pretest", "status")
        self.assertEqual(status["actions_used"], 2)
        self.assertEqual(status["actions_limit"], 5)
        self.assertEqual(status["actions_left"], 3)
        self.assertEqual(status["minutes_limit"], 60)
        self.assertIsNone(status["open_check"])
        self.assertEqual((status["checks"], status["findings"]), (0, 0))
        # pre-test verbs themselves never count against the budget
        self.ok("pretest", "status")
        self.ok("pretest", "check", "list")
        self.assertEqual(self.ok("pretest", "status")["actions_used"], 2)
        context = self.ok("pretest", "context")
        self.assertEqual(context["schema"], "autonom.pretest-context/v1")
        self.assertEqual([item["path"] for item in context["changed_files"]],
                         ["src/search.dart"])
        self.ok("pretest", "check", "add", "Results show", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        stopped = self.ok("pretest", "stop")
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(stopped["ended_at"])
        listed = self.ok("pretest", "list")
        self.assertIsNone(listed["active"])
        self.assertEqual([(row["pretest_id"], row["status"]) for row in listed["pretests"]],
                         [(started["pretest_id"], "stopped")])
        checks = [json.loads(line) for line in (self.pretests(session)[0] / "checks.ndjson")
                  .read_text(encoding="utf-8").splitlines()]
        self.assertEqual(checks[-1]["event"], "end")
        self.assertEqual(checks[-1]["verdict"], "inconclusive")
        self.refused("no_active_pretest", "pretest", "status")
        # a new pre-test may start after a stop
        self.start_pretest()


# --- attribution and tp-028 ---------------------------------------------------------


class AttributionTests(PretestCliCase):
    def shot_rows(self, session: dict) -> list[dict]:
        path = self.artifacts(session) / "shots/index.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_no_pretest_no_new_keys(self) -> None:
        session = self.start_session()
        self.ok("ui", "tap", "--text", "Continue")
        self.ok("screenshot")
        tree = self.ok("ui", "tree")
        self.ok("note", "add", "plain note")
        for entry in self.journal(session):
            self.assertNotIn("pretest_id", entry)
            self.assertNotIn("check_id", entry)
        for row in self.shot_rows(session):
            self.assertNotIn("pretest_id", row)
            self.assertNotIn("check_id", row)
            self.assertIsNone(row.get("task"))
        saved = json.loads(Path(tree["saved"]).read_text(encoding="utf-8"))
        for key in ("pretest_id", "check_id", "task"):
            self.assertNotIn(key, saved)
            self.assertNotIn(key, tree)
        self.assertFalse((self.artifacts(session) / "pretests").exists())

    def test_open_check_attributes_journal_screenshots_and_trees(self) -> None:
        session = self.start_session()
        pretest_id = self.start_pretest()["pretest_id"]
        self.ok("pretest", "check", "add", "Results show", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("ui", "tap", "--text", "Continue")
        shot = self.ok("screenshot")
        tree = self.ok("ui", "tree")
        self.ok("note", "add", "saw the results")
        entries = self.journal(session)
        tap = [e for e in entries if e.get("verb") == "ui tap"][-1]
        note = [e for e in entries if e.get("kind") == "note"][-1]
        shot_entry = [e for e in entries if e.get("verb") == "screenshot"][-1]
        for entry in (tap, note, shot_entry):
            self.assertEqual(entry["pretest_id"], pretest_id)
            self.assertEqual(entry["check_id"], "chk-1")
        row = self.shot_rows(session)[-1]
        self.assertEqual(row["task"], "chk-1")
        self.assertEqual(row["pretest_id"], pretest_id)
        self.assertIn("/chk-1/", shot["path"])
        saved = json.loads(Path(tree["saved"]).read_text(encoding="utf-8"))
        self.assertEqual((saved["task"], saved["pretest_id"], saved["check_id"]),
                         ("chk-1", pretest_id, "chk-1"))
        self.assertEqual(tree["task"], "chk-1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "pass")
        self.ok("ui", "tap", "--text", "Continue")
        after = [e for e in self.journal(session) if e.get("verb") == "ui tap"][-1]
        self.assertEqual(after["pretest_id"], pretest_id)
        self.assertNotIn("check_id", after)


# --- budget and guarded verbs ---------------------------------------------------------


class BudgetTests(PretestCliCase):
    def test_third_tap_is_budget_exhausted_and_journaled(self) -> None:
        session = self.start_session()
        directory = Path(self.start_pretest("--max-actions", "2")["dir"])
        for _ in range(2):
            self.ok("ui", "tap", "--text", "Continue")
        before = self.input_taps()
        payload = self.refused("budget_exhausted", "ui", "tap", "--text", "Continue")
        self.assertEqual(payload["pretest_id"], directory.name)
        self.assertEqual(self.input_taps(), before)
        entry = [e for e in self.journal(session) if e.get("verb") == "ui tap"][-1]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error_code"], "budget_exhausted")
        self.assertEqual(entry["pretest_id"], directory.name)
        status = self.ok("pretest", "status")
        self.assertEqual(status["actions_used"], 2)
        self.assertEqual(status["budget"]["refusals"], 1)
        self.assertEqual(self.budget(directory)["refusals"][0]["code"], "budget_exhausted")

    def test_forbidden_text_tap_sends_nothing(self) -> None:
        self.start_session()
        self.start_pretest()
        self.refused("forbidden_action", "ui", "tap", "--desc", "Купить")
        self.refused("forbidden_action", "ui", "tap", "--x", "300", "--y", "360")
        self.assertEqual(self.input_taps(), 0)

    def resolve_to(self, component: str) -> None:
        """The fake package manager answers `resolve-activity` with ``component``."""
        self.write_state(fail={
            f"-s {SERIAL} shell cmd package resolve-activity":
                [0, f"priority=0 preferredOrder=0 match=0x208000\n{component}"]})

    def test_android_open_is_counted_before_any_device_command(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest("--max-actions", "1")["dir"])
        self.resolve_to(f"{APP}/.MainActivity")
        self.log.write_text("", encoding="utf-8")
        opened = self.ok("open", "myapp://results")
        self.assertEqual(opened["opened"], "myapp://results")
        self.assertEqual(len(self.am_starts()), 1)
        self.assertEqual(self.budget(directory)["actions_used"], 1)
        self.log.write_text("", encoding="utf-8")
        self.refused("budget_exhausted", "open", "myapp://results")
        self.assertEqual(self.calls(), [])

    def test_android_open_of_another_app_is_not_allowed(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest()["dir"])
        self.resolve_to("com.android.chrome/com.google.android.apps.chrome.Main")
        self.log.write_text("", encoding="utf-8")
        payload = self.refused("app_not_allowed", "open", "https://example.com/x")
        self.assertEqual(payload["app_id"], "com.android.chrome")
        self.assertEqual(self.am_starts(), [])
        self.assertEqual(self.budget(directory)["refusals"][0]["code"], "app_not_allowed")

    def test_android_open_without_a_pretest_is_unchanged(self) -> None:
        self.start_session()
        self.log.write_text("", encoding="utf-8")
        self.ok("open", "myapp://results")
        words = [" ".join(argv) for argv in self.calls()]
        self.assertFalse(any("resolve-activity" in word for word in words), words)
        self.assertEqual(len(self.am_starts()), 1)

    def test_ui_tree_recovery_is_exempt_even_with_a_spent_budget(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest("--max-actions", "1")["dir"])
        self.ok("ui", "tap", "--text", "Continue")  # the whole budget
        sparse = self.root / "sparse.xml"
        sparse.write_text(SPARSE_DUMP, encoding="utf-8")
        self.write_state(ui_dump=str(sparse), ui_dump_after_launch=str(self.dump),
                         settings_secure={"accessibility_enabled": "0"})
        self.log.write_text("", encoding="utf-8")
        tree = self.ok("ui", "tree")
        self.assertTrue(tree["accessibility_recovery"]["recovered"], tree)
        words = [" ".join(argv) for argv in self.calls()]
        self.assertTrue(any("am force-stop" in word for word in words), words)
        self.assertEqual(len(self.am_starts()), 1)  # relaunched, never left stopped
        self.assertEqual(self.budget(directory), {"actions_used": 1, "refusals": []})


# --- checks and findings ----------------------------------------------------------------


class CheckFindingTests(PretestCliCase):
    def test_check_lifecycle_and_every_refusal(self) -> None:
        self.start_session()
        self.start_pretest()
        added = self.ok("pretest", "check", "add", "Search shows results", "--kind", "ticket",
                        "--why", "acceptance point 1")
        self.assertEqual((added["check_id"], added["state"]), ("chk-1", "added"))
        edge = self.ok("pretest", "check", "add", "Offline search", "--kind", "edge",
                       "--case", "offline")
        self.assertEqual((edge["check_id"], edge["case"]), ("chk-2", "offline"))
        self.refused("invalid_value", "pretest", "check", "add", "x", "--kind", "nope")
        self.refused("invalid_value", "pretest", "check", "add", "x", "--kind", "edge",
                     "--case", "no-such-case")
        self.refused("flow_file_not_found", "pretest", "check", "add", "x", "--kind",
                     "regression", "--flow", str(self.root / "missing.yaml"))
        self.refused("unknown_check", "pretest", "check", "begin", "chk-9")
        self.refused("no_open_check", "pretest", "check", "end", "chk-1", "--verdict", "pass")
        self.ok("pretest", "check", "begin", "chk-1")
        status = self.ok("pretest", "status")
        self.assertEqual(status["open_check"]["check_id"], "chk-1")
        payload = self.refused("check_already_open", "pretest", "check", "begin", "chk-2")
        self.assertEqual(payload["open_check_id"], "chk-1")
        self.refused("invalid_verdict", "pretest", "check", "end", "chk-1",
                     "--verdict", "great")
        self.refused("unknown_check", "pretest", "check", "end", "chk-9", "--verdict", "pass")
        ended = self.ok("pretest", "check", "end", "chk-1", "--verdict", "fail",
                        "--note", "500 not handled")
        self.assertEqual((ended["state"], ended["verdict"]), ("ended", "fail"))
        # an ended check is never ended again, same verdict or not (fix round 3)
        for verdict in ("fail", "pass"):
            again = self.refused("no_open_check", "pretest", "check", "end", "chk-1",
                                 "--verdict", verdict, "--note", "a later note")
            self.assertEqual((again["state"], again["verdict"]), ("ended", "fail"))
            self.assertIn("finding add --check chk-1", again["hint"])
        listed = self.ok("pretest", "check", "list")
        self.assertEqual(listed["count"], 2)
        self.assertIsNone(listed["open_check"])
        first = listed["checks"][0]
        self.assertEqual(first["note"], "500 not handled")
        for key in ("began_at", "ended_at"):
            self.assertTrue(first[key])
        self.assertIsInstance(first["duration_s"], (int, float))
        self.assertEqual(listed["checks"][1]["state"], "added")

    def test_findings_validate_evidence_and_ids(self) -> None:
        session = self.start_session()
        directory = Path(self.start_pretest()["dir"])
        self.ok("pretest", "check", "add", "Results show", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("screenshot")
        findings_file = directory / "findings.ndjson"
        common = ("--title", "500 swallowed", "--actual", "an empty list")
        self.refused("evidence_ref_unknown", "pretest", "finding", "add", "--check", "chk-1",
                     "--severity", "major", *common, "--evidence", "shot:999")
        self.refused("evidence_ref_unknown", "pretest", "finding", "add", "--check", "chk-1",
                     "--severity", "major", *common, "--evidence", "bogus")
        self.refused("invalid_severity", "pretest", "finding", "add", "--check", "chk-1",
                     "--severity", "critical", *common)
        self.refused("unknown_check", "pretest", "finding", "add", "--check", "chk-7",
                     "--severity", "major", *common)
        self.assertFalse(findings_file.exists() and findings_file.read_text(encoding="utf-8"))
        added = self.ok("pretest", "finding", "add", "--check", "chk-1", "--severity",
                        "major", *common, "--expected", "an error message",
                        "--evidence", "shot:1")
        self.assertEqual((added["finding_id"], added["origin"], added["confirmed"]),
                         ("f-1", "agent", True))
        self.assertEqual(added["evidence"], ["shot:1"])
        listed = self.ok("pretest", "finding", "list")
        self.assertEqual([item["finding_id"] for item in listed["findings"]], ["f-1"])
        self.assertTrue(self.journal(session))


# --- flows -------------------------------------------------------------------------------


class FixRound3CliTests(PretestCliCase):
    def mock_states(self, session: dict) -> list[list[tuple]]:
        return [[(m["id"], m.get("status"), m["enabled"]) for m in entry["mocks"]]
                for entry in self.journal(session) if entry.get("kind") == "mock_state"]

    def test_mock_changes_journal_the_registry_only_during_a_pretest(self) -> None:
        session = self.start_session()
        self.ok("network", "mock", "add", "--match", "*/api/old", "--status", "503",
                "--header", "X-Token: hdr-secret")
        self.assertEqual(self.mock_states(session), [])  # no pre-test: nothing new
        self.start_pretest()
        self.assertEqual(self.mock_states(session), [[("m_1", 503, True)]])  # at start
        self.ok("network", "mock", "add", "--url", "https://h.example/api/catalog",
                "--status", "500", "--json", '{"token": "body-secret"}')
        self.ok("network", "mock", "update", "m_2", "--status", "404")
        self.ok("network", "mock", "disable", "m_1")
        self.ok("network", "mock", "list")  # read-only: no entry
        self.refused("mock_not_found", "network", "mock", "remove", "m_9")  # failed: none
        self.ok("network", "mock", "remove", "m_2")
        self.assertEqual(self.mock_states(session), [
            [("m_1", 503, True)],
            [("m_1", 503, True), ("m_2", 500, True)],
            [("m_1", 503, True), ("m_2", 404, True)],
            [("m_1", 503, False), ("m_2", 404, True)],
            [("m_1", 503, False)],
        ])
        entries = self.journal(session)
        # each state right after the change it records, attributed to the pre-test
        for index, entry in enumerate(entries):
            if entry.get("kind") == "mock_state" and index:
                self.assertIn(entries[index - 1].get("verb"), ("network mock", "pretest start"))
                self.assertTrue(entry.get("pretest_id"))
        text = json.dumps(entries)
        for secret in ("hdr-secret", "body-secret"):
            self.assertNotIn(secret, text)
        self.ok("pretest", "stop")
        self.ok("network", "mock", "clear")
        self.assertEqual(len(self.mock_states(session)), 5)  # stopped: nothing new

    def test_swipe_duration_is_seconds(self) -> None:
        self.start_session()
        help_text = self.cli("ui", "swipe", "--help").stdout
        self.assertIn("seconds, e.g. 0.4", " ".join(help_text.split()))
        before = len(self.calls())
        for value in ("400", "60.5", "nan", "inf"):
            payload = self.refused("invalid_value", "ui", "swipe", "--from", "100,800",
                                   "--to", "100,200", "--duration", value)
            self.assertIn("seconds", payload["hint"])
            self.assertIn("0.4", payload["hint"])
            self.assertEqual(payload["max_seconds"], 60.0)
        self.assertEqual(self.refused("invalid_value", "ui", "swipe", "--from", "1,2",
                                      "--to", "3,4", "--duration", "400")["duration"], 400.0)
        self.assertEqual(len(self.calls()), before)  # nothing reached the device
        done = self.ok("ui", "swipe", "--from", "100,800", "--to", "100,200",
                       "--duration", "60")
        self.assertEqual(done["duration"], 60.0)
        self.ok("ui", "swipe", "--from", "100,800", "--to", "100,200", "--duration", "0.4")


class FlowsTests(PretestCliCase):
    def test_selected_flow_becomes_a_passing_regression_check(self) -> None:
        self.start_session()
        self.start_pretest()
        payload = self.ok("pretest", "flows")
        self.assertEqual(payload["count"], 1)
        run = payload["runs"][0]
        self.assertEqual((run["status"], run["verdict"]), ("passed", "pass"))
        self.assertTrue(run["run_id"])
        # one line per flow (fix round 3, item 5)
        self.assertEqual(payload["flows"], [{
            "flow": run["flow"], "check_id": "chk-1", "run_id": run["run_id"],
            "status": "passed", "verdict": "pass"}])
        self.assertTrue(payload["flows"][0]["flow"].endswith("results.yaml"))
        checks = self.ok("pretest", "check", "list")["checks"]
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual((check["kind"], check["verdict"], check["state"]),
                         ("regression", "pass", "ended"))
        self.assertEqual(check["run_ids"], [run["run_id"]])
        self.assertTrue(check["flow"].endswith("results.yaml"))

    def test_flows_refuse_while_a_check_is_open(self) -> None:
        self.start_session()
        self.start_pretest()
        self.ok("pretest", "check", "add", "Manual", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        self.refused("check_already_open", "pretest", "flows")
        self.assertEqual(self.ok("pretest", "check", "list")["count"], 1)

    def test_malformed_env_is_refused_before_any_check(self) -> None:
        self.start_session()
        self.start_pretest()
        # the pre-test verbs answer invalid_value, with the NAME=VALUE form as hint
        for pair in ("NOEQUALS", "=value"):
            with self.subTest(pair=pair):
                payload = self.refused("invalid_value", "pretest", "flows", f"--env={pair}")
                self.assertIn("use NAME=VALUE", payload["hint"])
                self.assertEqual(payload["flag"], "--env")
        for verb in (("warmup",), ("repro", "f-1", "--times", "1")):
            with self.subTest(verb=verb[0]):
                payload = self.refused("invalid_value", "pretest", *verb, "--env", "NOEQUALS")
                self.assertIn("use NAME=VALUE", payload["hint"])
        self.refused("flow_secret_undefined", "pretest", "flows", "--secret",
                     "AUTONOM_TEST_SECRET_THAT_IS_NOT_SET")
        self.assertEqual(self.ok("pretest", "check", "list")["count"], 0)
        # `flow run` keeps its own code, and now names the form too
        flow = self.repo / ".autonom/flows/results.yaml"
        payload = self.refused("flow_command_invalid", "flow", "run", str(flow),
                               "--env", "NOEQUALS")
        self.assertIn("use NAME=VALUE", payload["hint"])


class FlowsInvalidSelectionTests(PretestCliCase):
    """A selected flow with no path or a flow file that fails validation ends
    its own regression check blocked; it is never skipped and sends nothing."""

    def test_pathless_and_invalid_flows_end_their_checks_blocked(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest()["dir"])
        bad = self.repo / ".autonom/flows/bad.yaml"
        bad.write_text("schema: autonom.dev/flow/v1\nname: [unclosed\n---\n- nope\n",
                       encoding="utf-8")
        context_path = directory / "context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["flows"]["selected"] = [{"flow": "", "reasons": ["covers src/*"]},
                                        {"flow": str(bad), "reasons": ["covers src/*"]}]
        context_path.write_text(json.dumps(context), encoding="utf-8")
        self.log.write_text("", encoding="utf-8")
        payload = self.ok("pretest", "flows")
        self.assertEqual(payload["count"], 2)
        pathless, invalid = payload["runs"]
        self.assertEqual((pathless["status"], pathless["verdict"]), ("broken", "blocked"))
        self.assertEqual(pathless["failure"]["error_code"], "flow_file_not_found")
        self.assertEqual(pathless["failure"]["failure_class"], "flow_definition")
        self.assertIsNone(pathless["run_id"])
        self.assertEqual((invalid["status"], invalid["verdict"]), ("broken", "blocked"))
        self.assertTrue(invalid["failure"]["error_code"].startswith("flow_"),
                        invalid["failure"])
        self.assertEqual(invalid["failure"]["failure_class"], "flow_definition")
        self.assertIsNone(invalid["run_id"])
        checks = self.ok("pretest", "check", "list")["checks"]
        self.assertEqual([(c["kind"], c["state"], c["verdict"]) for c in checks],
                         [("regression", "ended", "blocked")] * 2)
        self.assertIsNone(checks[0]["flow"])
        self.assertTrue(checks[1]["flow"].endswith("bad.yaml"))
        self.assertIn("flow_file_not_found", checks[0]["note"])
        self.assertIn(invalid["failure"]["error_code"], checks[1]["note"])
        self.assertIsNone(self.ok("pretest", "status")["open_check"])
        self.assertEqual(self.input_taps(), 0)


class FlowPlatformWarningTests(PretestCliCase):
    """A.10: ``pretest start`` on an Android target with a pull-request flow
    for iOS keeps it selected and shows the platform warning."""

    def test_start_shows_flow_platform_warning(self) -> None:
        (self.repo / ".autonom/flows/ios.yaml").write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: iPhone only\n"
            "tags: [pull-request]\nrequires:\n  platform:\n    - ios\n---\n"
            "- assertVisible:\n    selector:\n      id: x\n", encoding="utf-8")
        self.start_session()
        started = self.start_pretest()
        self.assertEqual(started["flow_warnings"], ["flow iPhone only is for ios"])
        self.assertTrue(any(name.endswith("ios.yaml") for name in started["selected_flows"]))
        context = self.ok("pretest", "context")
        self.assertEqual([item["session_platform"] for item in context["flows"]["warnings"]],
                         ["android"])

    def test_start_without_mismatch_has_no_flow_warnings(self) -> None:
        self.start_session()
        self.assertNotIn("flow_warnings", self.start_pretest())


class FlowsStoppedRunTests(PretestCliCase):
    """A run that stops on an unexpected error still ends its check."""

    def test_an_os_error_mid_run_ends_the_check_blocked(self) -> None:
        self.start_session()
        self.start_pretest()
        cli = load_cli()
        code, _out, err = self.in_process(
            cli, ["pretest", "flows"],
            mock.patch.object(cli.flow_executor.Executor, "run",
                              side_effect=OSError(errno.EIO, "device link lost")))
        self.assertEqual(code, 2, err)
        self.assertEqual(json.loads(err)["error_code"], "backend_failed")
        check = self.ok("pretest", "check", "list")["checks"][0]
        self.assertEqual((check["state"], check["verdict"]), ("ended", "blocked"))
        self.assertEqual(check["note"], "run stopped: OSError")
        self.assertIsNone(self.ok("pretest", "status")["open_check"])


class FlowVerdictMappingTests(unittest.TestCase):
    """tp-008: passed->pass, failed->fail, broken->blocked, all skipped->inconclusive."""

    def test_mapping(self) -> None:
        cli = load_cli()
        step = lambda status: type("Step", (), {"status": status})()  # noqa: E731
        cases = [
            (("passed", None, [step("passed")]), "passed", "pass"),
            (("replayed", None, [step("passed")]), "passed", "pass"),
            (("passed", None, [step("skipped"), step("skipped")]), "skipped", "inconclusive"),
            (("passed", None, [step("skipped"), step("passed")]), "passed", "pass"),
            (("failed", {"failure_class": "test_failure"}, []), "failed", "fail"),
            (("failed", None, [step("failed")]), "failed", "fail"),
            (("failed", {"failure_class": "infrastructure"}, []), "broken", "blocked"),
            (("failed", {"failure_class": "flow_definition"}, []), "broken", "blocked"),
        ]
        for arguments, execution, verdict in cases:
            got = cli._flow_execution(*arguments)  # noqa: SLF001
            self.assertEqual(got, execution, arguments)
            self.assertEqual(cli._FLOW_VERDICTS[got], verdict, arguments)  # noqa: SLF001


class FlowsBudgetTests(PretestCliCase):
    flow_text = FLOW_FIVE_TAPS

    def test_fourth_tap_step_is_budget_exhausted_and_the_check_blocked(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest("--max-actions", "3")["dir"])
        self.log.write_text("", encoding="utf-8")
        payload = self.ok("pretest", "flows")
        run = payload["runs"][0]
        self.assertEqual((run["status"], run["verdict"]), ("broken", "blocked"))
        self.assertEqual(run["failure"]["error_code"], "budget_exhausted")
        self.assertEqual(run["failure"]["failure_class"], "infrastructure")
        self.assertTrue(run["run_id"])
        self.assertEqual(self.input_taps(), 3)
        check = self.ok("pretest", "check", "list")["checks"][0]
        self.assertEqual(check["verdict"], "blocked")
        self.assertIn("budget_exhausted", check["note"])
        self.assertEqual(self.budget(directory)["actions_used"], 3)


class NoFlowsTests(PretestCliCase):
    flow_text = None

    def test_no_selected_flow_is_a_warning(self) -> None:
        self.start_session()
        self.start_pretest()
        payload = self.ok("pretest", "flows")
        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["warnings"][0]["code"], "no_flows_selected")


# --- health, pack, verify, review -------------------------------------------------------


class PackReviewTests(PretestCliCase):
    def build_pack(self) -> tuple[dict, Path]:
        session = self.start_session()
        self.start_pretest()
        self.ok("pretest", "check", "add", "Results show", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("ui", "tap", "--text", "Continue")
        self.ok("screenshot")
        self.ok("pretest", "finding", "add", "--check", "chk-1", "--severity", "major",
                "--title", "500 swallowed", "--actual", "an empty list",
                "--expected", "an error message", "--evidence", "shot:1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "fail")
        self.ok("pretest", "check", "add", "Never run", "--kind", "edge", "--case", "rotation")
        health = self.ok("pretest", "health")
        self.assertEqual(health["health"]["schema"], "autonom.pretest-health/v1")
        self.assertTrue(Path(health["path"]).is_file())
        self.ok("pretest", "flows")
        packed = self.ok("pretest", "pack")
        return session, Path(packed["pack"]), packed

    def snapshot(self, pack: Path) -> dict[str, tuple[int, int]]:
        return {str(p.relative_to(pack)): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in pack.rglob("*") if p.is_file()}

    def test_pack_verify_repack_and_tamper(self) -> None:
        session, pack, packed = self.build_pack()
        self.assertFalse(packed["idempotent"])
        self.assertEqual(packed["verdict"], "fail")
        self.assertEqual(pack, Path(packed["pack"]))
        for name in ("manifest.json", "summary.json", "summary.md", "index.html",
                     "integrity.json", "finalized.json"):
            self.assertTrue((pack / name).is_file(), name)
        verified = self.ok("report", "verify", str(pack))
        self.assertEqual(verified["kind"], "autonom.pretest-pack/v1")
        self.assertEqual(len(verified["nested"]), 1)
        self.assertTrue(verified["nested"][0]["ok"])
        before = self.snapshot(pack)
        again = self.ok("pretest", "pack")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["integrity_sha256"], packed["integrity_sha256"])
        self.assertEqual(self.snapshot(pack), before)
        # a packed pre-test cannot change
        for argv in (("pretest", "check", "add", "late", "--kind", "ticket"),
                     ("pretest", "check", "begin", "chk-2"),
                     ("pretest", "finding", "add", "--check", "chk-1", "--severity", "info",
                      "--title", "t", "--actual", "a"),
                     ("pretest", "health"), ("pretest", "flows")):
            self.refused("pretest_finalized", *argv)
        self.refused("no_active_pretest", "pretest", "status")
        summary = json.loads((pack / "summary.json").read_text(encoding="utf-8"))
        self.assertIn("chk-2", summary["not_covered"])
        self.assertNotIn(str(Path.home()), (pack / "summary.md").read_text(encoding="utf-8"))
        # one changed byte is reported by name
        blob = next(p for p in sorted((pack / "blobs").rglob("*")) if p.is_file())
        blob.chmod(0o600)
        blob.write_bytes(blob.read_bytes() + b"x")
        payload = self.refused("report_integrity_failed", "report", "verify", str(pack))
        self.assertIn(str(blob.relative_to(pack)), [f["path"] for f in payload["failures"]])
        self.assertTrue(self.journal(session))

    def test_report_verify_of_a_plain_directory_is_not_a_pack(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        completed = self.cli("report", "verify", str(empty))
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertNotEqual(json.loads(completed.stderr)["error_code"], "not_a_pretest_pack")

    def test_pack_after_stop_and_out_dir(self) -> None:
        self.start_session()
        self.start_pretest()
        self.ok("pretest", "check", "add", "Results show", "--kind", "ticket")
        self.ok("pretest", "stop")
        out = self.root / "packs/wb-7"
        packed = self.ok("pretest", "pack", "--out", str(out))
        self.assertEqual(Path(packed["pack"]), out.resolve())
        self.assertEqual(packed["verdict"], "not_covered")
        self.ok("report", "verify", str(out))
        not_empty = self.root / "busy"
        not_empty.mkdir()
        (not_empty / "x.txt").write_text("x", encoding="utf-8")
        self.refused("pretest_finalized", "pretest", "pack", "--out", str(not_empty))

    def test_review_decisions_and_more_checks_child(self) -> None:
        session, pack, _ = self.build_pack()
        self.refused("review_needs_finding", "pretest", "review", str(pack), "return-to-dev")
        self.refused("unknown_finding", "pretest", "review", str(pack), "return-to-dev",
                     "--finding", "f-9")
        self.refused("not_automatic", "pretest", "review", str(pack), "approve",
                     "--confirm", "f-1")
        self.refused("invalid_value", "pretest", "review", str(pack), "more-checks")
        self.assertFalse((pack / "annotations").exists()
                         and list((pack / "annotations").iterdir()))
        other = self.root / "not-a-pack"
        other.mkdir()
        self.refused("not_a_pretest_pack", "pretest", "review", str(other), "approve")
        self.refused("not_a_pretest_pack", "pretest", "review-status", str(other))
        reviewed = self.ok("pretest", "review", str(pack), "return-to-dev", "--finding", "f-1",
                           "--note", "500 path missing", "--author", "anna")
        self.assertEqual(reviewed["decision"], "return-to-dev")
        self.assertTrue(Path(reviewed["path"]).is_file())
        self.assertEqual(Path(reviewed["path"]).parent, pack / "annotations")
        self.assertTrue(self.ok("report", "verify", str(pack))["ok"])
        status = self.ok("pretest", "review-status", str(pack))
        self.assertEqual(status["decision"], "return-to-dev")
        self.assertEqual(status["latest"]["author"], "anna")
        # more-checks while another pre-test is active writes nothing
        self.start_pretest()
        count = len(list((pack / "annotations").iterdir()))
        self.refused("pretest_already_active", "pretest", "review", str(pack), "more-checks",
                     "--check", "Rotate on results")
        self.assertEqual(len(list((pack / "annotations").iterdir())), count)
        self.ok("pretest", "stop")
        more = self.ok("pretest", "review", str(pack), "more-checks",
                       "--check", "Rotate on results", "--note", "rotation untested")
        child = more["child"]
        self.assertEqual(child["kind"], "more-checks")
        self.assertEqual(child["status"], "active")
        self.assertEqual(Path(child["parent_pack"]), pack.resolve())
        self.assertEqual([(c["title"], c["state"]) for c in child["checks"]],
                         [("Rotate on results", "added")])
        status = self.ok("pretest", "status")
        self.assertEqual(status["pretest_id"], child["pretest_id"])
        self.assertEqual(status["kind"], "more-checks")
        self.assertEqual(Path(status["parent_pack"]), pack.resolve())
        context = self.ok("pretest", "context")
        self.assertEqual([item["path"] for item in context["changed_files"]],
                         ["src/search.dart"])
        self.assertEqual(self.ok("pretest", "review-status", str(pack))["decision"],
                         "more-checks")
        self.assertTrue(self.ok("report", "verify", str(pack))["ok"])
        self.assertTrue(session)

    def test_review_approve_is_recorded_and_the_pack_still_verifies(self) -> None:
        _, pack, packed = self.build_pack()
        approved = self.ok("pretest", "review", str(pack), "approve",
                           "--note", "looks right", "--author", "lena")
        self.assertEqual(approved["decision"], "approve")
        self.assertEqual(approved["schema"], "autonom.pretest-review/v1")
        self.assertEqual(approved["note"], "looks right")
        self.assertEqual(approved["author"], "lena")
        self.assertEqual(approved["pack_integrity_sha256"], packed["integrity_sha256"])
        self.assertNotIn("child", approved)
        path = Path(approved["path"])
        self.assertEqual(path, pack / "annotations" / "review-1.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["decision"], "approve")
        status = self.ok("pretest", "review-status", str(pack))
        self.assertEqual(status["decision"], "approve")
        self.assertEqual(status["latest"]["author"], "lena")
        self.assertTrue(self.ok("report", "verify", str(pack))["ok"])
        # approve starts no child pre-test
        self.refused("no_active_pretest", "pretest", "status")

    def test_more_checks_refuses_a_pack_with_unreadable_ticket_or_bad_context(self) -> None:
        _, pack, _ = self.build_pack()
        cli = load_cli()
        from autonom_lib.pretest import pack as pack_mod

        # the pack's integrity check would refuse first; skip it to reach the
        # child's own reads of ticket.md and context.json
        skip_verify = mock.patch.object(pack_mod, "verify", return_value=None)
        ticket = pack / "ticket.md"
        saved = ticket.read_bytes()
        ticket.chmod(0o600)
        ticket.unlink()
        ticket.symlink_to(self.ticket)  # not a regular file: never opened
        code, _out, err = self.in_process(
            cli, ["pretest", "review", str(pack), "more-checks", "--check", "Rotate"],
            skip_verify)
        self.assertEqual(code, 2, err)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "not_a_pretest_pack")
        self.assertIn("cannot read the pack's ticket or context", payload["error"])
        self.assertIn("decision was recorded", payload["error"])
        self.assertIn("autonom report verify", payload["hint"])
        self.assertEqual(Path(payload["path"]), pack.resolve())
        self.assertTrue(Path(payload["review"]).is_file())
        self.refused("no_active_pretest", "pretest", "status")
        ticket.unlink()
        ticket.write_bytes(saved)
        context = pack / "context.json"
        context.chmod(0o600)
        context.write_text("[]", encoding="utf-8")
        code, _out, err = self.in_process(
            cli, ["pretest", "review", str(pack), "more-checks", "--check", "Rotate"],
            skip_verify)
        self.assertEqual(code, 2, err)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "not_a_pretest_pack")
        self.assertIn("context.json is not an object", payload["error"])
        self.assertIn("autonom report verify", payload["hint"])
        self.assertTrue(Path(payload["review"]).is_file())
        self.refused("no_active_pretest", "pretest", "status")

    def annotations(self, pack: Path) -> list[str]:
        directory = pack / "annotations"
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def test_a_relaxed_pack_cannot_loosen_the_more_checks_child_guard(self) -> None:
        from autonom_lib.pretest import store as store_mod

        defaults = {"max_actions": 300, "max_minutes": 60,
                    "forbid_text": list(store_mod.DEFAULT_FORBID_TEXT), "allow_apps": [APP]}
        self.start_session()
        relaxed = self.start_pretest("--forbid-text", "zzzz", "--allow-app", "com.bank.app",
                                     "--app-id", "com.bank.app", "--max-actions", "100000",
                                     "--max-minutes", "600")
        self.assertEqual(relaxed["budget"]["forbid_text"], ["zzzz"])
        self.ok("pretest", "check", "add", "Results show", "--kind", "ticket")
        pack = Path(self.ok("pretest", "pack")["pack"])
        self.assertTrue(self.ok("report", "verify", str(pack))["ok"])

        # the review's own flags are refused for decisions that start nothing
        payload = self.refused("invalid_value", "pretest", "review", str(pack), "approve",
                               "--max-actions", "5", "--app-id", "com.other")
        self.assertIn("--app-id, --max-actions only applies to more-checks", payload["error"])
        self.assertEqual(payload["flags"], ["--app-id", "--max-actions"])
        # bad child rules are refused before the decision is written
        for bad in (("--max-actions", "0"), ("--max-minutes", "nan"),
                    ("--max-minutes", "-1"), ("--forbid-text", "("),
                    ("--max-minutes=inf",), ("--max-minutes", "0"),
                    ("--max-actions", "nan"), ("--max-actions=-inf",),
                    ("--max-actions", "-2")):
            self.refused("invalid_value", "pretest", "review", str(pack), "more-checks",
                         "--check", "Rotate", *bad)
        self.assertEqual(self.annotations(pack), [])
        self.refused("no_active_pretest", "pretest", "status")

        # without flags: this session's app and the defaults, not the pack's rules
        child = self.ok("pretest", "review", str(pack), "more-checks",
                        "--check", "Rotate on results")["child"]
        self.assertEqual(child["app_id"], APP)
        self.assertEqual(child["budget"], defaults)
        status = self.ok("pretest", "status")
        self.assertEqual(status["pretest_id"], child["pretest_id"])
        self.assertEqual(status["app_id"], APP)
        self.assertEqual(status["actions_limit"], 300)
        self.assertEqual(status["minutes_limit"], 60)
        self.log.write_text("", encoding="utf-8")
        self.refused("forbidden_action", "ui", "tap", "--desc", "Купить")
        self.assertEqual(self.input_taps(), 0)
        self.write_state(fail={
            f"-s {SERIAL} shell cmd package resolve-activity":
                [0, "priority=0 preferredOrder=0 match=0x208000\ncom.bank.app/.Main"]})
        self.log.write_text("", encoding="utf-8")
        refused = self.refused("app_not_allowed", "open", "bank://home")
        self.assertEqual(refused["app_id"], "com.bank.app")
        self.assertEqual(self.am_starts(), [])
        self.write_state()
        self.ok("pretest", "stop")

        # explicit flags set the child's rules
        child = self.ok("pretest", "review", str(pack), "more-checks", "--check", "Rotate",
                        "--app-id", "com.other", "--max-actions", "5",
                        "--max-minutes", "2.5", "--forbid-text", "Continue",
                        "--allow-app", "com.other", "--allow-app", APP)["child"]
        self.assertEqual(child["app_id"], "com.other")
        self.assertEqual(child["budget"], {"max_actions": 5, "max_minutes": 2.5,
                                           "forbid_text": ["Continue"],
                                           "allow_apps": ["com.other", APP]})
        self.log.write_text("", encoding="utf-8")
        self.refused("forbidden_action", "ui", "tap", "--text", "Continue")
        self.assertEqual(self.input_taps(), 0)
        self.assertEqual(len(self.annotations(pack)), 2)
        self.assertTrue(self.ok("report", "verify", str(pack))["ok"])

    def test_more_checks_child_failure_names_the_recorded_review(self) -> None:
        _, pack, _ = self.build_pack()
        cli = load_cli()
        from autonom_lib import errors as errors_mod
        from autonom_lib.pretest import store as store_mod

        failure = errors_mod.AutonomError(errors_mod.BACKEND_FAILED, "git went away",
                                          hint="Check git.")
        code, _out, err = self.in_process(
            cli, ["pretest", "review", str(pack), "more-checks", "--check", "Rotate"],
            mock.patch.object(store_mod, "create", side_effect=failure))
        self.assertEqual(code, 2, err)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "backend_failed")
        self.assertIn("decision was recorded", payload["error"])
        self.assertTrue(payload["hint"].startswith("Check git."))
        self.assertTrue(Path(payload["review"]).is_file())
        self.assertEqual(self.ok("pretest", "review-status", str(pack))["decision"],
                         "more-checks")
        self.refused("no_active_pretest", "pretest", "status")


# --- kit ---------------------------------------------------------------------------------


class DamagedRecordCliTests(PretestCliCase):
    """B.8 / RISK-042, the CLI part: `pretest status` reports a damaged
    active record and `pretest stop --force` stops it."""

    def test_status_reports_damage_and_stop_force_recovers(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest()["dir"])
        record = directory / "pretest.json"
        record.write_text("{not json", encoding="utf-8")
        status = self.ok("pretest", "status")
        self.assertEqual(status["status"], "damaged")
        self.assertTrue(status["reason"].startswith("not valid JSON"))
        self.assertEqual(status["path"], str(record))
        self.assertIn("pretest stop --force", status["hint"])
        before = self.input_taps()
        refused = self.refused("pretest_damaged", "ui", "tap", "--text", "Continue")
        self.assertIn("pretest stop --force", refused["hint"])
        self.assertEqual(self.input_taps(), before)  # nothing reached the device
        self.refused("pretest_damaged", "pretest", "start", "--ticket", str(self.ticket),
                     "--base", "main", "--repo", str(self.repo))
        help_text = self.cli("pretest", "stop", "--help").stdout
        self.assertIn("--force", help_text)
        stopped = self.ok("pretest", "stop", "--force")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["damaged"]["file"], "pretest.json.damaged")
        self.assertEqual((directory / "pretest.json.damaged").read_text(encoding="utf-8"),
                         "{not json")
        self.refused("no_active_pretest", "pretest", "status")
        self.ok("ui", "tap", "--text", "Continue")  # the guard is gone with the pre-test

    def test_stop_without_force_still_recovers(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest()["dir"])
        (directory / "pretest.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.ok("pretest", "status")["status"], "damaged")
        self.assertEqual(self.ok("pretest", "stop")["status"], "stopped")

    def test_status_of_a_healthy_pretest_is_unchanged(self) -> None:
        self.start_session()
        self.start_pretest()
        status = self.ok("pretest", "status")
        self.assertEqual(status["status"], "active")
        self.assertNotIn("reason", status)


class SmallerMessagesTests(PretestCliCase):
    """The smaller CLI items: --ticket <dir>, flow-draft --out '' and its
    message, each warning printed once."""

    def test_ticket_directory_is_a_specific_invalid_value(self) -> None:
        self.start_session()
        payload = self.refused("invalid_value", "pretest", "start", "--ticket", str(self.root),
                               "--base", "main", "--repo", str(self.repo))
        self.assertIn("--ticket must be a file", payload["error"])
        self.assertIn("directory", payload["error"])
        self.assertIn("'-'", payload["hint"])
        self.assertFalse(list(self.home.rglob("pretests/pt_*")))

    def test_flow_draft_empty_out_is_refused_first(self) -> None:
        for value in ("", "  "):
            with self.subTest(out=repr(value)):
                payload = self.refused("invalid_value", "pretest", "flow-draft", "f-1",
                                       f"--out={value}")
                self.assertEqual(payload["flag"], "--out")
                self.assertIn("default", payload["hint"])

    def test_flow_draft_says_what_was_written_and_where(self) -> None:
        cli = load_cli()
        base = {"finding_id": "f-1", "check_id": "chk-2",
                "draft_flow": "/repo/.autonom/flows/drafts/wb-7-f-1.yaml", "pack": None}
        cases = [
            ({"written": True, "recorded": True},
             "wrote the draft flow to /repo/.autonom/flows/drafts/wb-7-f-1.yaml; "
             "recorded it on finding f-1."),
            ({"written": False, "recorded": False},
             "the draft flow at /repo/.autonom/flows/drafts/wb-7-f-1.yaml already holds this "
             "flow; nothing was written; finding f-1 already names it."),
            ({"written": True, "recorded": False, "pack": "/packs/wb-7"},
             "wrote the draft flow to /repo/.autonom/flows/drafts/wb-7-f-1.yaml; the pack is "
             "final, so nothing was recorded on the finding."),
        ]
        for extra, start in cases:
            with self.subTest(extra=extra):
                message = cli._flow_draft_message({**base, **extra})
                self.assertTrue(message.startswith(start), message)
                self.assertTrue(message.endswith("Review its closing assertion before "
                                                 "you run it."))
        # through the verb: a pack's draft points at rerun (never at repro, refused
        # there) when rerun would replay this very file
        result = {**base, "written": True, "recorded": False, "pack": "/packs/wb-7",
                  "repro": "autonom pretest repro f-1 --times 3", "warnings": []}
        planned = {"plan": [{"check": {"check_id": "chk-2"}, "flow": base["draft_flow"],
                             "source": "draft:f-1"}]}
        with mock.patch("autonom_lib.pretest.regress.plan_rerun",
                        return_value=planned) as plan_rerun:
            code, out, _ = self.in_process(
                cli, ["pretest", "flow-draft", "f-1", "--pack", "/packs/wb-7"],
                mock.patch("autonom_lib.pretest.regress.flow_draft", return_value=dict(result)))
        self.assertEqual(code, 0)
        plan_rerun.assert_called_once_with("/packs/wb-7", ["chk-2"])
        payload = json.loads(out)
        self.assertEqual(payload["repro"], "autonom pretest rerun <pack> --check chk-2")
        self.assertIn("nothing was recorded", payload["message"])
        self.assertNotIn("does not replay", payload["message"])

    def test_flow_draft_pack_custom_out_points_at_flow_run(self) -> None:
        # A draft rerun would not find (custom --out, or the check has its own flow,
        # or rerun is refused) is replayed with flow run on the written file.
        cli = load_cli()
        draft = "/tmp/my drafts/f1.yaml"
        result = {"finding_id": "f-1", "check_id": "chk-2", "draft_flow": draft,
                  "written": True, "recorded": False, "pack": "/packs/wb-7",
                  "repro": "autonom pretest repro f-1 --times 3", "warnings": []}
        other = {"plan": [{"check": {"check_id": "chk-2"},
                           "flow": "/repo/flows/own.yaml", "source": "flow"}]}
        refused = cli.errors.AutonomError(cli.errors.DRAFT_UNAVAILABLE,
                                          "chk-2 has no flow or draft", hint="h")
        for name, patch in (("other flow", {"return_value": other}),
                            ("rerun refused", {"side_effect": refused})):
            with self.subTest(name), \
                    mock.patch("autonom_lib.pretest.regress.plan_rerun", **patch):
                code, out, _ = self.in_process(
                    cli, ["pretest", "flow-draft", "f-1", "--pack", "/packs/wb-7",
                          "--out", draft],
                    mock.patch("autonom_lib.pretest.regress.flow_draft",
                               return_value=dict(result)))
                self.assertEqual(code, 0)
                payload = json.loads(out)
                self.assertEqual(payload["repro"], "autonom flow run '/tmp/my drafts/f1.yaml'")
                self.assertIn("'autonom pretest rerun <pack> --check chk-2' does not replay "
                              "this file", payload["message"])
                self.assertIn("autonom flow run", payload["message"])

    def test_each_warning_is_printed_once(self) -> None:
        cli = load_cli()
        twice = {"code": "no_final_assertion", "error": "e", "hint": "h"}
        other = {"code": "no_changed_files", "error": "e", "hint": "h"}
        self.assertEqual(cli._unique_warnings([twice, dict(twice), other, twice, "x", "x"]),
                         [twice, other, "x"])
        self.assertEqual(cli._unique_warnings([]), [])
        out = StringIO()
        with redirect_stdout(out):
            cli.emit({"ok": True, "warnings": [twice, dict(twice), other]}, as_json=True)
        self.assertEqual(json.loads(out.getvalue())["warnings"], [twice, other])
        # a warnings value that is not a list is left alone
        out = StringIO()
        with redirect_stdout(out):
            cli.emit({"ok": True, "warnings": "x"}, as_json=True)
        self.assertEqual(json.loads(out.getvalue())["warnings"], "x")


class MockTimingCliTests(PretestCliCase):
    """A.2 flags and A.5 Content-Type wiring of `network mock add|update`; no
    session or device is needed (the registry is machine state)."""

    DELAY_HINT = "delay is seconds in (0, 120], e.g. 2.5"

    def registry(self) -> list[dict]:
        return self.ok("network", "mock", "list", "--all")["mocks"]

    def test_delay_and_hang_are_stored_and_listed(self) -> None:
        added = self.ok("network", "mock", "add", "--match", "*/api/slow*", "--delay", "2.5")
        rule = added["mock"]
        self.assertEqual(rule["delay_s"], 2.5)
        self.assertNotIn("hang", rule)
        held = self.ok("network", "mock", "add", "--match", "*/api/stuck*", "--hang")["mock"]
        self.assertIs(held["hang"], True)
        self.assertNotIn("delay_s", held)
        listed = {item["id"]: item for item in self.registry()}
        self.assertEqual(listed[rule["id"]]["delay_s"], 2.5)
        self.assertIs(listed[held["id"]]["hang"], True)
        # update swaps one for the other; other fields stay
        swapped = self.ok("network", "mock", "update", rule["id"], "--hang")["mock"]
        self.assertIs(swapped["hang"], True)
        self.assertNotIn("delay_s", swapped)
        swapped = self.ok("network", "mock", "update", rule["id"], "--delay", "120")["mock"]
        self.assertEqual(swapped["delay_s"], 120.0)
        self.assertNotIn("hang", swapped)
        untouched = self.ok("network", "mock", "update", rule["id"], "--status", "503")["mock"]
        self.assertEqual(untouched["delay_s"], 120.0)

    def test_bad_delay_is_refused_and_writes_nothing(self) -> None:
        self.ok("network", "mock", "add", "--match", "*/api/x*")
        before = self.registry()
        for argv in (("--delay=0",), ("--delay=-1",), ("--delay=120.5",), ("--delay=nan",),
                     ("--delay=inf",), ("--delay=abc",), ("--delay", "1", "--hang"),
                     ("--delay=2_5",), ("--delay=1e1",)):
            with self.subTest(argv=argv):
                payload = self.refused("invalid_value", "network", "mock", "add",
                                       "--match", "*/api/y*", *argv)
                self.assertEqual(payload["hint"], self.DELAY_HINT)
                payload = self.refused("invalid_value", "network", "mock", "update", "m_1",
                                       *argv)
                self.assertEqual(payload["hint"], self.DELAY_HINT)
        self.assertEqual(self.registry(), before)

    def test_body_file_content_type_follows_the_extension(self) -> None:
        bodies = self.root / "bodies"
        bodies.mkdir()
        cases = {"items.json": "application/json", "page.HTML": "text/html; charset=utf-8",
                 "a.txt": "text/plain; charset=utf-8", "a.xml": "application/xml",
                 "a.png": "image/png", "a.jpeg": "image/jpeg",
                 "a.bin": "application/octet-stream"}
        for name, expected in cases.items():
            with self.subTest(name=name):
                path = bodies / name
                path.write_bytes(b"{}")
                rule = self.ok("network", "mock", "add", "--match", f"*/{name}*",
                               "--body-file", str(path))["mock"]
                self.assertEqual(rule["response"]["headers"], {"Content-Type": expected})
        explicit = self.ok("network", "mock", "add", "--match", "*/explicit*", "--body-file",
                           str(bodies / "items.json"), "--header",
                           "content-type: text/plain")["mock"]
        self.assertEqual(explicit["response"]["headers"], {"content-type": "text/plain"})
        inline = self.ok("network", "mock", "add", "--match", "*/inline*",
                         "--json", '{"a": 1}')["mock"]
        self.assertEqual(inline["response"]["headers"], {"Content-Type": "application/json"})
        text = self.ok("network", "mock", "add", "--match", "*/text*",
                       "--json", "plain words")["mock"]
        self.assertEqual(text["response"]["headers"], {})

    def test_help_lists_delay_and_hang(self) -> None:
        for verb in ("add", "update"):
            text = self.cli("network", "mock", verb, "--help").stdout
            self.assertIn("--delay", text)
            self.assertIn("--hang", text)


class KitTests(PretestCliCase):
    def test_kit_lists_eighteen_cases_and_coverage_when_active(self) -> None:
        plain = self.ok("pretest", "kit")
        self.assertEqual(plain["count"], 18)
        ids = [case["id"] for case in plain["kit"]]
        self.assertEqual(len(set(ids)), 18)
        for expected in ("offline", "server-error", "rotation", "deep-link"):
            self.assertIn(expected, ids)
        self.assertNotIn("coverage", plain)
        self.start_session()
        self.start_pretest()
        self.ok("pretest", "check", "add", "Offline", "--kind", "edge", "--case", "offline")
        self.ok("pretest", "check", "add", "Slow", "--kind", "edge", "--case", "slow")
        self.ok("pretest", "check", "begin", "chk-1")
        covered = self.ok("pretest", "kit")
        rows = {row["id"]: row for row in covered["coverage"]}
        self.assertTrue(rows["offline"]["covered"])
        self.assertEqual(rows["slow"]["checks"], ["chk-2"])
        self.assertFalse(rows["slow"]["covered"])  # planned, never begun
        self.assertFalse(rows["rotation"]["covered"])


if __name__ == "__main__":
    unittest.main()
