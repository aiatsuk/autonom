"""`autonom ui do` (AL-12): Flow v1 steps from stdin, run in one call.

The oracle is what the fakes executed. The CLI runs in a subprocess against
`fake_adb.py` (or `fake_idb.py`) behind a small wrapper that switches the
screen the fake reports once the first input has been sent, so a step after
a tap sees the next screen. The fake call log proves how many inputs were
sent (none after a failing step, none at all for invalid input); the reply
proves the step results, `failure` and the final screen.

The loader (`flow/inline.py`) is also tested in process: JSON and YAML give
the same steps, and every malformed input is `invalid_value` naming the
step and the problem. No real device.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
SERIAL = "emulator-5614"
UDID = "AAAAAAAA-1111-2222-3333-DDDDDDDDDDDD"
APP = "com.android.settings"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
SECRET = "hunter2-Secret"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib.flow import inline  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


# --- screens --------------------------------------------------------------------------


def android_dump(rows: list[tuple[str, str, str]]) -> str:
    """A UI Automator dump of Settings: one row per (class, text, resource
    id), stacked 120 px apart under a root FrameLayout."""
    nodes = []
    for index, (klass, text, rid) in enumerate(rows):
        top = 100 + index * 120
        clickable = "true" if klass.endswith("Button") else "false"
        nodes.append(
            f'    <node index="{index}" text="{text}" resource-id="{rid}" class="{klass}" '
            f'package="{APP}" content-desc="" checkable="false" checked="false" '
            f'clickable="{clickable}" enabled="true" focusable="true" focused="false" '
            f'scrollable="false" long-clickable="false" password="false" selected="false" '
            f'bounds="[40,{top}][1040,{top + 100}]" />')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
            '<hierarchy rotation="0">\n'
            f'  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
            f'package="{APP}" content-desc="" checkable="false" checked="false" '
            'clickable="false" enabled="true" focusable="false" focused="false" '
            'scrollable="false" long-clickable="false" password="false" selected="false" '
            'bounds="[0,0][1080,1920]">\n'
            + "\n".join(nodes) + "\n  </node>\n</hierarchy>\n")


SETTINGS_MAIN = [("android.widget.TextView", "Settings", "android:id/title"),
                 ("android.widget.Button", "Network &amp; internet", ""),
                 ("android.widget.Button", "General", ""),
                 ("android.widget.Button", "Display", "")]
SETTINGS_GENERAL = [("android.widget.TextView", "General", "android:id/title"),
                    ("android.widget.Button", "About", ""),
                    ("android.widget.Button", "Language", "")]
FIELD_EMPTY = [("android.widget.EditText", "", "com.android.settings:id/password")]
FIELD_TYPED = [("android.widget.EditText", SECRET, "com.android.settings:id/password"),
               ("android.widget.TextView", f"Signed in with {SECRET}", "")]
CODE = "493817"
CODE_SHOWN = [("android.widget.TextView", "Settings", "android:id/title"),
              ("android.widget.TextView", CODE, "com.android.settings:id/code")]


def ios_tree(heading: str, buttons: list[str]) -> list[dict[str, Any]]:
    """An idb describe-all payload: the Settings application root, a
    heading and one button per label."""
    def element(kind: str, label: str, y: int, height: int, width: int = 370,
                x: int = 16) -> dict[str, Any]:
        return {"type": kind, "role": f"AX{kind}", "AXLabel": label, "AXValue": None,
                "AXUniqueId": None, "enabled": True,
                "frame": {"x": x, "y": y, "width": width, "height": height}}
    payload = [element("Application", "Settings", 0, 874, width=402, x=0),
               element("Heading", heading, 120, 40)]
    for index, label in enumerate(buttons):
        payload.append(element("Button", label, 200 + index * 60, 52))
    return payload


# Runs the fake, then (once the first input has been sent) applies the state
# keys under `after_input`, so the next read shows the next screen.
WRAPPER = '''#!{python}
import json, os, runpy, sys

FAKE = {fake!r}
TOOL = {tool!r}


def is_input(argv):
    if TOOL == "adb":
        return "shell" in argv and argv[argv.index("shell") + 1:argv.index("shell") + 2] == ["input"]
    rest = [arg for arg in argv if not arg.startswith("-")]
    return rest[:1] == ["ui"] and rest[1:2] in (["tap"], ["swipe"], ["text"], ["key"], ["button"])


argv = sys.argv[1:]
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

TWO_STEPS = '[{"tapOn":{"text":"General"}},{"assertVisible":{"text":"About"}}]'


class UiDoCase(EnvSandboxMixin, unittest.TestCase):
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

    def run_cli(self, *argv: str, steps: str | None = None) -> tuple[int, dict[str, Any], str]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root, env=self.env, text=True,
            input=steps if steps is not None else "", capture_output=True, check=False,
            timeout=120)
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


# --- Android ----------------------------------------------------------------------------


class AndroidUiDoTests(UiDoCase):

    def setUp(self) -> None:
        super().setUp()
        self.main = self.screen("main.xml", android_dump(SETTINGS_MAIN))
        self.general = self.screen("general.xml", android_dump(SETTINGS_GENERAL))
        self.write_state({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": self.main, "foreground_app": APP,
            "after_input": {"ui_dump": self.general}})

    def android(self, *argv: str, steps: str | None = None) -> tuple[int, dict[str, Any], str]:
        return self.run_cli("--adb", str(self.adb), "--serial", SERIAL, *argv, steps=steps)

    def start(self, *extra: str, app: bool = True) -> dict[str, Any]:
        code, payload, _ = self.android("session", "start",
                                        *(("--app-id", APP) if app else ()), *extra)
        self.assertEqual(code, 0, payload)
        self.forget_calls()
        return payload["session"]

    def do(self, steps: str, *extra: str) -> tuple[int, dict[str, Any], str]:
        return self.android("ui", "do", *extra, steps=steps)

    def inputs(self) -> list[list[str]]:
        return [argv[argv.index("shell") + 1:] for argv in self.calls("adb")
                if "shell" in argv and argv[argv.index("shell") + 1:][:1] == ["input"]]

    def journal(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        path = self.home / "sessions" / session["session_id"] / journal_mod.JOURNAL_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_two_json_steps_pass_and_return_the_final_screen(self) -> None:  # AL-12-S01
        session = self.start()
        code, payload, stdout = self.do(TWO_STEPS)
        self.assertEqual(code, 0, payload)
        self.assertEqual(stdout.count("\n"), 1, "one line")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["run_id"].startswith("fr_"), payload["run_id"])
        self.assertIsNone(payload["failure"])
        self.assertEqual(
            [(step["index"], step["step"], step["command"], step["status"])
             for step in payload["steps"]],
            [(1, 1, "tapOn", "passed"), (2, 2, "assertVisible", "passed")])
        for step in payload["steps"]:
            self.assertIsInstance(step["duration_ms"], int)
            self.assertNotIn("error_code", step)
        self.assertEqual(self.inputs(), [["input", "tap", "540", "390"]], "exactly one tap")
        # the final screen, as observe gives it after an action
        self.assertIn('"About"', payload["screen"]["outline"])
        self.assertNotIn('"Display"', payload["screen"]["outline"])
        self.assertEqual((payload["screen"]["count"], payload["screen"]["truncated"]),
                         (3, False))
        self.assertEqual(payload["foreground"]["app"], APP)
        self.assertEqual((payload["platform"], payload["target_id"]), ("android", SERIAL))
        self.assertTrue(Path(payload["events"]).is_file())
        # the final read is saved to the session's tree history like `ui tree`
        [saved] = self.trees(session["session_id"])
        self.assertEqual(payload["screen"]["saved"], str(saved))
        tree = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual((tree["purpose"], tree["after"]), ("observe", "ui do"))
        verbs = [entry.get("verb") for entry in self.journal(session)]
        self.assertIn("ui do", verbs)

    def test_a_yaml_step_list_runs_the_same_steps(self) -> None:
        self.start()
        steps = ("# open General, then check About\n"
                 "- tapOn: General\n"
                 "- assertVisible:\n"
                 "    text: About\n")
        code, payload, _ = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual([(step["step"], step["command"]) for step in payload["steps"]],
                         [(1, "tapOn"), (2, "assertVisible")])
        self.assertEqual(len(self.inputs()), 1)
        self.assertIn('"About"', payload["screen"]["outline"])

    def test_it_stops_at_the_first_failing_step(self) -> None:  # AL-12-S02
        self.start()
        steps = ('[{"assertVisible":{"text":"Missing","timeoutMs":600}},'
                 '{"tapOn":{"text":"General"}}]')
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 1, payload)
        self.assertEqual(stdout.count("\n"), 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        failure = payload["failure"]
        self.assertEqual((failure["step"], failure["index"], failure["command"]),
                         (1, 1, "assertVisible"))
        self.assertEqual(failure["code"], errors.FLOW_ASSERTION_TIMEOUT)
        self.assertEqual(failure["failure_class"], "test_failure")
        self.assertTrue(failure["message"])
        [step] = payload["steps"]
        self.assertEqual((step["status"], step["error_code"]),
                         ("failed", errors.FLOW_ASSERTION_TIMEOUT))
        self.assertEqual(self.inputs(), [], "no step after the failing one ran")
        # the final screen is still reported: nothing changed it
        self.assertIn('"General"', payload["screen"]["outline"])

    def test_a_yaml_failure_names_its_step_not_its_line(self) -> None:
        self.start()
        steps = ("- assertVisible:\n"
                 "    text: Settings\n"
                 "- assertVisible:\n"
                 "    text: Missing\n"
                 "    timeoutMs: 600\n"
                 "- tapOn: General\n")
        code, payload, _ = self.do(steps)
        self.assertEqual(code, 1, payload)
        self.assertEqual((payload["failure"]["step"], payload["failure"]["index"]), (2, 2))
        self.assertEqual([step["status"] for step in payload["steps"]], ["passed", "failed"])
        self.assertEqual(self.inputs(), [])

    def test_steps_inside_a_group_name_the_group_step(self) -> None:
        self.start()
        steps = ("- group:\n"
                 "    label: open General\n"
                 "    commands:\n"
                 "      - tapOn: General\n"
                 "      - assertVisible: About\n"
                 "- assertVisible: Language\n")
        code, payload, _ = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(
            [(step["index"], step["step"], step["command"], step.get("depth"))
             for step in payload["steps"]],
            [(2, 1, "tapOn", 1), (3, 1, "assertVisible", 1), (4, 2, "assertVisible", None)])
        self.assertEqual(len(self.inputs()), 1)

    def test_invalid_input_is_refused_before_any_device_call(self) -> None:
        self.start()
        cases = [
            ('[{"tapOn":"General"},{"tapOnn":"About"}]', 2, errors.FLOW_UNKNOWN_COMMAND),
            ("- tapOn: General\n- assertVisible:\n\ttext: About\n", 2,
             errors.FLOW_PARSE_ERROR),
            ('[{"tapOn":"General"},{"assertVisible":{"text":"About","timeoutMs":"500"}}]',
             2, errors.FLOW_COMMAND_INVALID),
            ('[{"tapOn":"General"},{"tapOn":"About","back":{}}]', 2, None),
            ('[{"tapOn": }]', None, None),
            ("", None, None),
        ]
        for steps, step, flow_code in cases:
            with self.subTest(steps=steps):
                self.forget_calls()
                code, payload, stdout = self.do(steps)
                self.assertEqual(code, 2, payload)
                self.assertEqual(stdout, "")
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
                self.assertTrue(payload["error"].startswith("ui do"), payload["error"])
                self.assertTrue(payload["hint"])
                self.assertEqual(payload.get("step"), step)
                self.assertEqual(payload.get("flow_error_code"), flow_code)
                self.assertEqual(self.calls("adb"), [], "refused before any device call")

    def test_a_check_against_the_target_is_refused_before_any_step(self) -> None:
        session = self.start()
        runs = self.home / "sessions" / session["session_id"] / "flows"
        code, payload, _ = self.do('[{"tapOn":"General"},{"inputText":"${PASSWORD}"}]')
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual((payload["step"], payload["command"]), (2, "inputText"))
        self.assertEqual(payload["flow_error_code"], errors.FLOW_VAR_UNDEFINED)
        self.assertIn("${PASSWORD}", payload["error"])
        self.assertIn("--env", payload["hint"])
        self.assertEqual(self.inputs(), [])
        self.assertFalse(runs.exists() and any(runs.rglob("events.ndjson")),
                         "no run was started")

    def test_steps_use_the_session_app_id(self) -> None:
        self.start()
        code, payload, _ = self.do('["stopApp"]')
        self.assertEqual(code, 0, payload)
        stops = [argv for argv in self.calls("adb") if "force-stop" in argv]
        self.assertEqual(stops, [["-s", SERIAL, "shell", "am", "force-stop", APP]])

    def test_a_step_that_needs_an_app_id_is_refused_without_one(self) -> None:
        self.start(app=False)
        code, payload, _ = self.do('[{"assertVisible":"General"},{"launchApp":{}}]')
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual((payload["step"], payload["command"]), (2, "launchApp"))
        self.assertIn("--app-id", payload["hint"])
        self.assertEqual(self.calls("adb"), [])

    def test_without_a_session_nothing_runs(self) -> None:
        code, payload, _ = self.do(TWO_STEPS)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.NO_ACTIVE_SESSION)
        self.assertEqual(self.inputs(), [])

    def test_a_negative_observe_budget_is_refused_before_any_step(self) -> None:
        self.start()
        code, payload, _ = self.do(TWO_STEPS, "--observe-timeout-ms", "-1")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual(self.calls("adb"), [])

    def test_a_sensitive_value_stays_out_of_the_final_screen(self) -> None:
        session = self.start()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update({"ui_dump": self.screen("field.xml", android_dump(FIELD_EMPTY)),
                      "after_input": {"ui_dump": self.screen("typed.xml",
                                                             android_dump(FIELD_TYPED))}})
        self.write_state(state)
        steps = json.dumps([{"inputText": {"value": SECRET, "sensitive": True,
                                           "requireFocus": False}}])
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertNotIn(SECRET, stdout)
        self.assertIn(f"<{len(SECRET)} chars>", payload["screen"]["outline"])
        for saved in self.trees(session["session_id"]):
            self.assertNotIn(SECRET, saved.read_text(encoding="utf-8"))

    def test_a_sensitive_copied_value_stays_out_of_the_final_screen(self) -> None:
        # the value is never in the steps: it is read off the screen into a
        # sensitive variable, and the final screen still shows it
        session = self.start()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update({"ui_dump": self.screen("code.xml", android_dump(CODE_SHOWN)),
                      "after_input": {}})
        self.write_state(state)
        steps = json.dumps([{"copyTextFrom": {"id": "com.android.settings:id/code",
                                              "into": "CODE", "sensitive": True}}])
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.inputs(), [])
        self.assertNotIn(CODE, stdout)
        self.assertIn(f"<{len(CODE)} chars>", payload["screen"]["outline"])
        self.assertIn('"Settings"', payload["screen"]["outline"])
        [saved] = self.trees(session["session_id"])
        self.assertNotIn(CODE, saved.read_text(encoding="utf-8"))

    def typed_screen(self, rows: list[tuple[str, str, str]]) -> None:
        """An empty field now, ``rows`` once the first input was sent."""
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update({"ui_dump": self.screen("field.xml", android_dump(FIELD_EMPTY)),
                      "after_input": {"ui_dump": self.screen("typed.xml",
                                                             android_dump(rows))}})
        self.write_state(state)

    def shown_texts(self, payload: dict[str, Any]) -> list[str]:
        """The outline and the change lines of a reply."""
        return [*payload["screen"]["outline"].splitlines(),
                *((payload.get("changes") or {}).get("lines") or [])]

    def test_each_sensitive_value_is_masked_on_its_own(self) -> None:
        # a short value next to a long one is still masked, with its own length
        session = self.start()
        card = "4111111111111111"
        self.typed_screen([("android.widget.EditText", card, "com.android.settings:id/card"),
                           ("android.widget.TextView", "123", "com.android.settings:id/cvc"),
                           ("android.widget.TextView", "Settings", "android:id/title")])
        steps = json.dumps([
            {"inputText": {"value": card, "sensitive": True, "requireFocus": False}},
            {"inputText": {"value": "123", "sensitive": True, "requireFocus": False}}])
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(self.inputs()), 2)
        self.assertNotIn(card, stdout)
        lines = self.shown_texts(payload)
        self.assertFalse([line for line in lines if '"123"' in line], lines)
        cvc = next(line for line in lines if "id=com.android.settings:id/cvc" in line)
        self.assertIn('"<3 chars>"', cvc)
        field = next(line for line in lines if "id=com.android.settings:id/card" in line)
        self.assertIn('"<16 chars>"', field)
        self.assertIn('"Settings"', payload["screen"]["outline"])
        [saved] = self.trees(session["session_id"])
        text = saved.read_text(encoding="utf-8")
        self.assertNotIn(card, text)
        nodes = json.loads(text)["nodes"]
        self.assertEqual(sorted(node.get("text") for node in nodes if node.get("text")),
                         ["<16 chars>", "<3 chars>", "Settings"])

    def test_a_sensitive_value_from_a_runflow_env_stays_out_of_the_final_screen(self) -> None:
        # ${PW} is filled by the runFlow's env: overlay, which the executor
        # drops when the runFlow ends; the typed value is still masked
        session = self.start()
        self.typed_screen(FIELD_TYPED)
        steps = json.dumps([{"runFlow": {"env": {"PW": SECRET}, "commands": [
            {"inputText": {"value": "${PW}", "sensitive": True, "requireFocus": False}}]}}])
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(self.inputs()), 1)
        self.assertNotIn(SECRET, stdout)
        self.assertNotIn("hunter2", stdout)
        outline = payload["screen"]["outline"]
        self.assertEqual(outline.count(f'"<{len(SECRET)} chars>"'), 2, outline)
        self.assertNotIn('"<5 chars>"', outline, "the length of '${PW}'")
        for saved in self.trees(session["session_id"]):
            self.assertNotIn("hunter2", saved.read_text(encoding="utf-8"))

    def test_a_sensitive_value_set_again_later_is_still_masked(self) -> None:
        # CODE is read, typed, then read again with a new value: the screen
        # still shows the first value, which the run's final state no longer holds
        session = self.start()
        first, second = "111111", "222222"
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update({
            "ui_dump": self.screen("code1.xml", android_dump([
                ("android.widget.EditText", "", "com.android.settings:id/field"),
                ("android.widget.TextView", first, "com.android.settings:id/code")])),
            "after_input": {"ui_dump": self.screen("code2.xml", android_dump([
                ("android.widget.EditText", first, "com.android.settings:id/field"),
                ("android.widget.TextView", second, "com.android.settings:id/code"),
                ("android.widget.TextView", first, "com.android.settings:id/previous")]))}})
        self.write_state(state)
        copy = {"copyTextFrom": {"id": "com.android.settings:id/code", "into": "CODE",
                                 "sensitive": True}}
        steps = json.dumps([copy, {"inputText": {"value": "${CODE}", "sensitive": True,
                                                 "requireFocus": False}}, copy])
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed")
        # ${CODE} was typed as it was at that step (adb gets it shell-quoted)
        [typed] = self.inputs()
        self.assertEqual(typed[:2], ["input", "text"])
        self.assertIn(first, typed[2])
        self.assertNotIn(first, stdout)
        self.assertNotIn(second, stdout)
        lines = self.shown_texts(payload)
        for name in ("field", "code", "previous"):
            line = next(line for line in lines if f"id=com.android.settings:id/{name}" in line)
            self.assertIn('"<6 chars>"', line)
        saved = self.trees(session["session_id"])
        self.assertTrue(saved)
        for tree in saved:
            text = tree.read_text(encoding="utf-8")
            self.assertNotIn(first, text)
            self.assertNotIn(second, text)

    def shown_screen(self, rows: list[tuple[str, str, str]]) -> None:
        """``rows`` now and after any input."""
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update({"ui_dump": self.screen("shown.xml", android_dump(rows)),
                      "after_input": {}})
        self.write_state(state)

    def line_of(self, payload: dict[str, Any], rid: str) -> str:
        return next(line for line in payload["screen"]["outline"].splitlines()
                    if f"id={rid}" in line)

    def test_a_secret_inside_a_link_masks_only_the_secret(self) -> None:
        # the step that uses ${TOKEN} inside a URL types and stores nothing:
        # only the token is masked, with its own length, never the whole URL
        session = self.start()
        token = "abcd1234efgh"
        url = "https://app.example.com/reset?token=${TOKEN}"
        self.shown_screen([("android.widget.TextView", token, "com.android.settings:id/token"),
                           ("android.widget.Button", "Reset password", ""),
                           ("android.widget.TextView", "Settings", "android:id/title")])
        steps = json.dumps([
            {"copyTextFrom": {"id": "com.android.settings:id/token", "into": "TOKEN",
                              "sensitive": True}},
            {"openLink": {"url": url}}])
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed")
        opened = [argv for argv in self.calls("adb") if "android.intent.action.VIEW" in argv]
        self.assertEqual(len(opened), 1, "the link was opened")
        self.assertIn(token, opened[0][-1])
        self.assertNotIn(token, stdout)
        outline = payload["screen"]["outline"]
        self.assertIn('"<12 chars>"', self.line_of(payload, "com.android.settings:id/token"))
        self.assertIn('"Reset password"', outline)
        self.assertIn('"Settings"', outline)
        self.assertNotIn(f"<{len(url.replace('${TOKEN}', token))} chars>", stdout)
        [saved] = self.trees(session["session_id"])
        text = saved.read_text(encoding="utf-8")
        self.assertNotIn(token, text)
        self.assertIn("Reset password", text)

    def test_a_secret_inside_an_asserted_text_masks_only_the_secret(self) -> None:
        # assertVisible 'Your code: ${CODE}' matches the screen; the label
        # that shares the word 'code' with that sentence stays visible
        session = self.start()
        self.shown_screen([("android.widget.TextView", "Enter your code", "android:id/title"),
                           ("android.widget.TextView", CODE, "com.android.settings:id/code"),
                           ("android.widget.TextView", f"Your code: {CODE}",
                            "com.android.settings:id/message")])
        steps = json.dumps([
            {"copyTextFrom": {"id": "com.android.settings:id/code", "into": "CODE",
                              "sensitive": True}},
            {"assertVisible": {"text": "Your code: ${CODE}"}}])
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual([step["status"] for step in payload["steps"]], ["passed", "passed"])
        self.assertNotIn(CODE, stdout)
        self.assertIn('"Enter your code"', self.line_of(payload, "android:id/title"))
        for rid in ("com.android.settings:id/code", "com.android.settings:id/message"):
            self.assertIn(f'"<{len(CODE)} chars>"', self.line_of(payload, rid))
        self.assertNotIn(f"<{len('Your code: ' + CODE)} chars>", stdout)
        [saved] = self.trees(session["session_id"])
        text = saved.read_text(encoding="utf-8")
        self.assertNotIn(CODE, text)
        self.assertIn("Enter your code", text)

    def test_an_infrastructure_failure_names_the_input_step(self) -> None:
        self.start()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["fail"] = {f"-s {SERIAL} shell input tap": [1, "error: device offline"]}
        self.write_state(state)
        steps = ("- assertVisible: Settings\n"
                 "- tapOn:\n"
                 "    text: General\n")
        code, payload, stdout = self.do(steps)
        self.assertEqual(code, 2, payload)
        self.assertEqual(stdout, "")
        self.assertNotEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual(payload["failure_class"], "infrastructure")
        self.assertEqual((payload["step"], payload["step_index"], payload["line"]), (2, 2, 2))
        self.assertTrue(payload["run_id"].startswith("fr_"), payload)

    def test_an_item_line_with_nothing_after_its_dash_is_refused(self) -> None:
        # "- " used to reach the parser as an item with no content: a raw
        # "string index out of range" with the generic hint
        self.start()
        for steps, step, line in (("- \n", 1, 1), ("- tapOn: General\n-   \n", 2, 2)):
            with self.subTest(steps=steps):
                code, payload, stdout = self.do(steps)
                self.assertEqual(code, 2, payload)
                self.assertEqual(stdout, "")
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
                self.assertIn(f"ui do step {step}: empty sequence item", payload["error"])
                self.assertNotIn("index out of range", payload["error"])
                self.assertEqual((payload["step"], payload["line"]), (step, line))
                self.assertIn("'- launchApp'", payload["hint"])
                self.assertEqual(self.inputs(), [], "nothing ran")

    def test_replay_of_a_ui_do_run_names_ui_do(self) -> None:
        self.start()
        code, payload, _ = self.do(TWO_STEPS)
        self.assertEqual(code, 0, payload)
        run_id = payload["run_id"]
        self.forget_calls()
        code, refused, stdout = self.android("replay", "--run", run_id)
        self.assertEqual(code, 2, refused)
        self.assertEqual(stdout, "")
        self.assertEqual(refused["error_code"], errors.REPLAY_MANIFEST_INVALID)
        self.assertIn(run_id, refused["error"])
        self.assertIn("'ui do'", refused["error"])
        self.assertIn("autonom ui do", refused["hint"])
        self.assertIn("flow run", refused["hint"])
        self.assertNotIn("Restore the source checkout", refused["hint"])
        self.assertEqual((refused["flow"], refused["run_id"]), (inline.INLINE_PATH, run_id))
        self.assertEqual(self.inputs(), [], "replay sent nothing")
        # no copy of the steps was written for replay
        self.assertEqual(list(self.home.rglob("steps.yaml")), [])


# --- iOS --------------------------------------------------------------------------------


class IosUiDoTests(UiDoCase):

    def setUp(self) -> None:
        super().setUp()
        self.main = self.screen("main.json", json.dumps(ios_tree(
            "Settings", ["General", "Accessibility", "Camera"])))
        self.general = self.screen("general.json", json.dumps(ios_tree(
            "General", ["About", "Software Update"])))
        self.write_state({"simctl_devices": BOOTED, "idb_describe_all": self.main,
                          "after_input": {"idb_describe_all": self.general}})

    def ios(self, *argv: str, steps: str | None = None) -> tuple[int, dict[str, Any], str]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--idb", str(self.idb),
                            "--udid", UDID, *argv, steps=steps)

    def taps(self) -> int:
        count = 0
        for argv in self.calls("idb"):
            rest = [arg for arg in argv if not arg.startswith("-")]
            count += rest[:2] == ["ui", "tap"]
        return count

    def test_two_steps_on_ios_return_the_app_and_heading(self) -> None:
        code, payload, _ = self.ios("session", "start")
        self.assertEqual(code, 0, payload)
        self.forget_calls()
        steps = ('[{"tapOn":{"visibleText":"General"}},'
                 '{"assertVisible":{"visibleText":"About"}}]')
        code, payload, _ = self.ios("ui", "do", steps=steps)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(self.taps(), 1)
        self.assertIn("About", payload["screen"]["outline"])
        self.assertEqual(payload["foreground"]["app"], "Settings")

    def test_an_android_only_step_is_refused_on_ios_before_any_step(self) -> None:
        code, payload, _ = self.ios("session", "start")
        self.assertEqual(code, 0, payload)
        self.forget_calls()
        code, payload, _ = self.ios("ui", "do",
                                    steps='[{"tapOn":{"visibleText":"General"}},"back"]')
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual((payload["step"], payload["command"]), (2, "back"))
        self.assertEqual(payload["flow_error_code"], errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(self.taps(), 0)


# --- the loader, in process ---------------------------------------------------------------


def _leaves(steps: list) -> list:
    out = []
    for step in steps:
        out.append(step)
        if isinstance(step.args.get("commands"), list):
            out.extend(_leaves(step.args["commands"]))
    return out


class InlineLoadTests(unittest.TestCase):

    def refusal(self, data: str) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            inline.load(data)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertTrue(caught.exception.hint)
        return caught.exception

    def test_json_and_yaml_build_the_same_steps(self) -> None:
        from_json = inline.load('[{"tapOn":{"text":"General","timeoutMs":500}},'
                                '{"assertVisible":"About"},"back",'
                                '{"setLocation":{"latitude":52.5,"longitude":13.4}}]')
        from_yaml = inline.load("- tapOn:\n    text: General\n    timeoutMs: 500\n"
                                "- assertVisible: About\n- back\n"
                                "- setLocation:\n    latitude: 52.5\n    longitude: 13.4\n")
        self.assertEqual((from_json.format, from_yaml.format), ("json", "yaml"))
        for loaded in (from_json, from_yaml):
            flow = loaded.flow
            self.assertEqual((flow.path, flow.name), (inline.INLINE_PATH, inline.INLINE_NAME))
            self.assertEqual([step.command for step in flow.steps],
                             ["tapOn", "assertVisible", "back", "setLocation"])
            tap = flow.steps[0]
            self.assertEqual(tap.args["selector"].fields, {"text": "General"})
            self.assertEqual(tap.args["selector"].match, "exact")
            self.assertEqual(tap.args["timeoutMs"], 500)
            self.assertEqual(flow.steps[1].args["selector"].fields, {"text": "About"})
            self.assertEqual(flow.steps[3].args, {"latitude": 52.5, "longitude": 13.4})
            self.assertTrue(flow.source_sha256)
        self.assertEqual(from_json.starts, [1, 2, 3, 4])
        self.assertEqual(from_yaml.starts, [1, 4, 5, 6])
        self.assertEqual([from_yaml.step_at(line) for line in (1, 3, 4, 5, 7, 0, None)],
                         [1, 1, 2, 3, 4, None, None])

    def test_selector_fields_under_the_command_are_its_selector(self) -> None:
        def described(data: str) -> list:
            # what a step means, positions (which differ by syntax) left out
            out = []
            for step in _leaves(inline.load(data).flow.steps):
                selector = step.args.get("selector")
                meaning = None if selector is None else {
                    "fields": selector.fields, "match": selector.match,
                    "index": selector.index, "relations": selector.relations,
                    "source_fields": selector.source_fields}
                out.append((step.command, meaning,
                            {key: value for key, value in step.args.items()
                             if key not in ("selector", "commands")}))
            return out

        short = described(
            '[{"tapOn":{"text":"General","index":1,"timeoutMs":700}},'
            '{"assertVisible":{"id":"title","match":"contains","below":{"text":"Wi"}}},'
            '{"group":{"label":"g","commands":[{"scrollUntilVisible":'
            '{"description":"About","direction":"down"}}]}}]')
        full = described(
            "- tapOn:\n    selector:\n      text: General\n      index: 1\n"
            "    timeoutMs: 700\n"
            "- assertVisible:\n    selector:\n      id: title\n      match: contains\n"
            "      below:\n        text: Wi\n"
            "- group:\n    label: g\n    commands:\n      - scrollUntilVisible:\n"
            "          selector:\n            description: About\n"
            "          direction: down\n")
        self.assertEqual([item[0] for item in short],
                         ["tapOn", "assertVisible", "group", "scrollUntilVisible"])
        self.assertEqual(short, full)
        self.assertEqual(short[0][1]["match"], "exact", "exact unless the step says otherwise")
        # both forms at once are not merged: the schema names the stray field
        error = self.refusal('[{"tapOn":{"selector":{"text":"A"},"text":"B"}}]')
        self.assertIn("unknown argument 'text'", error.message)

    def test_json_values_keep_their_json_type(self) -> None:
        # a quoted "true" is text, never a boolean; a quoted number is no integer
        error = self.refusal('[{"tapOn":{"text":"General","optional":"true",'
                             '"reason":"banner"}}]')
        self.assertIn("quoted strings are not booleans", error.message)
        error = self.refusal('[{"tapOn":{"text":"General","timeoutMs":500.0}}]')
        self.assertIn("must be an integer", error.message)
        # an exponent is still a JSON number with a fraction, however large
        for number in ("5e2", "1e16", "1E+20"):
            error = self.refusal('[{"tapOn":{"text":"General","timeoutMs":%s}}]' % number)
            self.assertIn("must be an integer", error.message)
        loaded = inline.load('[{"setLocation":{"latitude":1e1,"longitude":-1.5e-3}}]')
        self.assertEqual(loaded.flow.steps[0].args, {"latitude": 10.0, "longitude": -0.0015})
        loaded = inline.load('[{"tapOn":{"text":"true"}}]')
        self.assertEqual(loaded.flow.steps[0].args["selector"].fields, {"text": "true"})

    def test_every_malformed_input_names_the_step_and_the_problem(self) -> None:
        cases = [
            ('[{"tapOn":"General"},{"tapOnn":"About"}]', 2, "unknown command"),
            ('[{"tapOn":"A"},{"tapOn":"B","back":{}}]', 2, "exactly one command key"),
            ('[{"tapOn":"A"},[]]', 2, "not an array"),
            ('[{"tapOn":"A"},{"tapOn":null}]', 2, "null is not"),
            ('[{"tapOn":{"text":"A","text":"B"}}]', 1, "duplicate key"),
            ('[{"tapOn":{"text":"A","optional":true}}]', 1, "no reason"),
            ('[{"tapOn":{"text":"A"}},{"openLink":"settings"}]', 2, "no scheme"),
            ('[{"tapOn":"A"},{"runFlow":"login.yaml"}]', 2, "runFlow file"),
            ('[{"group":{"label":"g","commands":[{"runFlow":{"file":"x.yaml"}}]}}]', 1,
             "runFlow file"),
            ('[{"inputText":"${bad name}"}]', 1, "interpolation"),
            ('[{"tapOn":"A"},{"setLocation":{"latitude":1e999,"longitude":1}}]', 2,
             "not a finite number"),
            ("- tapOn: General\n- swipe: sideways\n", 2, "must be one of"),
            ("- tapOn: General\n- assertVisible: {text: About}\n", 2, "flow mappings"),
        ]
        for data, step, problem in cases:
            with self.subTest(data=data):
                error = self.refusal(data)
                self.assertEqual(error.extra.get("step"), step, error.message)
                self.assertIn(f"step {step}", error.message)
                self.assertIn(problem, error.message)
                self.assertNotIn(inline.INLINE_PATH, error.message)

    def test_a_yaml_refusal_carries_its_line(self) -> None:
        error = self.refusal("- tapOn: General\n- assertVisible:\n\ttext: About\n")
        self.assertEqual((error.extra["step"], error.extra["line"]), (2, 3))
        self.assertEqual(error.extra["reason"], "tab_indent")
        self.assertEqual(error.extra["flow_error_code"], errors.FLOW_PARSE_ERROR)

    def test_input_that_is_not_a_step_list(self) -> None:
        cases = [
            ("", "no steps"),
            ("  \n# only a comment\n", "no steps"),
            ("[]", "no steps"),
            ('{"tapOn":"General"}', "array of steps"),
            ('[{"tapOn": }]', "not valid JSON"),
            ('[{"setLocation":{"latitude":NaN,"longitude":1}}]', "not valid JSON"),
            ("schema: autonom.dev/flow/v1\nname: x\n---\n- back\n", "'---'"),
            ("tapOn: General\n", "sequence"),
        ]
        for data, problem in cases:
            with self.subTest(data=data):
                error = self.refusal(data)
                self.assertIn(problem, error.message)
                self.assertNotIn("step", error.extra)
        error = self.refusal(b'["caf\xe9"]')
        self.assertIn("not UTF-8", error.message)

    def test_an_item_with_nothing_after_its_dash_names_its_step_and_line(self) -> None:
        cases = [
            ("- \n", 1, 1),
            ("-   \n", 1, 1),
            ("- tapOn: General\n- \n", 2, 2),
            ("- tapOn: General\n- group:\n    label: g\n    commands:\n      - \n", 2, 5),
        ]
        for data, step, line in cases:
            with self.subTest(data=data):
                error = self.refusal(data)
                self.assertIn(f"step {step}: empty sequence item", error.message)
                self.assertEqual((error.extra["step"], error.extra["line"]), (step, line))
                self.assertEqual(error.extra["reason"], "empty_sequence_item")
                self.assertEqual(error.extra["flow_error_code"], errors.FLOW_PARSE_ERROR)
                self.assertIn("'- launchApp'", error.hint)
        # trailing spaces anywhere else change nothing
        loaded = inline.load("- tapOn: General   \n- assertVisible:  \n    text: About \n"
                             "- inputText: \"a b \"  \n- back \n")
        self.assertEqual([step.command for step in loaded.flow.steps],
                         ["tapOn", "assertVisible", "inputText", "back"])
        self.assertEqual(loaded.flow.steps[0].args["selector"].fields, {"text": "General"})
        self.assertEqual(loaded.flow.steps[1].args["selector"].fields, {"text": "About"})
        self.assertEqual(loaded.flow.steps[2].args["value"], "a b ")

    def test_a_bom_and_crlf_are_accepted(self) -> None:
        loaded = inline.load("﻿- tapOn: General\r\n- back\r\n")
        self.assertEqual([step.command for step in loaded.flow.steps], ["tapOn", "back"])

    def test_bind_app_gives_the_steps_the_session_app(self) -> None:
        loaded = inline.load('[{"launchApp":{}}]')
        inline.bind_app(loaded, APP)
        self.assertEqual(loaded.flow.app_id, APP)
        loaded = inline.load('[{"group":{"label":"g","commands":["stopApp"]}}]')
        with self.assertRaises(errors.AutonomError) as caught:
            inline.bind_app(loaded, None)
        self.assertEqual((caught.exception.extra["step"], caught.exception.extra["command"]),
                         (1, "stopApp"))
        grant = '[{"setPermissions":{"action":"grant","service":"camera"%s}}]'
        with self.assertRaises(errors.AutonomError) as caught:
            inline.bind_app(inline.load(grant % ""), None)
        self.assertEqual(caught.exception.extra["command"], "setPermissions")
        # a step that names its own app needs none from the session
        inline.bind_app(inline.load(grant % ',"appId":"com.other"'), None)

    @staticmethod
    def executor() -> Any:
        """A flow executor for steps that never reach a device."""
        from types import SimpleNamespace

        from autonom_lib.flow import executor as flow_executor

        target = SimpleNamespace(platform="android", tool="adb", target_id=SERIAL)
        return flow_executor.Executor(target, {}, flow_executor.RunConfig())

    def test_watch_starts_with_the_literal_sensitive_values(self) -> None:
        # known before any step: each sensitive value that names no ${NAME}
        loaded = inline.load(json.dumps([
            {"inputText": "visible"},
            {"inputText": {"value": SECRET, "sensitive": True}},
            {"inputText": {"value": "123", "sensitive": True}},
            {"inputText": {"value": SECRET, "sensitive": True}},
            {"group": {"label": "g", "commands": [
                {"setClipboard": {"value": "pin-$${x}", "sensitive": True}}]}},
            {"inputText": {"value": "${NAME}-1", "sensitive": True}},
            {"setClipboard": {"value": "plain"}}]))
        self.assertEqual(inline.watch(self.executor(), loaded), [SECRET, "123", "pin-${x}"])
        self.assertEqual(inline.watch(self.executor(), inline.load('[{"inputText":"x"}]')), [])
        # an executor without the methods it wraps fails before any step
        from types import SimpleNamespace
        with self.assertRaises(AttributeError):
            inline.watch(SimpleNamespace(), loaded)

    def test_watch_records_each_value_as_the_step_used_it(self) -> None:
        # a variable set again later does not unmask what an earlier step
        # typed, set or read: the run's final state keeps only the last value
        from autonom_lib.flow import executor as flow_executor

        runner = self.executor()
        loaded = inline.load(json.dumps([
            {"setClipboard": {"value": "Ada", "into": "NAME"}},
            {"setClipboard": {"value": "${NAME}-1", "into": "CODE", "sensitive": True}},
            {"setClipboard": {"value": "Bob", "into": "NAME"}},
            {"setClipboard": {"value": "${NAME}-2", "into": "CODE", "sensitive": True}},
            {"setClipboard": {"value": "x${CODE}", "into": "OTHER"}},
            {"setClipboard": {"value": "${PW}!", "into": "P", "sensitive": True}},
            {"setClipboard": {"value": "${MISSING}", "into": "Q", "sensitive": True}},
            {"setClipboard": {"value": "${NAME}+", "into": "R"}}]))
        values = inline.watch(runner, loaded)
        self.assertEqual(values, [])
        steps = loaded.flow.steps
        for step in steps[:5]:
            runner._dispatch(step, loaded.flow, [0])  # noqa: SLF001
        # the frame of an inline runFlow with env: PW, dropped when it ends
        runner.values = {"PW": SECRET}
        runner._dispatch(steps[5], loaded.flow, [0])  # noqa: SLF001
        runner.values = {}
        self.assertEqual(values, ["Ada-1", "Bob-2", "xBob-2", f"{SECRET}!"])
        self.assertEqual(runner.runtime_values["CODE"], "Bob-2")
        # a step that fails still records what the run keeps sensitive ...
        runner.runtime_values["LATE"] = "late-value"
        runner.sensitive_var_names.add("LATE")
        with self.assertRaises(errors.AutonomError):
            runner._dispatch(steps[6], loaded.flow, [0])  # noqa: SLF001
        self.assertEqual(values[-1], "late-value")
        # ... and a later plain step that uses no secret records nothing
        runner._dispatch(steps[7], loaded.flow, [0])  # noqa: SLF001
        self.assertEqual(runner.runtime_values["R"], "Bob+")
        self.assertNotIn("Bob+", values)
        recorded = list(values)
        # a string that only holds a sensitive variable (a URL, an asserted
        # text, a selector, filled outside an input step) records nothing
        self.assertEqual(runner._resolve("https://x.example/?t=${CODE}"),  # noqa: SLF001
                         ("https://x.example/?t=Bob-2", True))
        self.assertEqual(values, recorded)
        # an input step that types a sensitive variable inside a longer value
        # records the value exactly as it typed it; a plain one records nothing
        typed: list[str] = []
        runner._admit = lambda: None  # noqa: SLF001 - no control lease here
        typing = inline.load(json.dumps([
            {"inputText": {"value": "Code ${CODE}", "requireFocus": False}},
            {"inputText": {"value": "Hello ${NAME}", "requireFocus": False}}]))
        with mock.patch.object(flow_executor.ui_mod, "type_text",
                               lambda _target, text: typed.append(text)):
            for step in typing.flow.steps:
                runner._dispatch(step, typing.flow, [0])  # noqa: SLF001
        self.assertEqual(typed, ["Code Bob-2", "Hello Bob"])
        self.assertEqual(values, [*recorded, "Code Bob-2"])
        # only this one executor is wrapped
        self.assertNotIn("_dispatch", vars(self.executor()))
        self.assertIs(type(runner)._dispatch, flow_executor.Executor._dispatch)  # noqa: SLF001

    def test_mask_matches_each_value_on_its_own(self) -> None:
        from autonom_lib import observe

        card = "4111111111111111"
        nodes = [
            {"ref": "e1", "role": "text", "text": "123"},
            {"ref": "e2", "role": "edittext", "text": card, "editable": True},
            {"ref": "e3", "role": "text", "text": "Card ending 1111"},
            {"ref": "e4", "role": "edittext", "text": "\u2022\u2022\u2022\u2022",
             "editable": True},
            {"ref": "e5", "role": "text", "text": "Settings", "desc": "!!!!"},
            {"ref": "e6", "role": "text", "text": "A"},
        ]
        # one value is masked exactly as after `ui type --sensitive`
        for value in (card, "123", "!!!!"):
            with self.subTest(value=value):
                self.assertEqual(inline.mask(nodes, [value]), observe.masked(nodes, value))
        self.assertIs(inline.mask(nodes, []), nodes)
        # joined into one string, the short values would stay visible
        joined = observe.masked(nodes, f"{card} 123 !!!! abcdef")
        self.assertEqual((joined[0]["text"], joined[4]["desc"]), ("123", "!!!!"))
        masked = inline.mask(nodes, [card, "123", "!!!!", "abcdef"])
        self.assertEqual([(node.get("text"), node.get("desc")) for node in masked], [
            ("<3 chars>", None),
            ("<16 chars>", None),
            ("<16 chars>", None),   # the last 4 digits of the card
            ("<4 chars>", None),    # a field showing none of them: its own length
            ("Settings", "<4 chars>"),
            ("<16 chars>", None),   # one character: every value may show it
        ])
        self.assertEqual([node["ref"] for node in masked], [node["ref"] for node in nodes])
        self.assertEqual(nodes[0]["text"], "123", "the nodes given are not changed")

    def test_scrub_cuts_each_value_out_of_an_error(self) -> None:
        card = "4111111111111111"
        error = {"code": "backend_failed", "message": f"typed {card} then 123",
                 "hint": "1111 and 123"}
        self.assertEqual(inline.scrub(error, [card, "123", "1111"]), {
            "code": "backend_failed", "message": "typed <16 chars> then <3 chars>",
            "hint": "<4 chars> and <3 chars>"})
        self.assertIs(inline.scrub(error, []), error)

    def test_masking_swaps_observe_masking_only_while_open(self) -> None:
        from autonom_lib import observe

        card = "4111111111111111"
        original = (observe.masked, observe._scrubbed)
        with inline.masking([]) as secret:
            self.assertIsNone(secret)
            self.assertEqual((observe.masked, observe._scrubbed), original)
        with inline.masking([SECRET]) as secret:
            self.assertEqual(secret, SECRET)
            self.assertEqual((observe.masked, observe._scrubbed), original)
        with inline.masking([card, "123"]) as secret:
            self.assertEqual(secret, card)
            # what observe calls while it reads the final screen
            [node] = observe.masked([{"role": "text", "text": "123"}], secret)
            self.assertEqual(node["text"], "<3 chars>")
            error = observe._scrubbed({"code": "x", "message": f"{card}/123"}, secret)
            self.assertEqual(error["message"], "<16 chars>/<3 chars>")
        self.assertEqual((observe.masked, observe._scrubbed), original)
        with self.assertRaises(KeyboardInterrupt):
            with inline.masking([card, "123"]):
                raise KeyboardInterrupt
        self.assertEqual((observe.masked, observe._scrubbed), original)

    def test_a_terminal_is_refused_without_reading(self) -> None:
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

            def read(self, *_args: Any) -> str:
                raise AssertionError("read a terminal")

        with self.assertRaises(errors.AutonomError) as caught:
            inline.read(Terminal())
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertIn("Pipe", caught.exception.hint)

    def test_steps_nested_without_end_are_refused(self) -> None:
        depth = 2000
        data = ("[" + '{"runFlow":{"commands":[' * depth + '"back"' + "]}}" * depth + "]")
        error = self.refusal(data)
        self.assertIn("nested too deeply", error.message)

    def test_more_than_the_limit_is_refused(self) -> None:
        stream = io.BytesIO(b" " * (inline.MAX_BYTES + 1))
        with self.assertRaises(errors.AutonomError) as caught:
            inline.read(stream)
        self.assertIn("1 MiB", caught.exception.message)
        self.assertEqual(inline.read(io.BytesIO(b"[]")), b"[]")


class _Executor:
    """Stands in for the flow executor: raises ``error`` from ``run``, after
    starting the run (``_writer_run_id``) when ``started``."""

    def __init__(self, error: BaseException, started: bool = False) -> None:
        self.error = error
        self._writer_run_id = None
        self.started = started

    def run(self, _flow: Any) -> Any:
        if self.started:
            self._writer_run_id = "fr_0000000000"
        raise self.error


class InlineRunTests(unittest.TestCase):
    """`run` turns only a definition refusal made before the run started into
    the input's invalid_value; `reply` reports what the executor recorded."""

    def setUp(self) -> None:
        self.steps = inline.load("- assertVisible: General\n- back\n")

    def test_a_refusal_before_the_run_names_the_step(self) -> None:
        refusal = errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM, "<stdin>:2:3: back is not supported on iOS",
            hint="tap the navigation bar's back control", line=2, column=3, command="back",
            file="<stdin>")
        with self.assertRaises(errors.AutonomError) as caught:
            inline.run(_Executor(refusal), self.steps)
        error = caught.exception
        self.assertEqual(error.code, errors.INVALID_VALUE)
        self.assertEqual(error.message, "ui do step 2 (back): back is not supported on iOS")
        self.assertEqual(error.hint, "tap the navigation bar's back control")
        self.assertEqual((error.extra["step"], error.extra["line"], error.extra["column"]),
                         (2, 2, 3))
        self.assertEqual(error.extra["flow_error_code"], errors.UNSUPPORTED_ON_PLATFORM)

    def test_other_failures_stay_the_executors(self) -> None:
        cases = [
            # a definition error inside a running step: the run exists
            (errors.AutonomError(errors.FLOW_VAR_UNDEFINED, "x", step_index=2), True),
            # a dead backend before the run: not the input's fault
            (errors.AutonomError(errors.BACKEND_FAILED, "adb went away"), False),
        ]
        for error, started in cases:
            with self.subTest(code=error.code, started=started):
                with self.assertRaises(errors.AutonomError) as caught:
                    inline.run(_Executor(error, started), self.steps)
                self.assertIs(caught.exception, error)

    def test_a_failure_after_the_start_names_the_input_step(self) -> None:
        steps = inline.load("- assertVisible: General\n- group:\n    label: g\n"
                            "    commands:\n      - back\n")
        error = errors.AutonomError(errors.BACKEND_FAILED, "adb went away", step_index=3,
                                    line=5, command="back")
        with self.assertRaises(errors.AutonomError) as caught:
            inline.run(_Executor(error, started=True), steps)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.extra["step"], 2)
        self.assertEqual(error.code, errors.BACKEND_FAILED)
        # an error without a line names no step
        bare = errors.AutonomError(errors.BACKEND_FAILED, "adb went away")
        with self.assertRaises(errors.AutonomError):
            inline.run(_Executor(bare, started=True), steps)
        self.assertNotIn("step", bare.extra)

    def test_recursion_in_the_executor_is_one_refusal(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            inline.run(_Executor(RecursionError("deep")), self.steps)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertIn("nested too deeply", caught.exception.message)

    def test_reply_of_an_interrupted_run(self) -> None:
        from types import SimpleNamespace

        outcome = SimpleNamespace(index=1, line=1, command="assertVisible", status="passed",
                                  duration_ms=12, label=None, depth=0, error_code=None,
                                  skip_reason=None)
        refused = SimpleNamespace(index=2, line=2, command="back", status="skipped",
                                  duration_ms=0, label=None, depth=0,
                                  error_code=errors.CONTROL_HELD, skip_reason="control_lost")
        result = SimpleNamespace(
            status="interrupted", run_id="fr_1", events_path="/e", warnings=[],
            steps=[outcome, refused],
            failure={"step_index": 2, "command": "back", "line": 2,
                     "error_code": errors.CONTROL_HELD, "failure_class": "control_lost",
                     "error": "held", "holder": {"kind": "human"}, "generation": 3})
        payload = inline.reply(self.steps, result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure"], {
            "step": 2, "index": 2, "command": "back", "code": errors.CONTROL_HELD,
            "message": "held", "failure_class": "control_lost",
            "holder": {"kind": "human"}, "generation": 3})
        self.assertEqual(payload["steps"][1], {
            "index": 2, "step": 2, "command": "back", "status": "skipped", "duration_ms": 0,
            "error_code": errors.CONTROL_HELD, "skip_reason": "control_lost"})
        self.assertNotIn("warnings", payload)
        # a refusal before any step has no step to name
        result.failure = {"step_index": None, "command": "setup", "line": None,
                          "error_code": errors.CONTROL_HELD, "error": "held",
                          "failure_class": "control_lost"}
        self.assertIsNone(inline.reply(self.steps, result)["failure"]["step"])


if __name__ == "__main__":
    unittest.main()
