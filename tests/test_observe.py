"""Observe after an action (AL-11): `ui tap|type|swipe|key` return the screen
they lead to.

The oracle is what the fakes executed. Each CLI case runs the real CLI in a
subprocess against `fake_adb.py` or `fake_idb.py` through a small wrapper
(written per test into its temporary directory) that changes the screen the
fake reports once the first input has been sent, so the read after the input
differs from the one before it. The fake call log then proves that exactly
one input was sent and how many reads observe made; the reply and the saved
tree history prove what it reported. The same wrapper can send the CLI a
real SIGINT from a chosen device call after the input (state key
``sigint_on``), the way Ctrl-C reaches it while observe waits on the device.
No real device.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
SERIAL = "emulator-5610"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
SECRET = "hunter2-Secret"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import errors, observe, ui  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


# --- screens --------------------------------------------------------------------------------


def android_dump(package: str, rows: list[tuple[str, str, str]]) -> str:
    """A UI Automator dump: a root FrameLayout and one row per (class, text,
    resource id), stacked 120 px apart."""
    nodes = []
    for index, (klass, text, rid) in enumerate(rows):
        top = 100 + index * 120
        clickable = "true" if klass.endswith(("Button", "LinearLayout")) else "false"
        nodes.append(
            f'    <node index="{index}" text="{text}" resource-id="{rid}" class="{klass}" '
            f'package="{package}" content-desc="" checkable="false" checked="false" '
            f'clickable="{clickable}" enabled="true" focusable="true" focused="false" '
            f'scrollable="false" long-clickable="false" password="false" selected="false" '
            f'bounds="[40,{top}][1040,{top + 100}]" />')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
            '<hierarchy rotation="0">\n'
            f'  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
            f'package="{package}" content-desc="" checkable="false" checked="false" '
            f'clickable="false" enabled="true" focusable="false" focused="false" '
            f'scrollable="false" long-clickable="false" password="false" selected="false" '
            f'bounds="[0,0][1080,1920]">\n'
            + "\n".join(nodes) + "\n  </node>\n</hierarchy>\n")


SETTINGS_MAIN = [("android.widget.TextView", "Settings", "android:id/title"),
                 ("android.widget.Button", "Network &amp; internet", ""),
                 ("android.widget.Button", "General", ""),
                 ("android.widget.Button", "Display", "")]
SETTINGS_GENERAL = [("android.widget.TextView", "General", "android:id/title"),
                    ("android.widget.Button", "About", ""),
                    ("android.widget.Button", "Language", "")]
FIELD_EMPTY = [("android.widget.EditText", "", "com.example.app:id/password")]
FIELD_TYPED = [("android.widget.EditText", SECRET, "com.example.app:id/password")]
CARD = "4111111111111111"
CARD_EMPTY = [("android.widget.TextView", "Card number", "com.example.app:id/label"),
              ("android.widget.EditText", "", "com.example.app:id/card"),
              ("android.widget.Button", "Pay", "com.example.app:id/pay")]
# the field groups the digits, and a summary repeats the last four
CARD_TYPED = [("android.widget.TextView", "Card number", "com.example.app:id/label"),
              ("android.widget.EditText", "4111 1111 1111 1111", "com.example.app:id/card"),
              ("android.widget.TextView", "Visa ending in 1111", "com.example.app:id/summary"),
              ("android.widget.Button", "Pay", "com.example.app:id/pay")]
CODE = "394817"
# a hidden field takes the code and six plain text boxes show one digit each
CODE_EMPTY = ([("android.widget.EditText", "", "com.example.app:id/code")]
              + [("android.widget.TextView", "", f"com.example.app:id/box{i}") for i in range(6)]
              + [("android.widget.Button", "Verify", "com.example.app:id/verify")])
CODE_TYPED = ([("android.widget.EditText", "", "com.example.app:id/code")]
              + [("android.widget.TextView", digit, f"com.example.app:id/box{i}")
                 for i, digit in enumerate(CODE)]
              + [("android.widget.Button", "Verify", "com.example.app:id/verify")])


def ios_tree(heading: str, buttons: list[str]) -> list[dict[str, Any]]:
    """An idb describe-all payload: the Settings application root, a
    heading and one button per label."""
    def element(kind: str, label: str | None, y: int, height: int, width: int = 370,
                x: int = 16) -> dict[str, Any]:
        return {"type": kind, "role": f"AX{kind}", "AXLabel": label, "AXValue": None,
                "AXUniqueId": None, "enabled": True,
                "frame": {"x": x, "y": y, "width": width, "height": height}}
    payload = [element("Application", "Settings", 0, 874, width=402, x=0),
               element("Heading", heading, 120, 40)]
    for index, label in enumerate(buttons):
        payload.append(element("Button", label, 200 + index * 60, 52))
    return payload


# --- wrappers -------------------------------------------------------------------------------

# Runs the fake, then (once the first input has been sent) applies the state
# keys under `after_input`, so the next read shows the next screen. When the
# state has `sigint_on` (a list of argv words) and a call carries all of
# them, the wrapper sends its parent, the CLI, one SIGINT (Ctrl-C) and waits:
# the CLI is interrupted while it waits on that device call, and ends it.
WRAPPER = '''#!{python}
import json, os, runpy, signal, sys, time

FAKE = {fake!r}
TOOL = {tool!r}


def interrupt_parent(argv):
    path = os.environ["AUTONOM_FAKE_STATE"]
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return
    words = state.get("sigint_on")
    if not words or not all(word in argv for word in words):
        return
    state.pop("sigint_on")  # once
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(10)  # the interrupted CLI kills this call long before


def is_input(argv):
    if TOOL == "adb":
        return "shell" in argv and argv[argv.index("shell") + 1:argv.index("shell") + 2] == ["input"]
    rest = [arg for arg in argv if not arg.startswith("-")]
    return rest[:1] == ["ui"] and rest[1:2] in (["tap"], ["swipe"], ["text"], ["key"], ["button"])


argv = sys.argv[1:]
interrupt_parent(argv)
try:
    sys.argv = [FAKE, *argv]
    runpy.run_path(FAKE, run_name="__main__")
finally:
    if is_input(argv):
        path = os.environ["AUTONOM_FAKE_STATE"]
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        pending = state.pop("after_input", None) or {{}}
        state.update(pending)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
'''


class ObserveCase(EnvSandboxMixin, unittest.TestCase):
    """A private AUTONOM_HOME, fake adb/simctl/idb behind the screen-changing
    wrapper, and a PATH without any real tool."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.adb = self.wrapper("adb", FAKE_ADB)
        self.idb = self.wrapper("idb", FAKE_IDB)
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_IDB=None, AUTONOM_AXE=None, AUTONOM_IOS_HID=None,
                     AUTONOM_IDB_COMPANION=None, AUTONOM_UI_BACKEND=None,
                     AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"),
                     AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None)
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])

    def wrapper(self, tool: str, fake: Path) -> Path:
        path = self.root / f"wrapped_{tool}.py"
        path.write_text(WRAPPER.format(python=sys.executable, fake=str(fake), tool=tool),
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def screen(self, name: str, text: str) -> str:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def write_state(self, state: dict[str, Any]) -> None:
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[int, dict[str, Any], str]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        return completed.returncode, json.loads(stream), completed.stdout

    def calls(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"] for row in rows if row.get("tool") == tool]

    def forget_calls(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def trees(self, session_id: str) -> list[Path]:
        return sorted((self.home / "sessions" / session_id / "trees").glob("[0-9]*.json"))


# --- Android ----------------------------------------------------------------------------------


class AndroidObserveTests(ObserveCase):

    def setUp(self) -> None:
        super().setUp()
        self.main = self.screen("main.xml", android_dump("com.android.settings", SETTINGS_MAIN))
        self.general = self.screen("general.xml",
                                   android_dump("com.android.settings", SETTINGS_GENERAL))
        self.write_state({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": self.main, "foreground_app": "com.android.settings",
            "after_input": {"ui_dump": self.general}})

    def android(self, *argv: str) -> tuple[int, dict[str, Any], str]:
        return self.run_cli("--adb", str(self.adb), "--serial", SERIAL, *argv)

    def start(self, *extra: str) -> dict[str, Any]:
        code, payload, _ = self.android("session", "start", "--app-id",
                                        "com.android.settings", *extra)
        self.assertEqual(code, 0, payload)
        self.forget_calls()
        return payload["session"]

    def inputs(self) -> list[list[str]]:
        return [argv[argv.index("shell") + 1:] for argv in self.calls("adb")
                if "shell" in argv and argv[argv.index("shell") + 1:][:1] == ["input"]]

    def dumps(self) -> int:
        return sum(1 for argv in self.calls("adb") if argv[-3:] == ["uiautomator", "dump",
                                                                       "/dev/tty"])

    def test_tap_in_an_agent_session_returns_the_new_screen(self) -> None:  # AL-11-S01
        session = self.start("--profile", "agent")
        code, payload, stdout = self.android("ui", "tap", "--text", "General")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.inputs(), [["input", "tap", "540", "390"]], "exactly one tap")
        screen = payload["screen"]
        self.assertIn('"About"', screen["outline"])
        self.assertNotIn("Network", screen["outline"])
        self.assertEqual((screen["count"], screen["truncated"]), (3, False))
        changes = payload["changes"]
        self.assertEqual(payload["changes_baseline"], "pre_read")
        self.assertEqual((changes["added"], changes["removed"]), (3, 4))
        self.assertEqual(changes["changed"], 0)
        added = [line for line in changes["lines"] if line.startswith("+ ")]
        self.assertEqual(len(added), 3)
        self.assertTrue(any('"About"' in line for line in added), changes["lines"])
        self.assertTrue(any(line.startswith("- ") and '"Display"' in line
                            for line in changes["lines"]))
        self.assertEqual(payload["foreground"], {"app": "com.android.settings",
                                                 "screen": ".MainActivity"})
        # the read before the tap and the observed read: no extra pre-read
        self.assertGreaterEqual(self.dumps(), 2)
        self.assertEqual(stdout.count("\n"), 1, "one line")
        # saved to the session history like `ui tree`
        [saved] = self.trees(session["session_id"])
        self.assertEqual(payload["screen"]["saved"], str(saved))
        tree = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual((tree["source"], tree["target"], tree["platform"], tree["count"]),
                         ("device", SERIAL, "android", 3))
        self.assertEqual((tree["purpose"], tree["after"]), ("observe", "ui tap"))
        self.assertEqual([node["text"] for node in tree["nodes"]],
                         ["General", "About", "Language"])
        latest = json.loads((saved.parent / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest, tree)

    def test_observe_sends_no_input_of_its_own(self) -> None:
        self.start("--profile", "agent")
        for argv in (("ui", "tap", "--x", "100", "--y", "200"),
                     ("ui", "swipe", "--from", "100,900", "--to", "100,300"),
                     ("ui", "type", "hello"),
                     ("ui", "key", "KEYCODE_BACK")):
            with self.subTest(argv=argv):
                self.forget_calls()
                code, payload, _ = self.android(*argv)
                self.assertEqual(code, 0, payload)
                self.assertIn("screen", payload)
                self.assertIn("foreground", payload)
                self.assertEqual(len(self.inputs()), 1, self.inputs())

    def test_observe_fails_the_action_does_not(self) -> None:  # AL-11-S02
        self.start()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["after_input"] = {"ui_dump_not_idle": 50}
        self.write_state(state)
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["observe_error"]["code"], errors.UI_NOT_IDLE)
        self.assertTrue(payload["observe_error"]["message"])
        self.assertNotIn("screen", payload)
        self.assertEqual(len(self.inputs()), 1, "one tap, never resent")

    def test_a_failing_read_reported_by_adb_is_an_observe_error(self) -> None:
        self.start()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["after_input"] = {"fail": {f"-s {SERIAL} exec-out uiautomator": [1, "boom"]}}
        self.write_state(state)
        code, payload, _ = self.android("ui", "tap", "--x", "100", "--y", "200", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["observe_error"]["code"], errors.BACKEND_FAILED)
        self.assertIn("boom", payload["observe_error"]["message"])
        self.assertEqual(len(self.inputs()), 1)

    def test_default_session_does_not_observe(self) -> None:  # AL-09-S02 for actions
        self.start()
        code, payload, _ = self.android("ui", "tap", "--x", "100", "--y", "200")
        self.assertEqual(code, 0, payload)
        for key in ("screen", "changes", "changes_baseline", "foreground", "observe_error"):
            self.assertNotIn(key, payload)
        self.assertEqual(self.dumps(), 0, "no read was added to a plain tap")

    def test_no_observe_wins_over_the_agent_profile(self) -> None:
        session = self.start("--profile", "agent")
        code, payload, _ = self.android("ui", "tap", "--x", "100", "--y", "200",
                                        "--no-observe")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("screen", payload)
        self.assertEqual(self.dumps(), 0)
        self.assertEqual(self.trees(session["session_id"]), [])

    def test_without_a_baseline_changes_is_null(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "tap", "--x", "100", "--y", "200", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertIsNone(payload["changes"])
        self.assertEqual(payload["changes_baseline"], "none")
        self.assertEqual(payload["changes_baseline_reason"], "no saved tree in this session")
        self.assertIn('"About"', payload["screen"]["outline"])

    def test_the_saved_tree_is_the_baseline_without_an_own_read(self) -> None:
        session = self.start()
        code, tree, _ = self.android("ui", "tree")
        self.assertEqual(code, 0, tree)
        self.forget_calls()
        code, payload, _ = self.android("ui", "swipe", "--from", "500,900", "--to", "500,300",
                                        "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["changes_baseline"], "saved_tree")
        self.assertEqual((payload["changes"]["added"], payload["changes"]["removed"]), (3, 4))
        # no read before the swipe: every dump came after it
        self.assertEqual(len(self.inputs()), 1)
        history = self.trees(session["session_id"])
        self.assertEqual(len(history), 2, "ui tree's, then observe's")

    def test_a_second_action_diffs_against_the_observed_tree(self) -> None:
        self.start("--profile", "agent")
        self.assertEqual(self.android("ui", "tap", "--x", "100", "--y", "200")[0], 0)
        code, payload, _ = self.android("ui", "key", "KEYCODE_VOLUME_UP")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["changes_baseline"], "saved_tree")
        self.assertEqual(payload["changes"], {"added": 0, "removed": 0, "changed": 0,
                                              "lines": []})

    def test_an_unsettled_screen_is_reported_not_failed(self) -> None:
        self.start("--profile", "agent")
        code, payload, _ = self.android("ui", "tap", "--x", "100", "--y", "200",
                                        "--observe-timeout-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertIn("screen", payload)
        [warning] = [item for item in payload["warnings"]
                     if item["code"] == "screen_not_settled"]
        self.assertIn("--observe-timeout-ms", warning["error"] + warning["hint"])
        # a zero budget is one read: the settle made it, no other one follows
        self.assertEqual(self.dumps(), 1)

    def test_a_negative_budget_is_refused_before_the_input(self) -> None:
        self.start("--profile", "agent")
        code, payload, _ = self.android("ui", "tap", "--x", "100", "--y", "200",
                                        "--observe-timeout-ms", "-1")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.calls("adb"), [], "refused before any device call")

    def test_the_new_flags_are_spelled_in_full_only(self) -> None:
        self.start("--profile", "agent")
        code, payload, _ = self.android("ui", "tap", "--x", "1", "--y", "1", "--obs")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.USAGE_ERROR)
        self.assertEqual(self.inputs(), [])

    def test_a_sensitive_value_never_reaches_the_observed_screen(self) -> None:
        session = self.start("--profile", "agent")
        empty = self.screen("empty.xml", android_dump("com.example.app", FIELD_EMPTY))
        typed = self.screen("typed.xml", android_dump("com.example.app", FIELD_TYPED))
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update({"ui_dump": empty, "after_input": {"ui_dump": typed}})
        self.write_state(state)
        code, payload, stdout = self.android("ui", "type", SECRET, "--sensitive")
        self.assertEqual(code, 0, payload)
        self.assertNotIn(SECRET, stdout)
        self.assertIn(f"<{len(SECRET)} chars>", payload["screen"]["outline"])
        self.assertEqual(payload["changes_baseline"], "pre_read")
        self.assertEqual(payload["changes"]["added"], 1)
        for path in (session["artifacts_dir"],):
            for saved in Path(path).rglob("*.json"):
                self.assertNotIn(SECRET, saved.read_text(encoding="utf-8"), saved)

    def typed_sensitive(self, before: list[tuple[str, str, str]],
                        after: list[tuple[str, str, str]],
                        value: str) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        """`ui type VALUE --sensitive` in an agent session whose screen goes
        from ``before`` to ``after``: (reply, stdout, saved tree nodes)."""
        session = self.start("--profile", "agent")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update({"ui_dump": self.screen("before.xml", android_dump("com.example.app", before)),
                      "after_input": {"ui_dump": self.screen(
                          "after.xml", android_dump("com.example.app", after))}})
        self.write_state(state)
        code, payload, stdout = self.android("ui", "type", value, "--sensitive")
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(self.inputs()), 1, "one input")
        self.assertEqual(payload["typed"], f"<{len(value)} chars>")
        [saved] = self.trees(session["session_id"])
        return payload, stdout, json.loads(saved.read_text(encoding="utf-8"))["nodes"]

    def assert_nowhere(self, shown: list[str]) -> None:
        """None of ``shown`` is in any file Autonom wrote (the tree history,
        latest.json, the journal, the session record...)."""
        files = [path for path in self.home.rglob("*") if path.is_file()]
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for value in shown:
                self.assertNotIn(value, text, path)

    def test_a_reformatted_sensitive_value_never_reaches_the_observed_screen(self) -> None:
        payload, stdout, nodes = self.typed_sensitive(CARD_EMPTY, CARD_TYPED, CARD)
        shown = [CARD, "4111 1111", "ending in 1111"]
        for value in shown:
            self.assertNotIn(value, stdout)
        self.assert_nowhere(shown)
        by_id = {node["resource_id"]: node for node in nodes}
        mask = f"<{len(CARD)} chars>"
        self.assertEqual(by_id["com.example.app:id/card"]["text"], mask)
        self.assertEqual(by_id["com.example.app:id/summary"]["text"], mask)
        # what does not show the value stays readable
        self.assertEqual(by_id["com.example.app:id/label"]["text"], "Card number")
        self.assertIn('"Pay"', payload["screen"]["outline"])
        self.assertTrue(all(CARD not in line and "1111" not in line
                            for line in payload["changes"]["lines"]), payload["changes"])

    def test_a_code_split_into_boxes_never_reaches_the_observed_screen(self) -> None:
        payload, stdout, nodes = self.typed_sensitive(CODE_EMPTY, CODE_TYPED, CODE)
        mask = f"<{len(CODE)} chars>"
        boxes = [node for node in nodes
                 if str(node.get("resource_id") or "").startswith("com.example.app:id/box")]
        self.assertEqual([node.get("text") for node in boxes], [mask] * 6)
        for digit in CODE:
            self.assertNotIn(f'"{digit}"', payload["screen"]["outline"])
            self.assertNotIn(f'"{digit}"', stdout)
        self.assertNotIn(CODE, stdout)
        self.assert_nowhere([CODE, '"text": "3"', '"text": "9"'])
        self.assertIn('"Verify"', payload["screen"]["outline"])

    def journal_entries(self, session: dict[str, Any], verb: str) -> list[dict[str, Any]]:
        journal = Path(session["artifacts_dir"]) / "journal.ndjson"
        if not journal.exists():
            return []
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        return [entry for entry in entries if entry.get("verb") == verb]

    def assert_interrupted_reply(self, reply: dict[str, Any], verb: str) -> None:
        """The reply of an action whose observe was interrupted: the action
        ran (ok), the error says the input was sent and how to go on."""
        self.assertTrue(reply["ok"], reply)
        error = reply["observe_error"]
        self.assertEqual(error["code"], "interrupted")
        self.assertIn(f"the input of {verb} was sent", error["message"])
        self.assertIn("autonom ui tree", error["hint"])
        self.assertIn("do not send it again", error["hint"])
        self.assertNotIn("error_code", reply)

    def test_an_interrupt_while_observing_keeps_the_action(self) -> None:
        """Ctrl-C (or a host's SIGINT) while observe settles: the tap was
        already sent, so the reply on stdout says the action ran and the
        journal records it ok; the command still exits 130, the status of
        an interrupt, so a shell chain stops. Never a second tap."""
        session = self.start()
        cli = self.load_cli()
        with mock.patch.object(ui, "settle", side_effect=KeyboardInterrupt):
            code, reply, stderr = self.run_in_process(
                cli, "ui", "tap", "--x", "100", "--y", "200", "--observe")
        self.assertEqual(code, 130, reply)
        self.assert_interrupted_reply(reply, "ui tap")
        self.assertIn("before the screen it led to was read", reply["observe_error"]["message"])
        for key in ("screen", "changes", "changes_baseline", "foreground"):
            self.assertNotIn(key, reply)
        self.assertEqual(stderr, "", "no exit-130 error envelope beside the reply")
        self.assertEqual(self.inputs(), [["input", "tap", "100", "200"]], "one tap")
        [tap] = self.journal_entries(session, "ui tap")
        self.assertTrue(tap["ok"], tap)
        self.assertEqual(self.trees(session["session_id"]), [], "nothing was read to save")

    def test_without_an_interrupt_the_same_command_exits_0(self) -> None:
        self.start()
        cli = self.load_cli()
        code, reply, _ = self.run_in_process(
            cli, "ui", "tap", "--x", "100", "--y", "200", "--observe")
        self.assertEqual(code, 0, reply)
        self.assertNotIn("observe_error", reply)
        # the flag of an interrupted run never outlives its invocation
        with mock.patch.object(ui, "settle", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_in_process(
                cli, "ui", "key", "KEYCODE_BACK", "--observe")[0], 130)
        code, reply, _ = self.run_in_process(cli, "ui", "key", "KEYCODE_BACK", "--observe")
        self.assertEqual(code, 0, reply)
        self.assertNotIn("observe_error", reply)

    def test_an_interrupt_in_the_foreground_lookup_keeps_the_screen(self) -> None:
        """Ctrl-C while `dumpsys` names the resumed activity: the screen was
        already read, compared and saved, so the reply keeps all of it."""
        session = self.start()
        cli = self.load_cli()
        with mock.patch.object(observe, "_resumed_activity", side_effect=KeyboardInterrupt):
            code, reply, stderr = self.run_in_process(
                cli, "ui", "tap", "--text", "General", "--observe")
        self.assertEqual(code, 130, reply)
        self.assert_interrupted_reply(reply, "ui tap")
        self.assertIn("only the foreground lookup was interrupted",
                      reply["observe_error"]["message"])
        self.assertIn('"About"', reply["screen"]["outline"])
        [saved] = self.trees(session["session_id"])
        self.assertEqual(reply["screen"]["saved"], str(saved))
        self.assertEqual(reply["changes_baseline"], "pre_read")
        self.assertEqual((reply["changes"]["added"], reply["changes"]["removed"]), (3, 4))
        self.assertEqual(reply["foreground"], {"app": "com.android.settings"})
        self.assertEqual(stderr, "")
        self.assertEqual(len(self.inputs()), 1)
        [tap] = self.journal_entries(session, "ui tap")
        self.assertTrue(tap["ok"], tap)

    # --- a real SIGINT, through a shell -----------------------------------------------------

    def arm_sigint(self, *words: str) -> None:
        """After the input, the first device call carrying ``words`` sends
        the CLI a real SIGINT."""
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["ui_dump"] = self.main
        state["after_input"] = {"ui_dump": self.general, "sigint_on": list(words)}
        self.write_state(state)

    def shell(self, script: str) -> subprocess.CompletedProcess[str]:
        """``script`` run by bash (sh without it), with SIGINT at its default
        so the CLI it starts turns one into KeyboardInterrupt even when this
        test runs where SIGINT is ignored (a background job)."""
        shell = shutil.which("bash", path=self.env["PATH"]) or "/bin/sh"
        cli = " ".join(shlex.quote(part) for part in (
            sys.executable, str(CLI), "--adb", str(self.adb), "--serial", SERIAL))
        completed = subprocess.run(
            [shell, "-c", script.replace("CLI", cli)], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def test_a_real_interrupt_while_observing_stops_a_shell_chain(self) -> None:
        """`autonom ui tap ... && next`: Ctrl-C while observe reads the new
        screen. The tap ran once and its reply is printed, but the chain stops
        (exit 130), in an `&&` chain and under `set -e` alike."""
        session = self.start()
        for script in ("CLI ui tap --x 100 --y 200 --observe && echo NEXT",
                       "set -e; CLI ui tap --x 100 --y 200 --observe; echo NEXT"):
            with self.subTest(script=script):
                self.forget_calls()
                self.arm_sigint("uiautomator", "dump")
                completed = self.shell(script)
                self.assertEqual(completed.returncode, 130, completed)
                self.assertNotIn("NEXT", completed.stdout)
                [line] = completed.stdout.splitlines()
                reply = json.loads(line)
                self.assert_interrupted_reply(reply, "ui tap")
                self.assertNotIn("screen", reply)
                self.assertEqual(completed.stderr.strip(), "", "the reply, not the envelope")
                self.assertEqual(self.inputs(), [["input", "tap", "100", "200"]], "one tap")
        taps = self.journal_entries(session, "ui tap")
        self.assertEqual(len(taps), 2)
        self.assertTrue(all(entry["ok"] for entry in taps), taps)

    def test_a_real_interrupt_in_the_foreground_lookup_keeps_screen_and_changes(self) -> None:
        session = self.start()
        self.arm_sigint("dumpsys", "activity", "activities")
        completed = self.shell("CLI ui tap --text General --observe && echo NEXT")
        self.assertEqual(completed.returncode, 130, completed)
        self.assertNotIn("NEXT", completed.stdout)
        reply = json.loads(completed.stdout)
        self.assert_interrupted_reply(reply, "ui tap")
        self.assertIn("only the foreground lookup", reply["observe_error"]["message"])
        self.assertIn('"About"', reply["screen"]["outline"])
        self.assertTrue(Path(reply["screen"]["saved"]).is_file())
        self.assertEqual(reply["changes_baseline"], "pre_read")
        self.assertGreater(reply["changes"]["added"], 0)
        self.assertEqual(reply["foreground"], {"app": "com.android.settings"})
        self.assertEqual(self.inputs(), [["input", "tap", "540", "390"]], "one tap")
        [tap] = self.journal_entries(session, "ui tap")
        self.assertTrue(tap["ok"], tap)

    def test_without_an_interrupt_the_chain_goes_on(self) -> None:
        self.start()
        completed = self.shell("CLI ui tap --x 100 --y 200 --observe && echo NEXT")
        self.assertEqual(completed.returncode, 0, completed)
        reply_line, next_line = completed.stdout.splitlines()
        self.assertEqual(next_line, "NEXT")
        reply = json.loads(reply_line)
        self.assertNotIn("observe_error", reply)
        self.assertIn('"About"', reply["screen"]["outline"])
        self.assertEqual(len(self.inputs()), 1)

    def test_an_interrupt_outside_observe_still_fails(self) -> None:
        """Only observe turns an interrupt into a reply: one before the input
        is still the exit-130 envelope, and nothing was sent."""
        session = self.start()
        cli = self.load_cli()
        with mock.patch.object(ui, "tap", side_effect=KeyboardInterrupt):
            code, reply, stderr = self.run_in_process(
                cli, "ui", "tap", "--x", "100", "--y", "200", "--observe")
        self.assertEqual(code, 130)
        self.assertEqual(reply, {}, "no reply on stdout")
        [line] = stderr.splitlines()
        envelope = json.loads(line)
        # an envelope like every other failure: the word it always had in
        # `error`, plus the additive code and a hint
        self.assertEqual(list(envelope), ["ok", "error_code", "error", "hint"])
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"], "interrupted")
        self.assertEqual(envelope["error_code"], errors.INTERRUPTED)
        self.assertEqual(errors.INTERRUPTED, observe.INTERRUPTED)
        self.assertIn("autonom ui tree", envelope["hint"])
        self.assertEqual(self.inputs(), [])
        [tap] = self.journal_entries(session, "ui tap")
        self.assertFalse(tap["ok"], tap)
        self.assertEqual(tap["error_code"], errors.INTERRUPTED)

    def load_cli(self) -> Any:
        """The CLI imported into this process, for a test that patches it."""
        spec = importlib.util.spec_from_file_location("autonom_cli_observe", CLI)
        cli = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cli)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        self.addCleanup(control_mod.end_invocation)
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        return cli

    def run_in_process(self, cli: Any, *argv: str) -> tuple[int, dict[str, Any], str]:
        """(exit code, stdout reply or {}, stderr) of one in-process run."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--adb", str(self.adb), "--serial", SERIAL, *argv])
        reply = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        return code, reply, err.getvalue()

    def test_a_read_of_an_earlier_command_in_the_process_is_no_baseline(self) -> None:
        """Two commands in one process (a host that imports the CLI): the
        second one's diff never starts from the first one's read."""
        self.start()
        spec = importlib.util.spec_from_file_location("autonom_cli_observe", CLI)
        cli = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cli)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        self.addCleanup(control_mod.end_invocation)
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

        def run(*argv: str) -> dict[str, Any]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--adb", str(self.adb), "--serial", SERIAL, *argv]),
                                 0)
            return json.loads(out.getvalue())

        first = run("ui", "tap", "--text", "General", "--observe")
        self.assertEqual(first["changes_baseline"], "pre_read")
        second = run("ui", "key", "KEYCODE_BACK", "--observe")
        self.assertEqual(second["changes_baseline"], "saved_tree", "not the tap's read")
        self.assertEqual(len(self.inputs()), 2)

    def test_observe_without_a_session_still_reports_the_screen(self) -> None:
        code, payload, _ = self.android("ui", "tap", "--x", "100", "--y", "200", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertIn('"About"', payload["screen"]["outline"])
        self.assertNotIn("saved", payload["screen"])
        self.assertEqual(payload["changes_baseline"], "none")
        self.assertEqual(payload["changes_baseline_reason"],
                         "no session of this target to keep a saved tree")
        self.assertEqual(len(self.inputs()), 1)

    # --- which saved tree is a baseline (DEC-006, read_shape) ----------------------------------

    def unchanged_screen(self) -> None:
        """The input changes nothing: the read after it is the read before."""
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.pop("after_input", None)
        self.write_state(state)

    def observed_coordinate_tap(self) -> dict[str, Any]:
        """`ui tap --x 500 --y 900 --observe` (no read of its own before the
        input on Android): its reply, after checking it sent one tap."""
        self.forget_calls()
        code, payload, _ = self.android("ui", "tap", "--x", "500", "--y", "900", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.inputs(), [["input", "tap", "500", "900"]], "exactly one tap")
        calls = self.calls("adb")
        tapped = next(index for index, argv in enumerate(calls)
                      if "shell" in argv and "input" in argv)
        self.assertFalse([argv for argv in calls[:tapped] if "uiautomator" in argv],
                         "no read before the tap")
        self.assertGreaterEqual(self.dumps(), 1)
        return payload

    def test_a_tree_read_another_way_is_never_compared(self) -> None:
        """`ui tree` with a filter or a limit lists other nodes of the same
        screen than observe reads; compared with it, an unchanged screen
        would show nodes as gone or new. So there is no baseline, and the
        reply says why."""
        self.start()
        self.unchanged_screen()
        cases = {
            ("--interactable",): "saved tree read with --interactable",
            ("--all",): "saved tree read with --all",
            ("--all", "--interactable"): "saved tree read with --all --interactable",
            ("--max-depth", "1"): "saved tree read with --max-depth 1",
            ("--max-nodes", "2"): "saved tree read with --max-nodes 2",
        }
        for flags, reason in cases.items():
            with self.subTest(flags=flags):
                code, tree, _ = self.android("ui", "tree", *flags)
                self.assertEqual(code, 0, tree)
                self.assertNotIn("read_shape", tree, "the ui tree reply is unchanged")
                saved = json.loads(Path(tree["saved"]).read_text(encoding="utf-8"))
                self.assertEqual(saved["read_shape"]["source"], "ui_tree")
                payload = self.observed_coordinate_tap()
                self.assertIsNone(payload["changes"])
                self.assertEqual(payload["changes_baseline"], "none")
                self.assertEqual(payload["changes_baseline_reason"], reason)

    def test_a_default_tree_of_an_unchanged_screen_shows_no_change(self) -> None:
        self.start()
        self.unchanged_screen()
        code, tree, _ = self.android("ui", "tree")
        self.assertEqual(code, 0, tree)
        saved = json.loads(Path(tree["saved"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["read_shape"], {"source": "ui_tree", "filter": "default",
                                               "max_depth": None, "max_nodes": 200,
                                               "truncated": False})
        payload = self.observed_coordinate_tap()
        self.assertEqual(payload["changes_baseline"], "saved_tree")
        self.assertEqual(payload["changes"], {"added": 0, "removed": 0, "changed": 0,
                                              "lines": []})
        self.assertNotIn("changes_baseline_reason", payload)
        # observe's own tree records its shape as well, so the next action
        # on the same screen compares with it
        observed = json.loads(Path(payload["screen"]["saved"]).read_text(encoding="utf-8"))
        self.assertEqual(observed["read_shape"], {"source": "observe", "filter": "default",
                                                  "max_depth": None, "max_nodes": 200,
                                                  "truncated": False})
        again = self.observed_coordinate_tap()
        self.assertEqual(again["changes_baseline"], "saved_tree")
        self.assertEqual(again["changes"]["lines"], [])

    def test_a_saved_tree_that_does_not_record_its_shape_is_never_compared(self) -> None:
        """A tree saved by an older version, or by a saver that does not say
        how it read (the pre-test a11y check), has no read_shape."""
        self.start()
        self.unchanged_screen()
        code, tree, _ = self.android("ui", "tree")
        self.assertEqual(code, 0, tree)
        latest = Path(tree["saved"]).parent / "latest.json"
        older = json.loads(latest.read_text(encoding="utf-8"))
        del older["read_shape"]
        latest.write_text(json.dumps(older), encoding="utf-8")
        payload = self.observed_coordinate_tap()
        self.assertIsNone(payload["changes"])
        self.assertEqual(payload["changes_baseline"], "none")
        self.assertIn("does not record how it was read", payload["changes_baseline_reason"])


# --- iOS ----------------------------------------------------------------------------------------


class IosObserveTests(ObserveCase):

    def setUp(self) -> None:
        super().setUp()
        self.main = self.screen("main.json", json.dumps(ios_tree(
            "Settings", ["General", "Accessibility", "Camera"])))
        self.general = self.screen("general.json", json.dumps(ios_tree(
            "General", ["About", "Software Update"])))
        self.write_state({"simctl_devices": BOOTED, "idb_describe_all": self.main,
                          "after_input": {"idb_describe_all": self.general}})

    def ios(self, *argv: str) -> tuple[int, dict[str, Any], str]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--idb", str(self.idb),
                            "--udid", UDID, *argv)

    def count(self, *verb: str) -> int:
        def strip(argv: list[str]) -> list[str]:
            rest = list(argv)
            while rest and rest[0] in ("--companion", "--companion-path", "--log",
                                       "--compression"):
                rest = rest[2:]
            return rest
        return [strip(argv)[:len(verb)] for argv in self.calls("idb")].count(list(verb))

    def test_tap_in_an_agent_session_returns_the_new_screen(self) -> None:  # AL-11-S01
        code, payload, _ = self.ios("session", "start", "--profile", "agent")
        self.assertEqual(code, 0, payload)
        self.forget_calls()
        code, payload, _ = self.ios("ui", "tap", "--desc", "General")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "tap"), 1, "exactly one tap")
        self.assertIn('"About"', payload["screen"]["outline"])
        self.assertEqual(payload["foreground"], {"app": "Settings", "screen": "General"})
        self.assertEqual(payload["changes_baseline"], "pre_read")
        self.assertTrue(any(line.startswith("+ ") and '"About"' in line
                            for line in payload["changes"]["lines"]))
        # the fake idb has no `ui quiet`: the settle polled, and every read
        # after the tap was observe's own (no read before it but the tap's)
        self.assertGreaterEqual(self.count("ui", "describe-all"), 2)

    def test_quiet_then_exactly_one_read_after_the_tap(self) -> None:  # AL-08 + AL-11
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["idb_quiet"] = "quiet"
        self.write_state(state)
        code, payload, _ = self.ios("session", "start", "--profile", "agent")
        self.assertEqual(code, 0, payload)
        self.forget_calls()
        code, payload, _ = self.ios("ui", "tap", "--desc", "General")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "tap"), 1)
        self.assertEqual(self.count("ui", "quiet"), 1)
        # the selector read before the tap, and one read after idb said quiet
        self.assertEqual(self.count("ui", "describe-all"), 2)
        self.assertNotIn("warnings", payload)
        self.assertEqual(payload["foreground"]["screen"], "General")

    def test_observe_error_on_ios_keeps_the_tap(self) -> None:  # AL-11-S02
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["after_input"] = {"idb_fail": {"ui describe-all": [1, "something broke"]}}
        self.write_state(state)
        code, payload, _ = self.ios("ui", "tap", "--desc", "General", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertIn("code", payload["observe_error"])
        self.assertIn("message", payload["observe_error"])
        self.assertEqual(self.count("ui", "tap"), 1)

    def before_input(self, verb: str) -> list[list[str]]:
        """The idb calls made before the first `ui <verb>` (the input), as
        their first two words."""
        calls = []
        for argv in self.calls("idb"):
            rest = [arg for arg in argv if not arg.startswith("-")][:2]
            if rest == ["ui", verb]:
                return calls
            calls.append(rest)
        self.fail(f"no ui {verb} was sent")

    def test_the_guard_lookup_is_the_coordinate_inputs_baseline(self) -> None:
        """No recorded geometry and no saved tree: the coordinate guard looks
        the screen size up with one `describe-all` before the input. That
        read is the baseline (pre_read); no other read is made before the
        input, and the input is sent once."""
        for argv, verb in ((("ui", "tap", "--x", "100", "--y", "220"), "tap"),
                           (("ui", "swipe", "--from", "200,700", "--to", "200,300"), "swipe")):
            with self.subTest(argv=argv):
                self.write_state({"simctl_devices": BOOTED, "idb_describe_all": self.main,
                                  "after_input": {"idb_describe_all": self.general}})
                self.forget_calls()
                code, payload, _ = self.ios(*argv, "--observe")
                self.assertEqual(code, 0, payload)
                self.assertEqual(self.count("ui", verb), 1, "exactly one input")
                self.assertEqual(self.before_input(verb), [["ui", "describe-all"]],
                                 "the guard's one lookup, and no other read before the input")
                self.assertEqual(payload["changes_baseline"], "pre_read")
                self.assertNotIn("changes_baseline_reason", payload)
                lines = payload["changes"]["lines"]
                self.assertTrue(any(line.startswith("+ ") and '"About"' in line
                                    for line in lines), lines)
                self.assertTrue(any(line.startswith("- ") and '"Camera"' in line
                                    for line in lines), lines)

    def test_the_guard_lookup_wins_over_an_older_saved_tree(self) -> None:
        """A session whose saved tree is older than the screen and whose
        record has no geometry: the tap's own lookup is the baseline, never
        the stale saved tree."""
        code, started, _ = self.ios("session", "start")
        self.assertEqual(code, 0, started)
        session = started["session"]
        code, tree, _ = self.ios("ui", "tree")  # saves the Settings screen
        self.assertEqual(code, 0, tree)
        privacy = self.screen("privacy.json", json.dumps(ios_tree(
            "Privacy", ["Location Services", "Tracking"])))
        self.write_state({"simctl_devices": BOOTED, "idb_describe_all": privacy,
                          "after_input": {"idb_describe_all": self.general}})
        # the record and its `current.json` copy, as an older version left them
        directory = Path(session["artifacts_dir"])
        for record_path in (directory / "session.json", directory.parent / "current.json"):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertIn("geometry", record["ui_observation"], record_path)
            del record["ui_observation"]["geometry"]
            record_path.write_text(json.dumps(record), encoding="utf-8")
        self.forget_calls()
        code, payload, _ = self.ios("ui", "tap", "--x", "100", "--y", "220", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "tap"), 1, "exactly one tap")
        self.assertEqual(self.before_input("tap"), [["ui", "describe-all"]])
        self.assertEqual(payload["changes_baseline"], "pre_read")
        lines = payload["changes"]["lines"]
        self.assertTrue(any(line.startswith("- ") and '"Tracking"' in line for line in lines),
                        lines)
        self.assertFalse(any('"Camera"' in line for line in lines), "not the stale saved tree")

    def test_recorded_geometry_means_no_read_and_the_saved_tree(self) -> None:
        """With the geometry recorded by `ui tree` the coordinate tap reads
        nothing before the input (AL-01), so the saved tree is the baseline."""
        code, started, _ = self.ios("session", "start")
        self.assertEqual(code, 0, started)
        code, tree, _ = self.ios("ui", "tree")
        self.assertEqual(code, 0, tree)
        self.forget_calls()
        code, payload, _ = self.ios("ui", "tap", "--x", "100", "--y", "220", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.count("ui", "tap"), 1)
        self.assertEqual(self.before_input("tap"), [], "no read before the tap")
        self.assertEqual(payload["changes_baseline"], "saved_tree")
        self.assertGreater(payload["changes"]["added"], 0)


# --- units ------------------------------------------------------------------------------------


def node(ref: str, role: str, label: str | None = None, *, bounds=(0, 0, 10, 10),
         **flags: Any) -> dict[str, Any]:
    return {"ref": ref, "role": role, "text": label, "desc": None, "resource_id": None,
            "bounds": list(bounds), "depth": 1, **flags}


class DiffTests(unittest.TestCase):

    def test_added_removed_and_changed(self) -> None:
        before = [node("n1", "text", "Title"), node("n2", "switch", "Wi-Fi", checkable=True,
                                                    checked=False, bounds=(0, 20, 10, 30)),
                  node("n3", "button", "Old", bounds=(0, 40, 10, 50))]
        after = [node("n1", "text", "Title"), node("n4", "switch", "Wi-Fi", checkable=True,
                                                   checked=True, bounds=(0, 20, 10, 30)),
                 node("n5", "button", "New", bounds=(0, 40, 10, 50))]
        result = observe.diff(before, after)
        self.assertEqual((result["added"], result["removed"], result["changed"]), (1, 1, 1))
        self.assertEqual([line[:2] for line in result["lines"]], ["+ ", "~ ", "- "])
        self.assertIn('"New"', result["lines"][0])
        self.assertIn("checked", result["lines"][1])
        self.assertNotIn("unchecked", result["lines"][1])
        self.assertIn('"Old"', result["lines"][2])

    def test_identity_is_role_id_text_desc_and_bounds(self) -> None:
        base = node("n1", "button", "Go")
        moved = node("n9", "button", "Go", bounds=(5, 5, 15, 15))
        self.assertEqual(observe.diff([base], [dict(base, ref="n7")])["lines"], [])
        result = observe.diff([base], [moved])
        self.assertEqual((result["added"], result["removed"]), (1, 1))

    def test_at_most_twenty_lines_with_full_counts(self) -> None:
        after = [node(f"n{i}", "text", f"Row {i}", bounds=(0, i * 10, 10, i * 10 + 5))
                 for i in range(30)]
        result = observe.diff([], after)
        self.assertEqual(result["added"], 30)
        self.assertEqual(len(result["lines"]), observe.CHANGE_LINES)
        self.assertTrue(all(line.startswith("+ ") for line in result["lines"]))

    def test_repeated_nodes_are_paired_in_order(self) -> None:
        row = node("n1", "text", "Same")
        result = observe.diff([row, dict(row)], [dict(row)])
        self.assertEqual((result["added"], result["removed"], result["changed"]), (0, 1, 0))


class MaskTests(unittest.TestCase):
    """What `ui type --sensitive` lets observe report: the value is kept out
    however the screen formats, splits or cuts it."""

    def test_the_value_or_a_part_of_it_however_shown(self) -> None:
        for value in ("4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111",
                      "Card 4111 1111 1111 1111 saved", "4111 1111 11",  # partly rendered
                      "41111111",  # cut at a maximum length
                      "**** **** **** 1111", "Visa ending in 1111", "1"):
            with self.subTest(value=value):
                self.assertTrue(observe.shows_secret(value, CARD))
        for value in ("HUNTER2 SECRET", "hunter2-Se", "Secret question"):
            with self.subTest(value=value):
                self.assertTrue(observe.shows_secret(value, SECRET))

    def test_what_does_not_show_it_stays(self) -> None:
        for value in ("Card number", "Pay", "Sign in", "Step 2 of 3", "41", "+", "•••••"):
            with self.subTest(value=value):
                self.assertFalse(observe.shows_secret(value, CARD))
        self.assertFalse(observe.shows_secret("Settings", SECRET))

    def test_one_character_texts_go_whatever_they_are(self) -> None:
        """A one-digit box and a key of a PIN pad look the same: every
        one-character text is hidden, so which ones were hidden tells nothing
        about the value."""
        pad = [observe.shows_secret(digit, CODE) for digit in "0123456789"]
        self.assertEqual(pad, [True] * 10)

    def test_a_short_or_symbol_only_value(self) -> None:
        self.assertTrue(observe.shows_secret("a-b-c", "abc"))
        self.assertFalse(observe.shows_secret("ab", "abc"))
        self.assertTrue(observe.shows_secret("pin: !@#$", "!@#$"))
        self.assertFalse(observe.shows_secret("Next", "!@#$"))

    def test_masked_hides_fields_and_what_shows_the_value(self) -> None:
        mask = f"<{len(CARD)} chars>"
        field = dict(node("n1", "textfield", "4111 1111 1111 1111"), desc="Card number",
                     editable=True)
        focused = dict(node("n2", "view", "Anything", bounds=(0, 20, 10, 30)), focused=True)
        summary = node("n3", "text", "Visa ending in 1111", bounds=(0, 40, 10, 50))
        label = node("n4", "text", "Pay now", bounds=(0, 60, 10, 70))
        blank = dict(node("n5", "textfield", None, bounds=(0, 80, 10, 90)), editable=True)
        nodes = [field, focused, summary, label, blank]
        out = observe.masked(nodes, CARD)
        self.assertEqual([(item.get("text"), item.get("desc")) for item in out],
                         [(mask, mask), (mask, None), (mask, None), ("Pay now", None),
                          (None, None)])
        # role, id, bounds and state stay, so the screen can still be acted on
        self.assertEqual({key: value for key, value in out[0].items() if key not in ("text", "desc")},
                         {key: value for key, value in field.items() if key not in ("text", "desc")})
        self.assertEqual(field["text"], "4111 1111 1111 1111", "the input list is not changed")
        self.assertIs(observe.masked(nodes, None), nodes)
        self.assertIs(observe.masked(nodes, ""), nodes)


class ForegroundTests(unittest.TestCase):
    ANDROID_TARGET = Target(platform=ANDROID, target_id=SERIAL, tool="adb")
    IOS_TARGET = Target(platform=IOS, target_id=UDID, tool="xcrun")

    def test_ios_app_label_and_first_heading(self) -> None:
        nodes = [dict(node("n0", "app", "Settings"), depth=0, desc="Settings", text=None),
                 dict(node("n1", "heading"), desc="General", text=None),
                 dict(node("n2", "heading"), desc="Later", text=None)]
        self.assertEqual(observe.foreground(self.IOS_TARGET, nodes),
                         {"app": "Settings", "screen": "General"})
        self.assertEqual(observe.foreground(self.IOS_TARGET, nodes[2:]), {"app": None,
                                                                          "screen": "Later"})

    def test_android_package_and_resumed_activity(self) -> None:
        nodes = [dict(node("n1", "text", "A"), package="com.example.app")]
        completed = subprocess.CompletedProcess(
            [], 0, stdout="  topResumedActivity=ActivityRecord{1d4ad5a u0 "
                          "com.example.app/.ui.MainActivity t18}\n")
        with mock.patch.object(observe.adb_mod, "run_adb", return_value=completed):
            self.assertEqual(observe.foreground(self.ANDROID_TARGET, nodes),
                             {"app": "com.example.app", "screen": ".ui.MainActivity"})

    def test_android_activity_of_another_package_is_not_the_screen(self) -> None:
        nodes = [dict(node("n1", "text", "A"), package="com.example.app")]
        completed = subprocess.CompletedProcess(
            [], 0, stdout="topResumedActivity=ActivityRecord{1 u0 com.other/.Main t1}\n")
        with mock.patch.object(observe.adb_mod, "run_adb", return_value=completed):
            self.assertEqual(observe.foreground(self.ANDROID_TARGET, nodes),
                             {"app": "com.example.app"})

    def test_a_failing_dumpsys_leaves_the_app(self) -> None:
        nodes = [dict(node("n1", "text", "A"), package="com.example.app")]
        with mock.patch.object(observe.adb_mod, "run_adb",
                               side_effect=subprocess.TimeoutExpired(["adb"], 10)):
            self.assertEqual(observe.foreground(self.ANDROID_TARGET, nodes),
                             {"app": "com.example.app"})


def forget_reads() -> None:
    """Drop what this thread holds of earlier reads and inputs."""
    for holder in (ui._LAST_SNAPSHOT, ui._LAST_PAYLOAD, ui._AT_INPUT):  # noqa: SLF001
        if hasattr(holder, "value"):
            del holder.value


class OwnReadTests(unittest.TestCase):
    """The baseline of a command's diff is the read it held when its input
    went out, never one made after the input or by an earlier command."""
    TARGET = Target(platform=ANDROID, target_id=SERIAL, tool="adb")

    def setUp(self) -> None:
        forget_reads()
        self.addCleanup(forget_reads)
        self.dump = android_dump("com.android.settings", SETTINGS_MAIN)
        self.other_dump = android_dump("com.android.settings", SETTINGS_GENERAL)
        guard = mock.patch.object(ui, "_pretest_guard")  # no pre-test in a unit test
        guard.start()
        self.addCleanup(guard.stop)
        sent = mock.patch.object(ui.ui_android, "tap")
        self.sent = sent.start()
        self.addCleanup(sent.stop)

    def snapshot(self, dump: str | None = None, target: Target | None = None) -> None:
        with mock.patch.object(ui.ui_android, "dump_hierarchy", return_value=dump or self.dump):
            ui.snapshot(target or self.TARGET)

    def tap(self, target: Target | None = None) -> None:
        ui.tap(target or self.TARGET, 10, 10, screen=(1080, 1920))

    def test_the_read_held_when_the_input_went_out(self) -> None:
        mark = observe.read_mark(self.TARGET)
        self.snapshot()
        self.tap()
        self.assertEqual(observe.own_read(self.TARGET, since=mark), self.dump)
        self.assertEqual(self.sent.call_count, 1)

    def test_a_read_made_after_the_input_is_not_the_baseline(self) -> None:
        mark = observe.read_mark(self.TARGET)
        self.snapshot()
        self.tap()
        self.snapshot(self.other_dump)  # after the input: the new screen
        self.assertEqual(observe.own_read(self.TARGET, since=mark), self.dump)

    def test_without_an_input_since_the_mark_there_is_none(self) -> None:
        self.snapshot()
        self.tap()  # an earlier command's input and read
        mark = observe.read_mark(self.TARGET)
        self.snapshot(self.other_dump)
        self.assertIsNone(observe.own_read(self.TARGET, since=mark), "no input of its own")

    def test_an_earlier_commands_read_is_not_this_ones(self) -> None:
        self.snapshot()
        mark = observe.read_mark(self.TARGET)
        self.tap()  # this command read nothing itself
        self.assertIsNone(observe.own_read(self.TARGET, since=mark))
        self.snapshot(self.other_dump)
        self.tap()
        self.assertEqual(observe.own_read(self.TARGET, since=mark), self.other_dump)

    def test_no_read_before_the_input_is_none(self) -> None:
        mark = observe.read_mark(self.TARGET)
        self.tap()
        self.assertIsNone(observe.own_read(self.TARGET, since=mark))
        self.assertIsNone(observe.own_read(self.TARGET))

    def test_a_snapshot_held_without_its_read_is_no_baseline(self) -> None:
        mark = observe.read_mark(self.TARGET)
        self.snapshot()
        ui._LAST_SNAPSHOT.value = (ANDROID, SERIAL, [])  # noqa: SLF001
        self.tap()
        self.assertIsNone(observe.own_read(self.TARGET, since=mark))

    def test_another_targets_input_is_not_this_ones(self) -> None:
        other = Target(platform=ANDROID, target_id="emulator-5999", tool="adb")
        mark = observe.read_mark(other)
        self.snapshot()
        self.tap()
        self.assertIsNone(observe.own_read(other, since=mark))
        # the latest input went to `other`: this target's read is no longer
        # the read of the latest input either
        mark = observe.read_mark(self.TARGET)
        self.snapshot(target=other)
        self.tap(other)
        self.assertIsNone(observe.own_read(self.TARGET, since=mark))


class IosGuardLookupTests(EnvSandboxMixin, unittest.TestCase):
    """On the iOS idb route the coordinate guard's size lookup is a whole
    `describe-all`: it is kept as the thread's read, so it is the baseline
    of an observed coordinate tap and no other read is made for it."""
    TARGET = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    def setUp(self) -> None:
        from autonom_lib import ui_ios

        self.ui_ios = ui_ios
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.set_env(AUTONOM_HOME=tmp.name, AUTONOM_IOS_HID="idb", AUTONOM_AXE=None,
                     AUTONOM_UI_BACKEND=None, AUTONOM_IDB=str(FAKE_IDB),
                     AUTONOM_IDB_COMPANION=None, AUTONOM_FAKE_STATE=None,
                     AUTONOM_FAKE_LOG=None)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        ui_ios.set_preference(None)
        self.addCleanup(ui_ios.set_preference, None)
        ui_ios.reset_hid_probe()
        self.addCleanup(ui_ios.reset_hid_probe)
        forget_reads()
        self.addCleanup(forget_reads)
        self.payload = json.dumps(ios_tree("Settings", ["General", "Camera"]))

    def test_the_lookup_is_kept_and_is_the_baseline(self) -> None:
        mark = observe.read_mark(self.TARGET)
        with mock.patch.object(self.ui_ios.ios_idb, "describe_all",
                               return_value=self.payload) as describe, \
                mock.patch.object(self.ui_ios.ios_idb, "tap") as tap:
            ui.tap(self.TARGET, 100, 220)
            ui.tap(self.TARGET, 100, 220)  # the kept read gives the size: no lookup
        self.assertEqual(describe.call_count, 1)
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(observe.own_read(self.TARGET, since=mark), self.payload)
        self.assertEqual(self.ui_ios.recorded_screen_size(self.TARGET), (402, 874))

    def test_a_failed_lookup_measures_without_a_second_read(self) -> None:
        mark = observe.read_mark(self.TARGET)
        failure = errors.AutonomError(errors.BACKEND_FAILED, "no companion")
        with mock.patch.object(self.ui_ios.ios_idb, "describe_all",
                               side_effect=failure) as describe, \
                mock.patch("autonom_lib.ios_geometry.measure",
                           return_value={"width": 402, "height": 874, "units": "points"}) \
                as measure, \
                mock.patch.object(self.ui_ios.ios_idb, "tap") as tap:
            ui.tap(self.TARGET, 100, 220)
        self.assertEqual((describe.call_count, measure.call_count, tap.call_count), (1, 1, 1))
        self.assertIsNone(observe.own_read(self.TARGET, since=mark), "nothing was read")
        self.assertEqual(self.ui_ios.recorded_screen_size(self.TARGET), (402, 874))


class ShapeTests(unittest.TestCase):
    """Only a saved tree read the way observe reads is compared with it."""

    def test_observe_reads_the_default_shape(self) -> None:
        self.assertIsNone(observe.shape_mismatch(observe.read_shape(observe.SHAPE_OBSERVE)))
        self.assertIsNone(observe.shape_mismatch(observe.read_shape(observe.SHAPE_UI_TREE)))
        self.assertEqual(observe.read_shape(observe.SHAPE_UI_TREE),
                         {"source": "ui_tree", "filter": "default", "max_depth": None,
                          "max_nodes": observe.MAX_NODES, "truncated": False})

    def test_every_other_shape_is_refused_with_its_reason(self) -> None:
        cases = {
            "saved tree read with --interactable": dict(interactable=True),
            "saved tree read with --all": dict(all_nodes=True),
            "saved tree read with --all --interactable": dict(all_nodes=True,
                                                              interactable=True),
            "saved tree read with --max-depth 3": dict(max_depth=3),
            "saved tree read with --max-nodes 50": dict(max_nodes=50),
            "saved tree read with --max-nodes 500": dict(max_nodes=500),
            f"saved tree cut at {observe.MAX_NODES} nodes": dict(truncated=True),
        }
        for reason, options in cases.items():
            with self.subTest(options=options):
                shape = observe.read_shape(observe.SHAPE_UI_TREE, **options)
                self.assertEqual(observe.shape_mismatch(shape), reason)

    def test_a_tree_that_does_not_record_its_shape_is_refused(self) -> None:
        for shape in (None, {}, "default", {"filter": "default"},
                      {"source": "pretest", "filter": "default", "max_nodes": 200}):
            with self.subTest(shape=shape):
                self.assertIn("does not record how it was read",
                              observe.shape_mismatch(shape))


class AfterActionTests(EnvSandboxMixin, unittest.TestCase):
    TARGET = Target(platform=ANDROID, target_id=SERIAL, tool="adb")

    def setUp(self) -> None:
        self.sandbox_home()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dump = android_dump("com.android.settings", SETTINGS_GENERAL)
        self.record = {"session_id": "s_unit", "platform": ANDROID, "target_id": SERIAL,
                       "artifacts_dir": tmp.name}

    def test_an_unexpected_exception_is_an_observe_error(self) -> None:
        with mock.patch.object(ui, "read_payload", side_effect=RuntimeError("kaput")):
            keys, warnings = observe.after_action(self.TARGET, self.record, verb="ui tap")
        self.assertEqual(keys, {"observe_error": {"code": errors.BACKEND_FAILED,
                                                  "message": "kaput"}})
        self.assertEqual(warnings, [])

    def test_a_timed_out_read_is_an_observe_error(self) -> None:
        with mock.patch.object(ui, "read_payload",
                               side_effect=subprocess.TimeoutExpired(["adb"], 20)):
            keys, _ = observe.after_action(self.TARGET, self.record, verb="ui tap")
        self.assertEqual(keys["observe_error"]["code"], errors.BACKEND_FAILED)

    def test_a_tree_that_cannot_be_saved_is_a_warning(self) -> None:
        with mock.patch.object(ui, "read_payload", return_value=self.dump), \
                mock.patch.object(observe, "save_tree", side_effect=OSError("disk full")), \
                mock.patch.object(observe, "_resumed_activity", return_value=None):
            keys, warnings = observe.after_action(self.TARGET, self.record, verb="ui tap",
                                                  timeout_ms=0)
        self.assertIn('"About"', keys["screen"]["outline"])
        self.assertNotIn("saved", keys["screen"])
        self.assertIn("evidence_write_failed", [item["code"] for item in warnings])

    def test_one_read_when_the_settle_returns_none(self) -> None:
        reads = []

        def read(target):
            reads.append(target)
            return self.dump

        with mock.patch.object(ui, "read_payload", side_effect=read), \
                mock.patch.object(ui, "settle", return_value={"settled": True}), \
                mock.patch.object(observe, "_resumed_activity", return_value=None):
            keys, _ = observe.after_action(self.TARGET, self.record, verb="ui key")
        self.assertEqual(len(reads), 1)
        self.assertEqual(keys["screen"]["count"], 3)

    def test_the_settle_asks_for_the_quiet_signal_with_the_budget(self) -> None:
        with mock.patch.object(ui, "read_payload", return_value=self.dump), \
                mock.patch.object(ui, "settle", wraps=ui.settle) as settle, \
                mock.patch.object(observe, "_resumed_activity", return_value=None):
            observe.after_action(self.TARGET, self.record, verb="ui tap", timeout_ms=1234)
        kwargs = settle.call_args.kwargs
        self.assertEqual((kwargs["timeout_ms"], kwargs["quiet_ms"], kwargs["quiet_signal"]),
                         (1234, observe.QUIET_MS, True))

    def test_an_interrupt_is_an_observe_error_not_a_raise(self) -> None:
        with mock.patch.object(ui, "settle", side_effect=KeyboardInterrupt):
            keys, warnings = observe.after_action(self.TARGET, self.record, verb="ui swipe")
        self.assertEqual(set(keys), {"observe_error"})
        self.assertEqual(keys["observe_error"]["code"], observe.INTERRUPTED)
        self.assertIn("the input of ui swipe was sent", keys["observe_error"]["message"])
        self.assertIn("autonom ui tree", keys["observe_error"]["hint"])
        self.assertTrue(observe.interrupted(keys))
        self.assertEqual(warnings, [])

    def interrupted_at(self, patch: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """after_action with ``patch`` raising KeyboardInterrupt, over a read
        that settles at once."""
        with mock.patch.object(ui, "read_payload", return_value=self.dump), \
                mock.patch.object(ui, "settle", return_value={"settled": True}), \
                patch:
            return observe.after_action(self.TARGET, self.record, verb="ui tap",
                                        baseline_payload=self.dump)

    def test_an_interrupt_keeps_what_observe_already_obtained(self) -> None:
        cases = {
            # comparing: the screen only
            "compare": (mock.patch.object(observe, "_compact", side_effect=[
                observe._compact(ANDROID, self.dump), KeyboardInterrupt]),  # noqa: SLF001
                        {"screen", "observe_error"}),
            # saving: the screen and the changes, not where it was saved
            "save": (mock.patch.object(observe, "save_tree", side_effect=KeyboardInterrupt),
                     {"screen", "changes", "changes_baseline", "observe_error"}),
            # the foreground lookup: everything but the activity
            "foreground": (mock.patch.object(observe, "_resumed_activity",
                                             side_effect=KeyboardInterrupt),
                           {"screen", "changes", "changes_baseline", "foreground",
                            "observe_error"}),
        }
        for stage, (patch, expected) in cases.items():
            with self.subTest(stage=stage):
                keys, warnings = self.interrupted_at(patch)
                self.assertEqual(set(keys), expected)
                self.assertTrue(observe.interrupted(keys))
                self.assertIn("the input of ui tap was sent", keys["observe_error"]["message"])
                self.assertEqual(keys["screen"]["count"], 3)
                self.assertEqual(warnings, [])
                if stage == "save":
                    self.assertNotIn("saved", keys["screen"])
                    self.assertIn("while saving", keys["observe_error"]["message"])
                if stage == "foreground":
                    self.assertTrue(Path(keys["screen"]["saved"]).is_file())
                    self.assertEqual(keys["foreground"], {"app": "com.android.settings"})
                    self.assertEqual(keys["changes"]["added"], 0)
                    self.assertIn("only the foreground lookup", keys["observe_error"]["message"])

    def test_an_unsettled_read_cut_short_keeps_its_warning(self) -> None:
        with mock.patch.object(ui, "read_payload", return_value=self.dump), \
                mock.patch.object(ui, "settle", return_value={"settled": False}), \
                mock.patch.object(observe, "_resumed_activity", side_effect=KeyboardInterrupt):
            keys, warnings = observe.after_action(self.TARGET, self.record, verb="ui tap")
        self.assertIn("screen", keys)
        self.assertEqual([item["code"] for item in warnings], ["screen_not_settled"])

    def test_only_an_interrupt_is_interrupted(self) -> None:
        """Any other failure is no cancel, whatever code it carries."""
        fake = errors.AutonomError("interrupted", "not a Ctrl-C", "")
        with mock.patch.object(ui, "read_payload", side_effect=fake):
            keys, _ = observe.after_action(self.TARGET, self.record, verb="ui tap")
        self.assertEqual(keys["observe_error"]["code"], errors.BACKEND_FAILED)
        self.assertFalse(observe.interrupted(keys))
        self.assertFalse(observe.interrupted({}))
        self.assertFalse(observe.interrupted({"observe_error": {"code": errors.UI_NOT_IDLE}}))

    def test_an_error_message_never_carries_the_typed_value(self) -> None:
        with mock.patch.object(ui, "read_payload",
                               side_effect=RuntimeError(f"cannot parse {SECRET}")):
            keys, _ = observe.after_action(self.TARGET, self.record, verb="ui type",
                                           secret=SECRET)
        self.assertNotIn(SECRET, json.dumps(keys))
        self.assertIn(f"<{len(SECRET)} chars>", keys["observe_error"]["message"])

    def test_check_timeout(self) -> None:
        self.assertEqual(observe.check_timeout(None), observe.DEFAULT_TIMEOUT_MS)
        self.assertEqual(observe.check_timeout(0), 0)
        with self.assertRaises(errors.AutonomError) as caught:
            observe.check_timeout(-5)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)

    def test_wanted(self) -> None:
        agent = {"profile": "agent"}
        self.assertTrue(observe.wanted(None, agent))
        self.assertFalse(observe.wanted(None, {"profile": "default"}))
        self.assertFalse(observe.wanted(None, {}))
        self.assertFalse(observe.wanted(None, None))
        self.assertFalse(observe.wanted(False, agent))
        self.assertTrue(observe.wanted(True, None))


if __name__ == "__main__":
    unittest.main()
