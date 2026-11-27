"""Waiting inside a call: `ui wait` by selector and `--gone`, `network
requests wait`, and `--until` on the action verbs.

The library (`autonom_lib.wait`, `network.store.Tail`) is tested with a fake
clock. The CLI cases run the real CLI in a subprocess against `fake_adb.py`
behind a wrapper that changes the screen the fake reports after a number of
reads or once an input was sent, and that can append a record to a flow store
when the input goes out: the fake call log then proves how many inputs were
sent (a wait sends none) and the reply proves what the wait saw. No real
device, no real proxy.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, wait  # noqa: E402
from autonom_lib.network import store  # noqa: E402

try:
    from test_observe import (SERIAL, SETTINGS_GENERAL, SETTINGS_MAIN,  # noqa: E402
                              ObserveCase, android_dump)
except ImportError:  # direct `python3 -m unittest tests.test_wait` runs
    from tests.test_observe import (SERIAL, SETTINGS_GENERAL, SETTINGS_MAIN,  # noqa: E402
                                    ObserveCase, android_dump)


def node(text: str, *, top: int = 100, desc: str = "", rid: str = "") -> dict[str, Any]:
    return {"role": "button", "text": text, "desc": desc, "resource_id": rid,
            "bounds": [40, top, 1040, top + 100], "visible": True}


SCREEN = {"role": "frame", "text": "", "desc": "", "resource_id": "",
          "bounds": [0, 0, 1080, 1920], "visible": True}


class Clock:
    """A clock that only moves when the poll sleeps or a read takes time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- --until ------------------------------------------------------------------------------


class ParseUntilTests(unittest.TestCase):

    def test_a_node_on_screen(self) -> None:
        for spec, selector in (("text=Orders", {"text": "Orders"}),
                               ("desc=Save", {"desc": "Save"}),
                               ("id=com.app:id/list", {"resource_id": "com.app:id/list"})):
            with self.subTest(spec=spec):
                condition = wait.parse_until(spec)
                self.assertEqual((condition.kind, condition.selector), (wait.VISIBLE, selector))
                self.assertEqual((condition.mode, condition.case_sensitive), ("contains", False))

    def test_gone(self) -> None:
        condition = wait.parse_until("gone:text=Loading")
        self.assertEqual((condition.kind, condition.selector), (wait.GONE, {"text": "Loading"}))
        self.assertEqual(condition.label(), 'gone: text "Loading"')

    def test_a_request(self) -> None:
        condition = wait.parse_until("request=*/v1/orders*")
        self.assertEqual((condition.kind, condition.filters),
                         (wait.REQUEST, {"path_glob": "*/v1/orders*"}))
        self.assertEqual(condition.describe(), {"wait": "request", "path": "*/v1/orders*"})
        self.assertEqual(condition.label(), "request */v1/orders*")

    def test_a_value_keeps_its_equals_signs(self) -> None:
        self.assertEqual(wait.parse_until("text=a=b").selector, {"text": "a=b"})

    def test_anything_else_is_invalid_value(self) -> None:
        for spec in ("Orders", "text=", "label=Orders", "gone:request=*/x", "gone:", "=x"):
            with self.subTest(spec=spec):
                with self.assertRaises(errors.AutonomError) as caught:
                    wait.parse_until(spec)
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
                self.assertIn("request=GLOB", caught.exception.hint)


# --- the poll -----------------------------------------------------------------------------


class PollTests(unittest.TestCase):

    def setUp(self) -> None:
        self.clock = Clock()

    def poll(self, conditions: list, screens: list, *, timeout_ms: int = 5000,
             requests: list | None = None) -> tuple[dict[str, Any], int]:
        reads = {"count": 0}

        def read() -> list:
            index = min(reads["count"], len(screens) - 1)
            reads["count"] += 1
            screen = screens[index]
            if isinstance(screen, Exception):
                raise screen
            return [SCREEN, *screen]

        batches = list(requests or [])
        result = wait.poll(conditions, timeout_ms=timeout_ms, read_nodes=read,
                           fresh_requests=lambda: batches.pop(0) if batches else [],
                           clock=self.clock, sleep=self.clock.sleep)
        return result, reads["count"]

    def test_a_node_that_is_there_needs_one_read_and_no_sleep(self) -> None:
        result, reads = self.poll([wait.element({"text": "Orders"})], [[node("Orders")]])
        self.assertTrue(result["met"])
        self.assertEqual((reads, result["reads"], self.clock.sleeps), (1, 1, []))
        self.assertEqual(result["conditions"],
                         [{"wait": "visible", "selector": {"text": "Orders"}, "met": True,
                           "count": 1}])

    def test_it_polls_until_the_node_comes(self) -> None:
        result, reads = self.poll([wait.element({"text": "Orders"})],
                                  [[node("Loading")], [node("Loading")], [node("Orders")]])
        self.assertTrue(result["met"])
        self.assertEqual(reads, 3)
        self.assertEqual(result["elapsed_ms"], 400)

    def test_gone_waits_until_no_match_is_on_screen(self) -> None:
        result, reads = self.poll([wait.element({"text": "Loading"}, gone=True)],
                                  [[node("Loading")], [node("Orders")]])
        self.assertTrue(result["met"])
        self.assertEqual((reads, result["conditions"][0]["count"]), (2, 0))

    def test_an_off_screen_node_is_not_on_screen(self) -> None:
        below = {**node("Orders", top=4000), "visible": False}
        result, _ = self.poll([wait.element({"text": "Orders"})], [[below]], timeout_ms=0)
        self.assertFalse(result["met"])
        gone, _ = self.poll([wait.element({"text": "Orders"}, gone=True)], [[below]],
                            timeout_ms=0)
        self.assertTrue(gone["met"])

    def test_unmet_ends_at_the_deadline(self) -> None:
        result, reads = self.poll([wait.element({"text": "Orders"})], [[node("Loading")]],
                                  timeout_ms=500)
        self.assertFalse(result["met"])
        self.assertEqual(result["elapsed_ms"], 500, "never sleeps past the deadline")
        self.assertEqual(reads, 4, "0, 200, 400 and once more at the deadline")
        self.assertEqual(result["conditions"][0]["count"], 0)

    def test_a_zero_budget_checks_once(self) -> None:
        result, reads = self.poll([wait.element({"text": "Orders"})], [[node("Loading")]],
                                  timeout_ms=0)
        self.assertEqual((result["met"], reads, self.clock.sleeps), (False, 1, []))

    def test_a_read_of_a_screen_in_transition_is_read_again(self) -> None:
        busy = errors.AutonomError(errors.UI_NOT_IDLE, "could not get idle state")
        result, reads = self.poll([wait.element({"text": "Orders"})],
                                  [busy, busy, [node("Orders")]])
        self.assertTrue(result["met"])
        self.assertEqual(reads, 3)
        self.assertNotIn("error", result)

    def test_a_read_that_keeps_failing_is_reported_not_raised(self) -> None:
        busy = errors.AutonomError(errors.BACKEND_FAILED, "dump was incomplete")
        result, _ = self.poll([wait.element({"text": "Orders"})], [busy], timeout_ms=300)
        self.assertFalse(result["met"])
        self.assertEqual(result["error"], {"code": errors.BACKEND_FAILED,
                                           "message": "dump was incomplete"})
        self.assertIsNone(result["conditions"][0]["count"])

    def test_any_other_failure_is_the_callers(self) -> None:
        gone = errors.AutonomError(errors.NO_TARGET, "device offline")
        with self.assertRaises(errors.AutonomError) as caught:
            self.poll([wait.element({"text": "Orders"})], [gone])
        self.assertEqual(caught.exception.code, errors.NO_TARGET)

    def test_a_request_condition_reads_no_tree(self) -> None:
        orders = {"id": "f_2", "method": "GET", "host": "api.example", "path": "/v1/orders",
                  "url": "https://api.example/v1/orders?page=1", "status": 200,
                  "mocked": False, "duration_ms": 31}
        other = {"id": "f_1", "method": "GET", "host": "api.example", "path": "/v1/me",
                 "url": "https://api.example/v1/me", "status": 200, "mocked": False}
        result, reads = self.poll([wait.request(path_glob="*/v1/orders*")], [[]],
                                  requests=[[], [other], [orders]])
        self.assertTrue(result["met"])
        self.assertEqual((reads, result["reads"]), (0, 0))
        self.assertEqual(result["conditions"][0]["request"],
                         {"id": "f_2", "method": "GET", "host": "api.example",
                          "path": "/v1/orders", "status": 200, "mocked": False,
                          "duration_ms": 31})

    def test_all_conditions_must_hold_and_a_request_stays_met(self) -> None:
        orders = {"id": "f_1", "method": "GET", "path": "/v1/orders", "status": 200}
        result, reads = self.poll(
            [wait.request(path_glob="/v1/orders"), wait.element({"text": "Orders"})],
            [[node("Loading")], [node("Loading")], [node("Orders")]],
            requests=[[orders]])
        self.assertTrue(result["met"])
        self.assertEqual(reads, 3, "the request came first; the node two reads later")
        self.assertEqual([item["met"] for item in result["conditions"]], [True, True])

    def test_an_incomplete_request_says_so(self) -> None:
        aborted = {"id": "f_1", "method": "GET", "path": "/v1/orders", "status": None,
                   "completed": False}
        result, _ = self.poll([wait.request(path_glob="/v1/orders")], [[]],
                              requests=[[aborted]])
        self.assertEqual(result["conditions"][0]["request"],
                         {"id": "f_1", "method": "GET", "path": "/v1/orders",
                          "completed": False})

    def test_index_narrows_to_that_occurrence(self) -> None:
        two = [node("Item", top=100), node("Item", top=300)]
        second, _ = self.poll([wait.element({"text": "Item"}, index=1)], [two], timeout_ms=0)
        third, _ = self.poll([wait.element({"text": "Item"}, index=2)], [two], timeout_ms=0)
        self.assertEqual((second["met"], third["met"]), (True, False))

    def test_the_unmet_message_names_what_did_not_hold(self) -> None:
        conditions = [wait.element({"text": "Orders"}), wait.request(path_glob="*/orders")]
        result, _ = self.poll(conditions, [[node("Orders")]], timeout_ms=0)
        error, hint = wait.unmet_message(conditions, result, "--until-timeout-ms")
        self.assertEqual(error, "not met within 0 ms: request */orders")
        self.assertIn("network status", hint)
        self.assertIn("--until-timeout-ms", hint)
        self.assertNotIn("ui tree", hint, "the node condition held")


# --- the flow store tail --------------------------------------------------------------------


class TailTests(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = {"artifacts_dir": tmp.name}
        self.path = store.flows_path(self.record)
        self.path.parent.mkdir(parents=True)

    def append(self, *rows: Any) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")

    def ids(self, flows: list) -> list:
        return [flow["id"] for flow in flows]

    def test_only_what_was_appended_each_once(self) -> None:
        tail = store.Tail(self.record)
        self.assertEqual(tail.fresh(), [], "no store yet")
        self.append({"id": "f_1"}, {"id": "f_2"})
        self.assertEqual(self.ids(tail.fresh()), ["f_1", "f_2"])
        self.assertEqual(tail.fresh(), [])
        self.append({"id": "f_3"})
        self.assertEqual(self.ids(tail.fresh()), ["f_3"])

    def test_skip_recorded_is_the_baseline(self) -> None:
        self.append({"id": "f_1"}, {"id": "f_2"})
        tail = store.Tail(self.record)
        self.assertEqual(tail.skip_recorded(), "f_2")
        self.append({"id": "f_3"})
        self.assertEqual(self.ids(tail.fresh()), ["f_3"])

    def test_skip_through_keeps_what_came_after_the_cursor(self) -> None:
        self.append({"id": "f_1"}, {"id": "f_2"}, {"id": "f_3"})
        tail = store.Tail(self.record)
        self.assertTrue(tail.skip_through("f_1"))
        self.append({"id": "f_4"})
        self.assertEqual(self.ids(tail.fresh()), ["f_2", "f_3", "f_4"])

    def test_an_unknown_cursor_takes_nothing_as_seen(self) -> None:
        self.append({"id": "f_1"}, {"id": "f_2"})
        tail = store.Tail(self.record)
        self.assertFalse(tail.skip_through("f_9"))
        self.assertEqual(self.ids(tail.fresh()), ["f_1", "f_2"])

    def test_a_half_written_line_waits_for_its_end(self) -> None:
        tail = store.Tail(self.record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "f_1"')
        self.assertEqual(tail.fresh(), [])
        self.append("}")
        self.assertEqual(self.ids(tail.fresh()), ["f_1"])

    def test_lines_that_are_not_records_are_counted(self) -> None:
        tail = store.Tail(self.record)
        self.append("not json", "[1]", {"id": ["x"]}, {"id": "f_1"})
        self.assertEqual(self.ids(tail.fresh()), ["f_1"])
        self.assertEqual(tail.skipped, 3)

    def test_a_replaced_store_is_read_from_its_start(self) -> None:
        self.append({"id": "f_1"}, {"id": "f_2"})
        tail = store.Tail(self.record)
        tail.skip_recorded()
        self.path.write_text(json.dumps({"id": "f_9"}) + "\n", encoding="utf-8")
        self.assertEqual(self.ids(tail.fresh()), ["f_9"])


# --- the CLI ----------------------------------------------------------------------------------

# The fake behind a wrapper that changes the screen: `after_dumps` (state keys
# to apply) once `dumps_left` more `uiautomator dump` calls were made, and
# `after_input` once the first input was sent; `append_on_input` (`file`,
# `line`) appends a flow record when that input goes out.
WRAPPER = '''#!{python}
import json, os, runpy, sys

FAKE = {fake!r}
PATH = os.environ["AUTONOM_FAKE_STATE"]


def load():
    with open(PATH, encoding="utf-8") as handle:
        return json.load(handle)


def save(state):
    with open(PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


argv = sys.argv[1:]
is_input = "shell" in argv and argv[argv.index("shell") + 1:argv.index("shell") + 2] == ["input"]
is_dump = argv[-3:] == ["uiautomator", "dump", "/dev/tty"]
try:
    sys.argv = [FAKE, *argv]
    runpy.run_path(FAKE, run_name="__main__")
finally:
    state = load()
    if is_input:
        state.update(state.pop("after_input", None) or {{}})
        extra = state.pop("append_on_input", None)
        if extra:
            with open(extra["file"], "a", encoding="utf-8") as handle:
                handle.write(extra["line"] + "\\n")
        save(state)
    elif is_dump and "after_dumps" in state:
        state["dumps_left"] = int(state.get("dumps_left", 0)) - 1
        if state["dumps_left"] <= 0:
            state.update(state.pop("after_dumps"))
        save(state)
'''


class WaitCase(ObserveCase):
    """Android Settings on the fake: the main screen, then General."""

    def setUp(self) -> None:
        super().setUp()
        self.main = self.screen("main.xml", android_dump("com.android.settings", SETTINGS_MAIN))
        self.general = self.screen("general.xml",
                                   android_dump("com.android.settings", SETTINGS_GENERAL))
        self.write_state({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "ui_dump": self.main, "foreground_app": "com.android.settings"})

    def wrapper(self, tool: str, fake: Path) -> Path:
        path = self.root / f"wrapped_{tool}.py"
        path.write_text(WRAPPER.format(python=sys.executable, fake=str(fake)),
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def update_state(self, **keys: Any) -> None:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update(keys)
        self.write_state(state)

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
        return sum(1 for argv in self.calls("adb")
                   if argv[-3:] == ["uiautomator", "dump", "/dev/tty"])

    def capture(self, session: dict[str, Any], *flows: dict[str, Any]) -> Path:
        """A proxy that looks alive for the session, and its flow store."""
        network = Path(session["artifacts_dir"]) / "network"
        network.mkdir(parents=True, exist_ok=True)
        (network / "proxy.json").write_text(
            json.dumps({"pid": os.getpid(), "port": 18080}), encoding="utf-8")
        flows_file = network / "flows.jsonl"
        flows_file.write_text("".join(json.dumps(flow) + "\n" for flow in flows),
                              encoding="utf-8")
        return flows_file


def flow(identifier: str, path: str, **extra: Any) -> dict[str, Any]:
    return {"id": identifier, "completed": True, "method": "GET", "host": "api.example",
            "path": path, "url": f"https://api.example{path}", "status": 200,
            "duration_ms": 12, "mocked": False, "started_at": "2026-11-27T10:00:00Z",
            **extra}


class UiWaitCliTests(WaitCase):

    def test_a_node_that_is_on_screen(self) -> None:
        self.start()
        code, payload, stdout = self.android("ui", "wait", "--text", "General")
        self.assertEqual(code, 0, payload)
        self.assertEqual(
            {key: payload[key] for key in ("ok", "wait", "selector", "met", "count", "reads")},
            {"ok": True, "wait": "visible", "selector": {"text": "General"}, "met": True,
             "count": 1, "reads": 1})
        self.assertEqual((payload["timeout_ms"], payload["target_id"]), (5000, SERIAL))
        self.assertNotIn("warnings", payload)
        self.assertEqual(self.inputs(), [], "a wait sends nothing")
        self.assertEqual(stdout.count("\n"), 1)

    def test_it_waits_for_the_node_to_come(self) -> None:
        self.start()
        self.update_state(dumps_left=2, after_dumps={"ui_dump": self.general})
        # a long budget: on a loaded machine one read of the fake can take seconds
        code, payload, _ = self.android("ui", "wait", "--text", "About", "--timeout-ms", "90000")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["met"])
        self.assertEqual((payload["reads"], self.dumps()), (3, 3))

    def test_gone_waits_for_the_node_to_leave(self) -> None:
        self.start()
        self.update_state(dumps_left=1, after_dumps={"ui_dump": self.general})
        code, payload, _ = self.android("ui", "wait", "--text", "Display", "--gone",
                                        "--timeout-ms", "90000")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["wait"], payload["met"], payload["count"]), ("gone", True, 0))
        self.assertEqual(payload["reads"], 2)

    def test_unmet_is_reported_and_exits_1(self) -> None:
        self.start()
        code, payload, stdout = self.android("ui", "wait", "--text", "About",
                                             "--timeout-ms", "300")
        self.assertEqual(code, 1, payload)
        self.assertTrue(stdout.strip(), "the report is on stdout")
        self.assertEqual((payload["ok"], payload["met"], payload["count"]), (False, False, 0))
        [warning] = payload["warnings"]
        self.assertEqual(warning["code"], "wait_not_met")
        self.assertIn('text "About"', warning["error"])
        self.assertIn("--timeout-ms", warning["hint"])
        self.assertEqual(self.inputs(), [])

    def test_exact_mode_and_the_other_selector_flags_apply(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "wait", "--text", "Gener", "--mode", "exact",
                                        "--timeout-ms", "0")
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["mode"], "exact")
        code, payload, _ = self.android("ui", "wait", "--resource-id", "android:id/title",
                                        "--timeout-ms", "0")
        self.assertEqual((code, payload["selector"]),
                         (0, {"resource_id": "android:id/title"}))

    def test_a_malformed_regex_is_refused_before_the_device(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "wait", "--text", "(", "--mode", "regex")
        self.assertEqual((code, payload["error_code"]), (2, errors.INVALID_VALUE))
        self.assertEqual(self.dumps(), 0)

    def test_one_condition_and_only_one(self) -> None:
        self.start()
        for argv, word in ((("ui", "wait"), "needs a condition"),
                           (("ui", "wait", "--settled", "--text", "General"), "one condition"),
                           (("ui", "wait", "--gone"), "needs a condition"),
                           (("ui", "wait", "--settled", "--gone"), "--gone needs a selector")):
            with self.subTest(argv=argv):
                code, payload, _ = self.android(*argv)
                self.assertEqual((code, payload["error_code"]), (2, errors.USAGE_ERROR))
                self.assertIn(word, payload["error"])
        self.assertEqual(self.calls("adb"), [], "refused before any device call")

    def test_settled_still_waits_for_a_still_screen(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "wait", "--settled", "--quiet-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["settled"])

    def test_timeout_is_refused_not_read_as_milliseconds(self) -> None:
        self.start()
        for argv in (("ui", "wait", "--settled", "--timeout", "40"),
                     ("ui", "wait", "--text", "General", "--timeout=40")):
            with self.subTest(argv=argv):
                code, payload, _ = self.android(*argv)
                self.assertEqual((code, payload["error_code"]), (2, errors.USAGE_ERROR))
                self.assertIn("--timeout-ms is spelled in full, in milliseconds",
                              payload["error"])
                self.assertIn("usage: autonom ui wait", payload["hint"])
        self.assertEqual(self.calls("adb"), [])

    def test_the_older_abbreviations_still_resolve(self) -> None:
        self.start()
        # --quiet is --quiet-ms and --ser is --serial, as before the selector flags
        code, payload, _ = self.run_cli("--adb", str(self.adb), "ui", "wait", "--settled",
                                        "--quiet", "0", "--ser", SERIAL)
        self.assertEqual((code, payload["quiet_ms"]), (0, 0), payload)
        code, payload, _ = self.android("ui", "wait", "--te", "General")
        self.assertEqual((code, payload["error_code"]), (2, errors.USAGE_ERROR))


class UntilCliTests(WaitCase):

    def setUp(self) -> None:
        super().setUp()
        self.update_state(after_input={"ui_dump": self.general})

    def test_the_tap_waits_for_what_it_leads_to(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "tap", "--text", "General",
                                        "--until", "text=About", "--until", "gone:text=Display")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.inputs(), [["input", "tap", "540", "390"]], "exactly one tap")
        until = payload["until"]
        self.assertTrue(until["met"])
        self.assertEqual(until["timeout_ms"], wait.DEFAULT_UNTIL_TIMEOUT_MS)
        self.assertEqual(until["conditions"], [
            {"wait": "visible", "selector": {"text": "About"}, "met": True, "count": 1},
            {"wait": "gone", "selector": {"text": "Display"}, "met": True, "count": 0}])
        self.assertNotIn("screen", payload, "a default session does not observe")
        self.assertNotIn("warnings", payload)

    def test_unmet_keeps_ok_and_exits_1(self) -> None:
        self.start()
        code, payload, stdout = self.android("ui", "tap", "--text", "General", "--until",
                                             "text=Wi-Fi", "--until-timeout-ms", "300")
        self.assertEqual(code, 1, payload)
        self.assertTrue(stdout.strip())
        self.assertTrue(payload["ok"], "the input was sent")
        self.assertFalse(payload["until"]["met"])
        self.assertEqual(len(self.inputs()), 1, "and only once")
        [warning] = payload["warnings"]
        self.assertEqual(warning["code"], "until_not_met")
        self.assertIn('text "Wi-Fi"', warning["error"])
        self.assertIn("do not send it again", warning["hint"])
        self.assertIn("--until-timeout-ms", warning["hint"])

    def test_every_action_verb_takes_it(self) -> None:
        self.start()
        for argv in (("ui", "tap", "--x", "100", "--y", "200"),
                     ("ui", "swipe", "--from", "100,900", "--to", "100,300"),
                     ("ui", "type", "hello"),
                     ("ui", "key", "KEYCODE_BACK")):
            with self.subTest(argv=argv):
                self.forget_calls()
                code, payload, _ = self.android(*argv, "--until", "text=General",
                                                "--until-timeout-ms", "0")
                self.assertEqual(code, 0, payload)
                self.assertTrue(payload["until"]["met"])
                self.assertEqual(len(self.inputs()), 1, self.inputs())

    def test_a_bad_condition_sends_nothing(self) -> None:
        self.start()
        for spec in ("About", "label=About", "gone:request=*/x"):
            with self.subTest(spec=spec):
                code, payload, _ = self.android("ui", "tap", "--text", "General",
                                                "--until", spec)
                self.assertEqual((code, payload["error_code"]), (2, errors.INVALID_VALUE))
                self.assertEqual(payload["flag"], "--until")
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--until",
                                        "text=About", "--until-timeout-ms", "-1")
        self.assertEqual((code, payload["error_code"]), (2, errors.INVALID_VALUE))
        self.assertEqual(self.calls("adb"), [], "refused before any device call")

    def test_the_new_flags_are_spelled_in_full_only(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--unt",
                                        "text=About")
        self.assertEqual((code, payload["error_code"]), (2, errors.USAGE_ERROR))
        self.assertEqual(self.inputs(), [])

    def test_a_request_condition_needs_a_running_proxy(self) -> None:
        self.start()
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--until",
                                        "request=*/orders*")
        self.assertEqual((code, payload["error_code"]), (2, errors.PROXY_NOT_RUNNING))
        self.assertIn("network start", payload["hint"])
        self.assertEqual(self.inputs(), [], "a wait that could never be met sends nothing")

    def test_a_request_sent_by_the_tap_is_not_missed(self) -> None:
        session = self.start()
        flows_file = self.capture(session, flow("f_1", "/v1/orders"))
        self.update_state(append_on_input={
            "file": str(flows_file), "line": json.dumps(flow("f_2", "/v1/orders"))})
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--until",
                                        "request=*/v1/orders*", "--until", "text=About")
        self.assertEqual(code, 0, payload)
        request, node_wait = payload["until"]["conditions"]
        self.assertEqual(request["request"]["id"], "f_2",
                         "the request recorded before the tap does not count")
        self.assertEqual((request["path"], request["met"], node_wait["met"]),
                         ("*/v1/orders*", True, True))

    def test_no_request_is_unmet_and_names_the_capture(self) -> None:
        session = self.start()
        self.capture(session, flow("f_1", "/v1/orders"))
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--until",
                                        "request=*/v1/orders*", "--until-timeout-ms", "300")
        self.assertEqual(code, 1, payload)
        self.assertEqual(len(self.inputs()), 1)
        self.assertIn("network status", payload["warnings"][0]["hint"])

    def test_an_agent_session_reports_the_screen_the_wait_ended_on(self) -> None:
        self.start("--profile", "agent")
        code, payload, _ = self.android("ui", "tap", "--text", "General", "--until",
                                        "text=About", "--json")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["until"]["met"])
        self.assertIn('"About"', payload["screen"]["outline"])
        self.assertEqual(payload["changes_baseline"], "pre_read")
        self.assertEqual((payload["changes"]["added"], payload["changes"]["removed"]), (3, 4))
        self.assertEqual(len(self.inputs()), 1)


class RequestsWaitCliTests(WaitCase):

    def test_it_needs_a_session_with_a_running_proxy(self) -> None:
        code, payload, _ = self.android("network", "requests", "wait", "--path", "*/orders*")
        self.assertEqual((code, payload["error_code"]), (2, errors.NO_ACTIVE_SESSION))
        self.start()
        code, payload, _ = self.android("network", "requests", "wait", "--path", "*/orders*")
        self.assertEqual((code, payload["error_code"]), (2, errors.PROXY_NOT_RUNNING))

    def test_it_waits_for_a_request_recorded_after_it_began(self) -> None:
        session = self.start()
        flows_file = self.capture(session, flow("f_1", "/v1/orders", method="POST",
                                                status=201))
        done = threading.Event()

        def keep_recording() -> None:
            # the app goes on talking: another request every quarter second,
            # so one is recorded after the wait began however slow its start
            number = 1
            while not done.wait(0.25):
                number += 1
                with flows_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(flow(f"f_{number}a", "/v1/me")) + "\n")
                    handle.write(json.dumps(flow(f"f_{number}", "/v1/orders", method="POST",
                                                 status=201)) + "\n")

        writer = threading.Thread(target=keep_recording)
        writer.start()
        self.addCleanup(writer.join)
        self.addCleanup(done.set)
        code, payload, stdout = self.android("network", "requests", "wait", "--path",
                                             "*/v1/orders*", "--method", "post")
        done.set()
        self.assertEqual(code, 0, payload)
        self.assertEqual(
            {key: payload[key] for key in ("ok", "wait", "path", "method", "met")},
            {"ok": True, "wait": "request", "path": "*/v1/orders*", "method": "post",
             "met": True})
        request = payload["request"]
        self.assertNotEqual(request.pop("id"), "f_1",
                            "the request recorded before the wait began does not count")
        self.assertEqual(request, {"method": "POST", "host": "api.example",
                                   "path": "/v1/orders", "status": 201, "mocked": False,
                                   "duration_ms": 12})
        self.assertNotIn("reads", payload, "no tree is read")
        self.assertEqual(self.calls("adb"), [], "and no device call is made")
        self.assertEqual(stdout.count("\n"), 1)

    def test_since_id_counts_what_came_after_the_cursor(self) -> None:
        session = self.start()
        self.capture(session, flow("f_1", "/v1/orders"), flow("f_2", "/v1/orders"))
        code, payload, _ = self.android("network", "requests", "wait", "--path", "/v1/orders",
                                        "--since-id", "f_1", "--timeout-ms", "0")
        self.assertEqual((code, payload["request"]["id"]), (0, "f_2"), payload)
        code, payload, _ = self.android("network", "requests", "wait", "--path", "/v1/orders",
                                        "--since-id", "f_2", "--timeout-ms", "0")
        self.assertEqual((code, payload["met"]), (1, False), payload)

    def test_an_unknown_cursor_is_warned_about(self) -> None:
        session = self.start()
        self.capture(session, flow("f_1", "/v1/orders"))
        code, payload, _ = self.android("network", "requests", "wait", "--path", "/v1/orders",
                                        "--since-id", "f_9", "--timeout-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["code"] for item in payload["warnings"]], ["since_id_not_found"])

    def test_unmet_is_reported_and_exits_1(self) -> None:
        session = self.start()
        self.capture(session, flow("f_1", "/v1/orders"))
        code, payload, _ = self.android("network", "requests", "wait", "--path", "/v1/orders",
                                        "--status", "200", "--timeout-ms", "300")
        self.assertEqual(code, 1, payload)
        self.assertEqual((payload["ok"], payload["met"], payload["status"]), (False, False, 200))
        [warning] = payload["warnings"]
        self.assertEqual(warning["code"], "wait_not_met")
        self.assertIn("request /v1/orders status 200", warning["error"])

    def test_timeout_is_refused_here_too(self) -> None:
        session = self.start()
        self.capture(session)
        code, payload, _ = self.android("network", "requests", "wait", "--timeout", "5")
        self.assertEqual((code, payload["error_code"]), (2, errors.USAGE_ERROR))
        self.assertIn("--timeout-ms is spelled in full, in milliseconds", payload["error"])


if __name__ == "__main__":
    unittest.main()


# --- through the Android on-device helper -----------------------------------------------------

try:
    from test_android_helper_backend import NETWORK_XML, BackendCase  # noqa: E402
except ImportError:  # direct `python3 -m unittest tests.test_wait` runs
    from tests.test_android_helper_backend import NETWORK_XML, BackendCase  # noqa: E402


class ThroughTheHelperTests(BackendCase):
    """The fast path: reads and inputs served by the (fake) on-device helper."""

    def test_a_poll_read_does_not_wait_for_a_quiet_screen(self) -> None:
        code, payload = self.cli("ui", "wait", "--text", "Network & internet")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["met"])
        [read] = self.ops("read")
        self.assertEqual((read["quiet_ms"], read["max_ms"]), (500, 0),
                         "a spinner that never rests must not hold each read for 2 s")
        self.assertEqual(self.inputs(), [])

    def test_until_waits_after_the_input_and_observe_reads_after_the_wait(self) -> None:
        self.helper.xml_after = NETWORK_XML
        code, payload = self.cli("ui", "tap", "--text", "Network & internet", "--observe",
                                 "--until", "text=Airplane mode")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["until"]["met"])
        ops = self.ops()
        self.assertEqual([item["op"] for item in ops], ["read", "tap", "read", "read"],
                         "the tap's own read, the tap, the wait's read, observe's read")
        tap = ops[1]
        self.assertFalse(tap.get("read_after"), "the input does not settle or read: the wait "
                                                "decides when the screen is ready")
        self.assertEqual(ops[2]["max_ms"], 0, "the wait's read does not wait for quiet")
        self.assertGreater(ops[3]["max_ms"], 0, "observe's read settles within its budget")
        self.assertIn('"Airplane mode"', payload["screen"]["outline"])
        self.assertEqual(payload["changes_baseline"], "pre_read")
        self.assertGreater(payload["changes"]["added"], 0)
        self.assertEqual(self.inputs(), [], "nothing went through adb input")

    def test_without_until_an_observed_tap_is_still_one_round_trip(self) -> None:
        self.helper.xml_after = NETWORK_XML
        code, payload = self.cli("ui", "tap", "--text", "Network & internet", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["op"] for item in self.ops()], ["read", "tap"])
        self.assertIs(self.ops()[-1]["read_after"], True)
