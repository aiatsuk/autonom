"""The short text replies of an agent-profile session, `--json` and
`--screen`, the fast-path line, and `--shot` on the action verbs.

`autonom_lib.brief` is tested on replies written out here; the CLI cases run
the real CLI in a subprocess against `fake_adb.py` (the harness of
`test_wait.py`) and read exactly what it printed. No real device.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import brief, errors  # noqa: E402

try:
    from test_observe import SERIAL  # noqa: E402
    from test_wait import WaitCase, flow  # noqa: E402
except ImportError:  # direct `python3 -m unittest tests.test_brief` runs
    from tests.test_observe import SERIAL  # noqa: E402
    from tests.test_wait import WaitCase, flow  # noqa: E402


OBSERVED: dict[str, Any] = {
    "ok": True, "x": 540, "y": 390, "ref": "n3", "backend": "adb",
    "detail": "actions/0001_tap.json",
    "screen": {"outline": 'n1 text "General"\nn2 button "About" [clickable]', "count": 2,
               "truncated": False, "saved": "/s/trees/0001.json"},
    "changes": {"added": 1, "removed": 1, "changed": 0,
                "lines": ['+ n2 button "About" [clickable]', '- n2 button "Display"']},
    "changes_baseline": "pre_read",
    "foreground": {"app": "com.android.settings", "screen": ".MainActivity"},
    "platform": "android", "target_id": "emulator-5554",
}


class ActionTextTests(unittest.TestCase):

    def test_status_then_screen_then_what_changed(self) -> None:
        self.assertEqual(brief.action("ui tap", OBSERVED).splitlines(), [
            "ui tap ok at 540,390 (n3)",
            "screen: com.android.settings .MainActivity, 2 nodes",
            "changes: +1 -1 ~0",
            '+ n2 button "About" [clickable]',
            '- n2 button "Display"',
        ])

    def test_nothing_it_does_not_need(self) -> None:
        text = brief.action("ui tap", OBSERVED)
        for word in ("actions/0001_tap.json", "/s/trees/0001.json", "pre_read", "backend",
                     "emulator-5554", "{"):
            self.assertNotIn(word, text)

    def test_an_unchanged_screen_is_one_word(self) -> None:
        same = {**OBSERVED, "changes": {"added": 0, "removed": 0, "changed": 0, "lines": []}}
        self.assertEqual(brief.action("ui key", {**same, "keycode": "BACK", "x": None,
                                                 "y": None}).splitlines()[-1], "changes: none")

    def test_the_whole_outline_only_on_request(self) -> None:
        self.assertNotIn("outline:", brief.action("ui tap", OBSERVED))
        lines = brief.action("ui tap", OBSERVED, screen=True).splitlines()
        self.assertEqual(lines[-3:], ["outline:", 'n1 text "General"',
                                      'n2 button "About" [clickable]'])

    def test_without_a_baseline_the_screen_is_the_news(self) -> None:
        first = {**OBSERVED, "changes": None, "changes_baseline": "none",
                 "changes_baseline_reason": "the session has no saved tree"}
        lines = brief.action("ui tap", first).splitlines()
        self.assertEqual(lines[2:], [
            "changes: unknown (the session has no saved tree); outline:",
            'n1 text "General"', 'n2 button "About" [clickable]'])

    def test_more_changes_than_lines_says_how_many(self) -> None:
        many = {**OBSERVED, "changes": {"added": 30, "removed": 0, "changed": 0,
                                        "lines": [f"+ n{i}" for i in range(20)]}}
        self.assertEqual(brief.action("ui tap", many).splitlines()[-1],
                         "... 10 more (--screen for the whole screen)")

    def test_a_reply_without_observe_is_one_line(self) -> None:
        self.assertEqual(brief.action("ui tap", {"ok": True, "x": 1, "y": 2, "ref": None}),
                         "ui tap ok at 1,2")

    def test_what_each_verb_acted_on(self) -> None:
        for verb, data, line in (
                ("ui tap", {"x": 5, "y": 6, "ref": None, "duration_ms": 600},
                 "ui tap ok at 5,6 held 600 ms"),
                ("ui swipe", {"from": [100, 900], "to": [100, 300], "duration": 0.3},
                 "ui swipe ok 100,900 -> 100,300"),
                ("ui pinch", {"gesture": "pinch", "at": [200, 400], "scale": 2.0},
                 "ui pinch ok scale 2.0 at 200,400"),
                ("ui key", {"keycode": "BACK"}, "ui key ok BACK"),
                ("ui type", {"typed": "hello"}, 'ui type ok "hello"'),
                ("ui type", {"typed": "<14 chars>", "sensitive": True},
                 "ui type ok <14 chars>"),
                ("ui type", {"typed": "héllo", "complete": False},
                 'ui type ok "héllo" (not all of it landed)'),
                ("ui fill", {"filled": "new name", "ref": "n7", "tapped": True},
                 'ui fill ok "new name" (n7)'),
                ("ui fill", {"filled": "<8 chars>", "sensitive": True, "ref": "n7",
                             "text_matches": False},
                 "ui fill ok <8 chars> (n7) (the field shows something else)")):
            with self.subTest(verb=verb, data=data):
                self.assertEqual(brief.action(verb, {"ok": True, **data}), line)

    def test_the_wait_it_made(self) -> None:
        until = {"met": True, "elapsed_ms": 840, "timeout_ms": 10000, "reads": 4, "conditions": [
            {"wait": "request", "path": "*/orders*", "met": True,
             "request": {"id": "f_7", "method": "GET", "host": "api.example",
                         "path": "/v1/orders", "status": 200, "duration_ms": 31}},
            {"wait": "gone", "selector": {"text": "Loading"}, "met": True, "count": 0}]}
        lines = brief.action("ui tap", {**OBSERVED, "until": until}).splitlines()
        self.assertEqual(lines[1], "until met in 840 ms: request */orders* (GET "
                                   'api.example/v1/orders 200 31 ms id f_7); gone: text "Loading"')

    def test_an_unmet_wait_is_its_warning(self) -> None:
        data = {"ok": True, "x": 1, "y": 2, "until": {"met": False, "elapsed_ms": 300},
                "warnings": [{"code": "until_not_met",
                              "error": 'not met within 300 ms: text "Wi-Fi"',
                              "hint": "The input of ui tap was sent: do not send it again."}]}
        self.assertEqual(brief.action("ui tap", data).splitlines(), [
            "ui tap ok at 1,2",
            '! until_not_met: not met within 300 ms: text "Wi-Fi". The input of ui tap was '
            "sent: do not send it again."])

    def test_what_is_over_the_screen(self) -> None:
        signals = {"keyboard": {"visible": True, "package": "com.ime", "bounds": [0, 1, 2, 3]},
                   "dialog": {"kind": "permission", "package": "com.android.permission",
                              "title": "Allow Maps to use your location?", "buttons": []}}
        data = {**OBSERVED, "screen": {**OBSERVED["screen"], "signals": signals,
                                       "truncated": True}}
        self.assertEqual(
            brief.action("ui tap", data).splitlines()[1],
            "screen: com.android.settings .MainActivity, 2 nodes (more not listed), keyboard "
            'up, permission dialog "Allow Maps to use your location?"')
        hidden = {"keyboard": {"visible": False, "package": None, "bounds": None},
                  "dialog": None}
        calm = {**OBSERVED, "screen": {**OBSERVED["screen"], "signals": hidden}}
        self.assertEqual(brief.action("ui tap", calm).splitlines()[1],
                         "screen: com.android.settings .MainActivity, 2 nodes")

    def test_a_failed_observe_and_warnings_come_last(self) -> None:
        data = {"ok": True, "x": 1, "y": 2,
                "observe_error": {"code": "ui_not_idle", "message": "never went idle",
                                  "hint": "Run autonom ui tree."},
                "warnings": [{"code": "screen_not_settled", "error": "still changing",
                              "hint": "Raise --observe-timeout-ms"}],
                "shot": {"path": "/s/shots/0001_done.jpg", "label": "done"}}
        self.assertEqual(brief.action("ui tap", data).splitlines(), [
            "ui tap ok at 1,2",
            "shot: /s/shots/0001_done.jpg",
            "! observe failed (ui_not_idle): never went idle. Run autonom ui tree.",
            "! screen_not_settled: still changing. Raise --observe-timeout-ms."])


class FastPathTests(unittest.TestCase):

    DATA = {**OBSERVED, "ui_backend": "uiautomator", "input_backend": "adb",
            "fallback_reason": "uiautomation_busy",
            "fallback_hint": "Another UiAutomation user holds the device; stop it, or wait."}

    def test_it_is_the_first_line_and_says_how_to_get_it_back(self) -> None:
        for text in (brief.action("ui tap", self.DATA),
                     brief.waited({**self.DATA, "wait": "visible", "selector": {"text": "A"},
                                   "met": True, "elapsed_ms": 5}),
                     brief.steps({**self.DATA, "status": "passed", "steps": []})):
            with self.subTest(text=text[:30]):
                self.assertEqual(
                    text.splitlines()[0],
                    "! not on the fast path: uiautomation_busy. Another UiAutomation user "
                    "holds the device; stop it, or wait.")

    def test_no_line_on_the_fast_path(self) -> None:
        self.assertFalse(brief.action("ui tap", OBSERVED).startswith("!"))

    def test_the_json_line_names_it_right_after_ok(self) -> None:
        spec = importlib.util.spec_from_file_location("autonom_cli_brief",
                                                      ROOT / "scripts/autonom.py")
        cli = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cli)
        ordered = cli._fast_path_first(dict(self.DATA))  # noqa: SLF001
        self.assertEqual(list(ordered)[:3], ["ok", "fallback_reason", "fallback_hint"])
        self.assertEqual(ordered, self.DATA, "the same keys and values")
        untouched = dict(OBSERVED)
        self.assertIs(cli._fast_path_first(untouched), untouched)  # noqa: SLF001


class WaitTextTests(unittest.TestCase):

    def test_a_node(self) -> None:
        met = {"ok": True, "wait": "visible", "selector": {"text": "About"}, "met": True,
               "count": 1, "reads": 3, "elapsed_ms": 420, "timeout_ms": 5000}
        self.assertEqual(brief.waited(met), 'wait met in 420 ms: text "About" (1 on screen)')
        unmet = {**met, "ok": False, "met": False, "count": 0, "elapsed_ms": 5010,
                 "warnings": [{"code": "wait_not_met", "error": "not met within 5000 ms: ...",
                               "hint": "Read it with 'autonom ui tree'."}]}
        self.assertEqual(brief.waited(unmet).splitlines(), [
            'wait NOT met after 5010 ms: text "About" (0 on screen)',
            "! Read it with 'autonom ui tree'."])

    def test_a_node_gone(self) -> None:
        gone = {"ok": True, "wait": "gone", "selector": {"resource_id": "app:id/spinner"},
                "met": True, "count": 0, "elapsed_ms": 900}
        self.assertEqual(brief.waited(gone),
                         'wait met in 900 ms: gone: id "app:id/spinner" (0 on screen)')

    def test_a_request(self) -> None:
        met = {"ok": True, "wait": "request", "path": "*/orders*", "method": "POST", "met": True,
               "request": {"id": "f_3", "method": "POST", "host": "api.example",
                           "path": "/v1/orders", "status": 201, "mocked": True,
                           "duration_ms": 12},
               "elapsed_ms": 630, "timeout_ms": 10000}
        self.assertEqual(brief.waited(met),
                         "wait met in 630 ms: request POST */orders* (POST "
                         "api.example/v1/orders 201 mocked 12 ms id f_3)")
        aborted = {**met, "request": {"id": "f_4", "method": "GET", "host": "h", "path": "/p",
                                      "completed": False}}
        self.assertIn("(GET h/p did not complete id f_4)", brief.waited(aborted))

    def test_settled(self) -> None:
        self.assertEqual(brief.waited({"ok": True, "settled": True, "elapsed_ms": 620}),
                         "settled in 620 ms")
        unsettled = {"ok": False, "settled": False, "elapsed_ms": 5003,
                     "warnings": [{"code": "screen_not_settled", "error": "still changing",
                                   "hint": "Raise --timeout-ms."}]}
        self.assertEqual(brief.waited(unsettled).splitlines(), [
            "NOT settled after 5003 ms",
            "! screen_not_settled: still changing. Raise --timeout-ms."])


class StepsTextTests(unittest.TestCase):

    def test_each_step_the_failure_and_the_final_screen(self) -> None:
        data = {"ok": False, "status": "failed", "run_id": "r_1", "steps": [
            {"index": 1, "command": "tapOn", "status": "passed", "duration_ms": 80},
            {"index": 2, "command": "assertVisible", "status": "failed",
             "error_code": "flow_assertion_timeout", "duration_ms": 10000},
            {"index": 3, "command": "inputText", "status": "skipped",
             "skip_reason": "after_failure"}],
            "failure": {"step": 2, "index": 2, "command": "assertVisible",
                        "code": "flow_assertion_timeout",
                        "message": 'no node matched text "Done" within 10000 ms'},
            **{key: OBSERVED[key] for key in ("screen", "changes", "foreground")}}
        self.assertEqual(brief.steps(data).splitlines(), [
            "ui do failed: 3 step(s)",
            "  1. tapOn passed",
            "  2. assertVisible failed (flow_assertion_timeout)",
            "  3. inputText skipped (after_failure)",
            'failed at step 2 (assertVisible): flow_assertion_timeout: no node matched text '
            '"Done" within 10000 ms.',
            "screen: com.android.settings .MainActivity, 2 nodes",
            "changes: +1 -1 ~0",
            '+ n2 button "About" [clickable]',
            '- n2 button "Display"'])


# --- the CLI ----------------------------------------------------------------------------------


class TextReplyCliTests(WaitCase):

    def setUp(self) -> None:
        super().setUp()
        self.update_state(after_input={"ui_dump": self.general})

    def text(self, *argv: str) -> tuple[int, list[str], str]:
        code, stdout, stderr = self.run_text("--adb", str(self.adb), "--serial", SERIAL, *argv)
        return code, stdout.splitlines(), stderr

    def test_an_agent_session_answers_an_action_in_text(self) -> None:
        self.start("--profile", "agent")
        code, lines, _ = self.text("ui", "tap", "--text", "General")
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[:3], ["ui tap ok at 540,390 (n3)",
                                     "screen: com.android.settings .MainActivity, 3 nodes",
                                     "changes: +3 -4 ~0"])
        self.assertEqual(len([line for line in lines if line.startswith("+ ")]), 3)
        self.assertTrue(any(line.startswith("- ") and '"Display"' in line for line in lines))
        self.assertEqual(len(lines), 10, "status, screen, changes and the seven changed nodes")
        self.assertEqual(self.inputs(), [["input", "tap", "540", "390"]])

    def test_json_gives_the_json_line(self) -> None:
        self.start("--profile", "agent")
        code, lines, _ = self.text("ui", "tap", "--text", "General", "--json")
        self.assertEqual((code, len(lines)), (0, 1))
        payload = json.loads(lines[0])
        self.assertIn('"About"', payload["screen"]["outline"])
        self.assertEqual(payload["changes"]["added"], 3)

    def test_the_journal_keeps_the_whole_reply_either_way(self) -> None:
        self.start("--profile", "agent")
        self.text("ui", "tap", "--text", "General")
        code, payload, _ = self.android("journal", "--verb", "ui tap")
        self.assertEqual(code, 0, payload)
        [entry] = payload["entries"]
        self.assertTrue(entry["ok"])
        self.assertIn("ui tap", json.dumps(entry))

    def test_screen_adds_the_outline(self) -> None:
        self.start("--profile", "agent")
        code, lines, _ = self.text("ui", "tap", "--text", "General", "--screen")
        self.assertEqual(code, 0, lines)
        at = lines.index("outline:")
        self.assertEqual(len(lines[at + 1:]), 3)
        self.assertIn('"Language"', lines[-1])

    def test_a_default_session_keeps_the_json_line(self) -> None:
        self.start()
        code, lines, _ = self.text("ui", "tap", "--text", "General")
        self.assertEqual((code, len(lines)), (0, 1))
        payload = json.loads(lines[0])
        self.assertEqual((payload["ok"], payload["x"], payload["y"]), (True, 540, 390))
        self.assertNotIn("screen", payload)

    def test_no_observe_is_a_single_line(self) -> None:
        self.start("--profile", "agent")
        code, lines, _ = self.text("ui", "key", "KEYCODE_BACK", "--no-observe")
        self.assertEqual((code, lines), (0, ["ui key ok KEYCODE_BACK"]))

    def test_an_unmet_until_says_the_input_was_sent(self) -> None:
        self.start("--profile", "agent")
        code, lines, _ = self.text("ui", "tap", "--text", "General", "--until", "text=Wi-Fi",
                                   "--until-timeout-ms", "200")
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "ui tap ok at 540,390 (n3)")
        self.assertTrue(lines[-1].startswith('! until_not_met: not met within 200 ms: text '
                                             '"Wi-Fi". The input of ui tap was sent: do not '
                                             "send it again."), lines[-1])
        self.assertEqual(len(self.inputs()), 1)

    def test_an_error_stays_the_json_envelope_on_stderr(self) -> None:
        self.start("--profile", "agent")
        code, lines, stderr = self.text("ui", "tap", "--text", "Nowhere")
        self.assertEqual((code, lines), (2, []))
        self.assertEqual(json.loads(stderr)["error_code"], errors.NO_MATCHING_NODE)

    def test_the_wait_verbs_answer_in_one_line(self) -> None:
        session = self.start("--profile", "agent")
        code, lines, _ = self.text("ui", "wait", "--text", "General")
        self.assertEqual(code, 0, lines)
        self.assertRegex(lines[0], r'^wait met in \d+ ms: text "General" \(1 on screen\)$')
        self.assertEqual(len(lines), 1)
        code, lines, _ = self.text("ui", "wait", "--settled", "--quiet-ms", "0")
        self.assertRegex(lines[0], r"^settled in \d+ ms$")
        code, lines, _ = self.text("ui", "wait", "--text", "General", "--json")
        self.assertTrue(json.loads(lines[0])["met"])
        self.capture(session, flow("f_1", "/v1/orders"))
        code, lines, _ = self.text("network", "requests", "wait", "--path", "/v1/orders",
                                   "--since-id", "f_0", "--timeout-ms", "0")
        self.assertEqual(code, 0, lines)
        self.assertRegex(lines[0], r"^wait met in \d+ ms: request /v1/orders \(GET "
                                   r"api\.example/v1/orders 200 12 ms id f_1\)$")
        self.assertTrue(lines[1].startswith("! since_id_not_found:"))

    def test_ui_do_lists_its_steps(self) -> None:
        self.start("--profile", "agent")
        completed_code, stdout, _ = self.run_do('[{"assertVisible": {"text": "General"}}]')
        lines = stdout.splitlines()
        self.assertEqual(completed_code, 0, lines)
        self.assertEqual(lines[:2], ["ui do passed: 1 step(s)", "  1. assertVisible passed"])
        self.assertTrue(lines[2].startswith("screen: com.android.settings"))
        code, stdout, _ = self.run_do('[{"assertVisible": {"text": "General"}}]', "--json")
        self.assertEqual(json.loads(stdout)["status"], "passed")

    def run_do(self, steps: str, *flags: str) -> tuple[int, str, str]:
        import subprocess

        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/autonom.py"), "--adb", str(self.adb),
             "--serial", SERIAL, "ui", "do", *flags],
            cwd=self.root, env=self.env, text=True, input=steps, capture_output=True,
            check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed.returncode, completed.stdout, completed.stderr

    def test_the_first_line_says_when_the_fast_path_is_not_used(self) -> None:
        # auto, and no JDK to build the on-device helper with: uiautomator and adb
        self.env["PATH"] = str(self.bin)
        self.env["HOME"] = str(self.root / "user")
        for key in ("AUTONOM_ANDROID_UI_BACKEND", "JAVA_HOME", "ANDROID_HOME",
                    "ANDROID_SDK_ROOT"):
            self.env.pop(key, None)
        self.start("--profile", "agent")
        code, lines, _ = self.text("ui", "tap", "--text", "General")
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("! not on the fast path: javac not found. "),
                        lines[0])
        self.assertIn("JDK", lines[0])
        self.assertEqual(lines[1], "ui tap ok at 540,390 (n3)")
        code, lines, _ = self.text("ui", "tap", "--x", "10", "--y", "20", "--json")
        self.assertEqual(list(json.loads(lines[0]))[:3],
                         ["ok", "fallback_reason", "fallback_hint"])


class ShotCliTests(WaitCase):

    def setUp(self) -> None:
        super().setUp()
        self.update_state(after_input={"ui_dump": self.general})

    def screencaps(self) -> int:
        return sum(1 for argv in self.calls("adb") if "screencap" in argv)

    def test_the_action_takes_its_screenshot(self) -> None:
        session = self.start()
        code, payload, _ = self.android("ui", "tap", "--text", "General",
                                        "--shot", "general opened")
        self.assertEqual(code, 0, payload)
        shot = payload["shot"]
        self.assertEqual(shot["label"], "general opened")
        path = Path(shot["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.resolve(),
                         (Path(session["artifacts_dir"]) / "shots").resolve())
        self.assertIn("general-opened", path.name)
        self.assertEqual((len(self.inputs()), self.screencaps()), (1, 1))
        # filed like `autonom screenshot --label`: the shots index lists it
        code, listed, _ = self.android("shots", "list")
        self.assertEqual([entry["label"] for entry in listed["shots"]], ["general opened"])

    def test_it_is_taken_after_the_wait(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--until",
                                        "text=About", "--shot", "about")
        self.assertEqual(code, 0, payload)
        calls = self.calls("adb")
        capture = next(index for index, argv in enumerate(calls) if "screencap" in argv)
        last_dump = max(index for index, argv in enumerate(calls)
                        if argv[-3:] == ["uiautomator", "dump", "/dev/tty"])
        self.assertGreater(capture, last_dump, "the screen the wait ended on")

    def test_an_agent_session_names_the_file_in_one_line(self) -> None:
        self.start("--profile", "agent")
        code, stdout, _ = self.run_text("--adb", str(self.adb), "--serial", SERIAL, "ui", "key",
                                        "KEYCODE_BACK", "--shot", "back")
        self.assertEqual(code, 0, stdout)
        [line] = [line for line in stdout.splitlines() if line.startswith("shot: ")]
        self.assertTrue(Path(line[len("shot: "):]).is_file())

    def test_a_label_is_needed_before_anything_is_sent(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--shot", " ")
        self.assertEqual((code, payload["error_code"], payload["flag"]),
                         (2, errors.INVALID_VALUE, "--shot"))
        self.assertEqual(self.calls("adb"), [])

    def test_a_failed_capture_does_not_fail_the_action(self) -> None:
        self.start()
        self.update_state(fail={f"-s {SERIAL} exec-out screencap": [1, "error: closed"]})
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--shot", "broken")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertNotIn("shot", payload)
        self.assertEqual([item["code"] for item in payload["warnings"]], ["shot_failed"])
        self.assertEqual(len(self.inputs()), 1)

    def test_screenshot_itself_is_unchanged(self) -> None:
        self.start()
        code, payload, _ = self.android("screenshot", "--label", "plain")
        self.assertEqual(code, 0, payload)
        self.assertEqual(list(payload)[:3], ["ok", "path", "copies"])
        self.assertTrue(payload["path"].endswith("_plain.png"))
        self.assertEqual(payload["target_id"], SERIAL)


if __name__ == "__main__":
    unittest.main()


# --- another tool holds UiAutomation (the on-device helper cannot start) -------------------

try:
    from test_android_helper_backend import BackendCase  # noqa: E402
except ImportError:  # direct `python3 -m unittest tests.test_brief` runs
    from tests.test_android_helper_backend import BackendCase  # noqa: E402


class BusyUiAutomationTests(BackendCase):
    """Appium, uiautomator2 or an instrumentation test holds UiAutomation: the
    helper answers `uiautomation_busy`, and auto falls back to uiautomator."""

    def setUp(self) -> None:
        super().setUp()
        self.helper.busy_hellos = 1

    def test_a_reply_names_the_cause_right_after_ok(self) -> None:
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(list(payload)[:3], ["ok", "fallback_reason", "fallback_hint"])
        self.assertIn("uiautomation_busy", payload["fallback_reason"])
        self.assertIn("UiAutomation", payload["fallback_hint"])
        self.assertTrue(brief.tree(payload).startswith("! not on the fast path: "))

    def test_a_failure_of_the_slow_path_names_the_cause_too(self) -> None:
        # the dump uiautomator makes instead fails as well: the envelope must
        # not leave "incomplete hierarchy" as the only explanation
        self.update_device(ui_dump_incomplete=50)
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 2, payload)
        self.assertEqual(list(payload)[:3], ["ok", "fallback_reason", "fallback_hint"])
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error_code"], errors.BACKEND_FAILED)
        self.assertIn("uiautomation_busy", payload["fallback_reason"])
        self.assertIn("stop it, or wait", payload["fallback_hint"])
        lines = brief.failure(payload).splitlines()
        self.assertTrue(lines[0].startswith("! not on the fast path: uiautomation_busy"), lines)
        self.assertTrue(lines[1].startswith("error backend_failed: "), lines)

    def test_the_explicit_uiautomator_choice_keeps_todays_envelope(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        self.update_device(ui_dump_incomplete=50)
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 2, payload)
        self.assertNotIn("fallback_reason", payload)
        self.assertEqual(list(payload)[:3], ["ok", "error_code", "error"])
