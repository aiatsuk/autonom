"""Android reads: never-idle screens, cut-off dumps, state in the outline.

AL-05 — `uiautomator dump` prints "ERROR: could not get idle state." (exit 0,
no XML) on a screen that keeps changing. A tree read keeps the retry it
always had for any read without `</hierarchy>` (read, 0.5 s, read again; the
second read always happens, however slow the reads are) and gets at most one
more read, only when that read can start within 2 s of the end of the first
bad read. When the last read still says "could not get idle state" it fails
with `ui_not_idle` (a hint that names `autonom screenshot`) instead of
`backend_failed` "mid-transition"; a last read that is cut off fails as
`backend_failed`, as before. Flow handles `ui_not_idle` exactly where it
handled `backend_failed` (the `inputText` focus poll) and nowhere else, so
flows behave as before (DEC-007, INV-4): with one idle-state dump, each
command below makes as many dumps as at the base commit.

AL-06 — outline lines say `checked`/`unchecked` for every checkable node
(an off switch used to print nothing), and `editable`/`password` for text
fields; compact nodes carry `editable` and `password` on both platforms.

Hermetic: the CLI runs against `tests/fakes/fake_adb.py`, the dump and the
executor run in-process with injected clocks. No device is contacted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
CHECKABLE = ROOT / "tests/fixtures/ui_dump_checkable.xml"
IOS_TEXTFIELD = ROOT / "tests/fixtures/idb_describe_all_textfield.json"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors, ui, ui_android, ui_ios  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

NOT_IDLE_OUTPUT = "ERROR: could not get idle state.\n"
COMPLETE = UI_FIXTURE.read_text(encoding="utf-8")
CUT = COMPLETE[: len(COMPLETE) // 2]


class _Cli(EnvSandboxMixin, unittest.TestCase):
    """`autonom` against the fake adb, in a scratch AUTONOM_HOME."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_HOME=str(self.root / "home"),
                     AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None)

    def set_state(self, **values) -> None:
        self.state.write_text(json.dumps(values), encoding="utf-8")

    def dumps(self) -> int:
        if not self.log.exists():
            return 0
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return sum(1 for row in rows if row["tool"] == "adb" and "uiautomator" in row["argv"])

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], capture_output=True, text=True,
            env=dict(os.environ), timeout=120, cwd=str(self.root))
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def android(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", "emulator-5554", *argv)


def _unreleased_changelog() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]


# --- AL-05: never-idle screens ------------------------------------------------


class NotIdleCliTests(_Cli):
    def test_idle_state_error_is_ui_not_idle(self) -> None:
        """AL-05-S01: exit 2, `ui_not_idle`, the hint names `autonom screenshot`."""
        self.set_state(ui_dump=str(UI_FIXTURE), ui_dump_not_idle=5)
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 2, payload)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error_code"], errors.UI_NOT_IDLE)
        self.assertEqual(payload["error_code"], "ui_not_idle")
        self.assertIn("autonom screenshot", payload["hint"])
        self.assertIn("ui wait --settled", payload["hint"])
        self.assertIn("does not help", payload["hint"])
        for word in ("animation", "video", "timer", "spinner"):
            self.assertIn(word, payload["error"])
        text = (payload["error"] + " " + payload["hint"]).lower()
        self.assertNotIn("mid-transition", text)
        self.assertNotIn("incomplete", text)
        # read again like any bad read: the second read always, a third only
        # when it can start inside the 2 s window (the fake adb is quick, but
        # a loaded machine can make a read slow)
        self.assertIn(self.dumps(), (2, 3))

    def test_one_idle_state_dump_then_the_tree_succeeds_as_before(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE), ui_dump_not_idle=1)
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertGreaterEqual(payload["count"], 4)
        self.assertEqual(self.dumps(), 2)

    def test_ui_find_refuses_the_same_way(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE), ui_dump_not_idle=5)
        code, payload = self.android("ui", "find", "--text", "Settings")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.UI_NOT_IDLE)

    def test_compatibility_lists_ui_not_idle(self) -> None:
        """AL-05-S01: docs/COMPATIBILITY.md lists the code with what emits it."""
        text = (ROOT / "docs/COMPATIBILITY.md").read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if line.startswith("| `ui_not_idle` |")]
        self.assertEqual(len(rows), 1, "one row names ui_not_idle")
        self.assertIn("could not get idle state", rows[0])
        self.assertIn("backend_failed", rows[0])
        self.assertIn("Flow treats `ui_not_idle` exactly like `backend_failed`", rows[0])

    def test_the_docs_promise_what_the_code_does(self) -> None:
        """The rows and the changelog say what the code does: the same reads
        as before plus at most one, the last read picks the code, and Flow
        handles both codes alike — no claim from an earlier design."""
        compatibility = (ROOT / "docs/COMPATIBILITY.md").read_text(encoding="utf-8")
        section = compatibility.split(
            "### Android reads: never-idle screens and element state (Unreleased)", 1)[1]
        section = section.split("\n### ", 1)[0]
        changelog = _unreleased_changelog()
        for name, text in (("COMPATIBILITY.md", section), ("CHANGELOG.md", changelog)):
            with self.subTest(doc=name):
                self.assertIn("exactly like `backend_failed`", text)
                self.assertIn("within 2 s", text)
                for stale in ("after one dump", "after that one read", "one more read, past",
                              "do not treat the two codes alike", "fails the other polls",
                              "counts a never-idle read", "counts a `ui_not_idle` read",
                              "no read is skipped", "No read is skipped",
                              "read again every 0.5 s"):
                    self.assertNotIn(stale, text)


class TruncatedDumpCliTests(_Cli):
    def test_truncated_twice_then_complete_succeeds_after_three_dumps(self) -> None:
        """AL-05-S02."""
        self.set_state(ui_dump=str(UI_FIXTURE), ui_dump_incomplete=2)
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertGreaterEqual(payload["count"], 4)
        self.assertEqual(self.dumps(), 3)

    def test_truncated_every_time_fails_as_before(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE), ui_dump_incomplete=5)
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.BACKEND_FAILED)
        self.assertIn("mid-transition", payload["error"])
        self.assertIn(self.dumps(), (2, 3), "the second read always, never a fourth")


class SlowDumpCliTests(_Cli):
    """The fake adb takes 1.2 s per dump (a real emulator takes about 2 s):
    the second read still happens, a third could not start within 2 s of the
    end of the first bad read."""

    def test_a_slow_device_still_gets_its_second_read(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE), ui_dump_incomplete=1, ui_dump_delay_s=1.2)
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertGreaterEqual(payload["count"], 4)
        self.assertEqual(self.dumps(), 2)

    def test_a_slow_device_gets_no_third_read_outside_the_window(self) -> None:
        self.set_state(ui_dump=str(UI_FIXTURE), ui_dump_incomplete=2, ui_dump_delay_s=1.2)
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.BACKEND_FAILED)
        self.assertEqual(self.dumps(), 2)


def _completed(text: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=text, returncode=0)


class DumpHierarchyTests(unittest.TestCase):
    """`dump_hierarchy` with a scripted adb and an injected clock that each
    read (``read_s`` each, or ``read_times`` one by one) and each pause move
    on."""

    def _dump(self, outputs: list[str], *, read_s: float = 0.0,
              read_times: list[float] | None = None, **kwargs) -> str:
        now = [100.0]
        self.slept: list[float] = []
        self.calls: list[list[str]] = []
        self.started: list[float] = []
        self.ended: list[float] = []

        def run_adb(_adb, args, **_kw):
            self.calls.append(list(args))
            index = len(self.calls) - 1
            self.started.append(now[0])
            now[0] += read_times[index] if read_times is not None else read_s
            self.ended.append(now[0])
            return _completed(outputs[index])

        def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            now[0] += seconds

        with mock.patch.object(ui_android.adb_mod, "run_adb", side_effect=run_adb):
            return ui_android.dump_hierarchy("adb", "emulator-5554", sleep=sleep,
                                             clock=lambda: now[0], **kwargs)

    def _refused(self, outputs: list[str], **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            self._dump(outputs, **kwargs)
        return caught.exception

    def test_complete_dump_is_read_once(self) -> None:
        self.assertEqual(self._dump([COMPLETE]), COMPLETE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.slept, [])

    def test_cut_then_complete_is_read_twice_as_before(self) -> None:
        """Read, 0.5 s, read: what the dump always did for a cut document."""
        self.assertEqual(self._dump([CUT, COMPLETE]), COMPLETE)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.slept, [0.5])

    def test_slow_reads_cut_then_complete_succeed_after_two_reads(self) -> None:
        """2.3 s per dump (the bench emulator): the second read happens
        whatever the time, as it always did."""
        self.assertEqual(self._dump([CUT, COMPLETE], read_s=2.3), COMPLETE)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.slept, [0.5])

    def test_the_second_read_never_waits_on_the_window(self) -> None:
        """Even a window shorter than the pause leaves the second read."""
        self.assertEqual(self._dump([CUT, COMPLETE], read_s=2.3, window_s=0.1), COMPLETE)
        self.assertEqual(len(self.calls), 2)
        refused = self._refused([CUT, CUT, COMPLETE], window_s=0.1)
        self.assertEqual(refused.code, errors.BACKEND_FAILED)
        self.assertEqual(len(self.calls), 2)

    def test_slow_reads_get_no_third_read_outside_the_window(self) -> None:
        """2.3 s per dump: a third read would start 3.3 s after the end of
        the first bad read, so the second bad read is the last."""
        refused = self._refused([CUT, CUT, COMPLETE], read_s=2.3)
        self.assertEqual(refused.code, errors.BACKEND_FAILED)
        self.assertIn("incomplete hierarchy", refused.message)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.slept, [0.5])

    def test_fast_reads_cut_cut_complete_succeed_after_three_reads(self) -> None:
        """AL-05-S02 in-process."""
        self.assertEqual(self._dump([CUT, CUT, COMPLETE]), COMPLETE)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.slept, [0.5, 0.5])

    def test_fast_reads_cut_every_time_fail_as_backend_failed_after_three_reads(self) -> None:
        refused = self._refused([CUT] * 5)
        self.assertEqual(refused.code, errors.BACKEND_FAILED)
        self.assertIn("mid-transition", refused.message)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.slept, [0.5, 0.5], "no pause after the last read")

    def test_idle_state_on_the_last_read_is_ui_not_idle(self) -> None:
        refused = self._refused([CUT, CUT, NOT_IDLE_OUTPUT])
        self.assertEqual(refused.code, errors.UI_NOT_IDLE)
        self.assertIn("autonom screenshot", refused.hint)
        self.assertIn("ui wait --settled", refused.hint)
        self.assertNotIn("mid-transition", refused.message)
        self.assertEqual(len(self.calls), 3)

    def test_idle_state_every_time_is_ui_not_idle_after_three_reads(self) -> None:
        refused = self._refused([NOT_IDLE_OUTPUT] * 5)
        self.assertEqual(refused.code, errors.UI_NOT_IDLE)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.slept, [0.5, 0.5])

    def test_slow_idle_state_reads_are_ui_not_idle_after_two_reads(self) -> None:
        refused = self._refused([NOT_IDLE_OUTPUT] * 5, read_s=2.3)
        self.assertEqual(refused.code, errors.UI_NOT_IDLE)
        self.assertEqual(len(self.calls), 2)

    def test_idle_state_then_complete_succeeds(self) -> None:
        """One idle-state read gets the second read a cut dump gets."""
        self.assertEqual(self._dump([NOT_IDLE_OUTPUT, COMPLETE]), COMPLETE)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.slept, [0.5])

    def test_the_last_read_picks_the_code(self) -> None:
        refused = self._refused([NOT_IDLE_OUTPUT, NOT_IDLE_OUTPUT, CUT])
        self.assertEqual(refused.code, errors.BACKEND_FAILED)
        refused = self._refused([NOT_IDLE_OUTPUT, CUT], read_s=2.3)
        self.assertEqual(refused.code, errors.BACKEND_FAILED)

    def test_the_window_counts_from_the_end_of_the_first_bad_read(self) -> None:
        """A slow first read does not use the window up: it starts when that
        read has ended."""
        self.assertEqual(self._dump([CUT, CUT, COMPLETE], read_times=[2.2, 0.1, 0.1]),
                         COMPLETE)
        self.assertEqual(len(self.calls), 3)
        self.assertLessEqual(self.started[2] - self.ended[0], 2.0)

    def test_the_third_read_starts_inside_the_window_or_not_at_all(self) -> None:
        self.assertEqual(self._dump([CUT, CUT, COMPLETE], read_times=[0.0, 0.9, 0.0]),
                         COMPLETE)
        self.assertEqual(len(self.calls), 3)
        self.assertAlmostEqual(self.started[2] - self.ended[0], 1.9)
        refused = self._refused([CUT, CUT, COMPLETE], read_times=[0.0, 1.1, 0.0])
        self.assertEqual(refused.code, errors.BACKEND_FAILED)
        self.assertEqual(len(self.calls), 2, "it would have started 2.1 s in")

    def test_the_real_clock_and_sleep_are_the_defaults(self) -> None:
        outputs = [CUT, COMPLETE]
        with mock.patch.object(ui_android.adb_mod, "run_adb",
                               side_effect=lambda *_a, **_k: _completed(outputs.pop(0))), \
                mock.patch.object(ui_android.time, "sleep") as slept, \
                mock.patch.object(ui_android.time, "monotonic", return_value=5.0) as clock:
            self.assertEqual(ui_android.dump_hierarchy("adb", "s"), COMPLETE)
        slept.assert_called_once_with(0.5)
        self.assertGreaterEqual(clock.call_count, 1)

    def test_a_complete_tree_that_mentions_the_words_is_still_a_tree(self) -> None:
        text = COMPLETE.replace('text="Settings"', 'text="ERROR: could not get idle state."', 1)
        self.assertEqual(self._dump([text]), text)
        self.assertEqual(len(self.calls), 1)

    def test_idle_state_error_with_a_failing_exit_is_ui_not_idle(self) -> None:
        """A failing exit is raised at once, as it always was; with the
        idle-state words it is named for what it is."""
        refused = ui_android.adb_mod.AdbError("ERROR: could not get idle state.")
        with mock.patch.object(ui_android.adb_mod, "run_adb", side_effect=refused) as run:
            with self.assertRaises(errors.AutonomError) as caught:
                ui_android.dump_hierarchy("adb", "s", sleep=lambda _s: None)
        self.assertEqual(caught.exception.code, errors.UI_NOT_IDLE)
        self.assertEqual(run.call_count, 1)

    def test_other_adb_failures_are_unchanged(self) -> None:
        offline = ui_android.adb_mod.AdbError("error: device offline")
        with mock.patch.object(ui_android.adb_mod, "run_adb", side_effect=offline) as run:
            with self.assertRaises(errors.AutonomError) as caught:
                ui_android.dump_hierarchy("adb", "s", sleep=lambda _s: None)
        self.assertIs(caught.exception, offline)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(run.call_count, 1)

    def test_detection(self) -> None:
        self.assertTrue(ui_android.is_not_idle_output(NOT_IDLE_OUTPUT))
        self.assertTrue(ui_android.is_not_idle_output("error: Could Not Get Idle State"))
        self.assertFalse(ui_android.is_not_idle_output(CUT))
        self.assertFalse(ui_android.is_not_idle_output(""))

    def test_limits_and_code(self) -> None:
        self.assertEqual(ui_android.DUMP_READS, 3)
        self.assertEqual(ui_android.DUMP_RETRY_PAUSE_S, 0.5)
        self.assertEqual(ui_android.DUMP_RETRY_WINDOW_S, 2.0)
        self.assertEqual(errors.UI_NOT_IDLE, "ui_not_idle")


# --- AL-05: Flow treats ui_not_idle exactly like backend_failed -----------------

_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"
_FOCUSED = {"ref": "n1", "role": "textfield", "text": "", "resource_id": "field",
            "focused": True, "bounds": [0, 0, 100, 40], "class": "android.widget.EditText"}
_SETTINGS = {"ref": "n2", "role": "text", "text": "Settings", "bounds": [0, 50, 100, 90]}


def _not_idle() -> errors.AutonomError:
    return ui_android.not_idle_error()


def _backend_failed() -> errors.AutonomError:
    return errors.AutonomError(errors.BACKEND_FAILED, "UIAutomator returned an incomplete "
                               "hierarchy (the screen was probably mid-transition)")


class _InProcess(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sandbox_home()
        no_shot = errors.AutonomError(errors.BACKEND_FAILED, "no screenshot in tests")
        for patcher in (
                mock.patch("autonom_lib.screenshot.capture_evidence", side_effect=no_shot),
                mock.patch("autonom_lib.logs.tail", return_value=([], [])),
                mock.patch("autonom_lib.ui.tree", return_value=([], []))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def executor(self, target: Target | None = None, **config):
        target = target or Target("android", "emulator-5554", str(FAKE_ADB),
                                  {"serial": "emulator-5554"})
        session = {"session_id": "s_test", "artifacts_dir": str(self.root / "art")}
        clock = [0.0]
        return flow_executor.Executor(
            target, session,
            flow_executor.RunConfig(**{"default_timeout_ms": 2000, "interval_ms": 500,
                                       **config}),
            clock=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            screen=(1080, 1920),
        )

    def build(self, body: str):
        return flow_schema.build_flow(
            flow_parser.parse_document(_HEAD + body, str(self.root / "t.yaml")))

    @staticmethod
    def scripted(*items):
        """A `ui.snapshot` that raises or returns the items in turn, then
        repeats the last one."""
        queue = list(items)
        calls = [0]

        def snapshot(_target):
            calls[0] += 1
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        return snapshot, calls


# One step of every kind that reads the tree; `inputText` is the focus poll.
_READING_STEPS = {
    "assertVisible": "- assertVisible:\n    selector:\n      text: Settings\n",
    "assertNotVisible": "- assertNotVisible:\n    selector:\n      text: Settings\n",
    "waitUntil": ("- waitUntil:\n    visible:\n      text: Settings\n"
                  "    timeoutMs: 3000\n"),
    "tapOn": "- tapOn:\n    selector:\n      text: Settings\n",
    "waitForSettled": "- waitForSettled\n",
    "scrollUntilVisible": "- scrollUntilVisible:\n    selector:\n      text: Settings\n",
    "runFlow when": ("- runFlow:\n    when:\n      visible:\n        text: Settings\n"
                     "    commands:\n      - back\n"),
    "repeat while": ("- repeat:\n    times: 2\n    while:\n      visible:\n"
                     "        text: Settings\n    commands:\n      - back\n"),
    "inputText": "- inputText: hi\n",
}


class FlowCodeParityTests(_InProcess):
    """DEC-007: Flow handles `ui_not_idle` exactly where it handled
    `backend_failed`. A tree read that fails with either code gives every
    command the same outcome: the same reads, the same result, the same
    failure class."""

    def outcome(self, body: str, error: Exception, *then) -> tuple:
        screen = [_FOCUSED] if "inputText" in body else [_SETTINGS]
        snapshot, calls = self.scripted(error, *(then or (screen,)))
        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.type_text") as typed, \
             mock.patch("autonom_lib.ui.press_key") as pressed, \
             mock.patch("autonom_lib.ui.tap") as tapped, \
             mock.patch("autonom_lib.ui.swipe") as swiped:
            try:
                result = self.executor().run(self.build(body))
                end = ("ran", result.status, None)
            except errors.AutonomError as exc:
                self.assertEqual(exc.code, error.code, "the read's own code surfaces")
                end = ("raised", exc.extra.get("failure_class"), exc.extra.get("step"))
        return (end, calls[0], typed.call_count, pressed.call_count,
                tapped.call_count, swiped.call_count)

    def test_one_failed_read_then_a_tree(self) -> None:
        for name, body in _READING_STEPS.items():
            with self.subTest(step=name):
                self.assertEqual(self.outcome(body, _not_idle()),
                                 self.outcome(body, _backend_failed()))

    def test_a_screen_that_is_never_read(self) -> None:
        for name, body in _READING_STEPS.items():
            with self.subTest(step=name):
                self.assertEqual(self.outcome(body, _not_idle(), _not_idle()),
                                 self.outcome(body, _backend_failed(), _backend_failed()))

    def test_focus_poll_reads_again_after_ui_not_idle(self) -> None:
        """The one poll that read again after `backend_failed` does the
        same after `ui_not_idle`."""
        flow = self.build("- inputText: hi\n")
        snapshot, calls = self.scripted(_not_idle(), [_FOCUSED])
        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.type_text") as typed:
            result = self.executor().run(flow)
        self.assertEqual(result.status, "passed", result.failure)
        self.assertEqual(calls[0], 2)
        typed.assert_called_once()

    def test_focus_poll_that_never_reads_fails_with_ui_not_idle(self) -> None:
        flow = self.build("- inputText: hi\n")
        snapshot, calls = self.scripted(_not_idle())
        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.type_text") as typed:
            with self.assertRaises(errors.AutonomError) as caught:
                self.executor().run(flow)
        self.assertEqual(caught.exception.code, errors.UI_NOT_IDLE)
        self.assertEqual(caught.exception.extra.get("failure_class"),
                         flow_schema.INFRASTRUCTURE)
        self.assertEqual(calls[0], 5, "every 0.5 s until the 2 s deadline")
        typed.assert_not_called()

    def test_an_assertion_fails_on_it_at_once_like_backend_failed(self) -> None:
        """The dump has already read the screen again; the assertion poll
        never read again over `backend_failed` and does not over this."""
        flow = self.build("- assertVisible:\n    selector:\n      text: Settings\n")
        snapshot, calls = self.scripted(_not_idle(), [_SETTINGS])
        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot):
            with self.assertRaises(errors.AutonomError) as caught:
                self.executor().run(flow)
        self.assertEqual(caught.exception.code, errors.UI_NOT_IDLE)
        self.assertEqual(calls[0], 1)

    def test_both_codes_are_infrastructure(self) -> None:
        self.assertEqual(flow_schema.failure_class(errors.UI_NOT_IDLE),
                         flow_schema.failure_class(errors.BACKEND_FAILED))
        self.assertEqual(flow_schema.failure_class(errors.UI_NOT_IDLE),
                         flow_schema.INFRASTRUCTURE)

    def test_a_retry_block_aborts_on_it_like_backend_failed(self) -> None:
        """Infrastructure failures abort a `retry` block instead of being
        retried; a never-idle screen still does, under its new code."""
        body = ("- retry:\n"
                "    maxAttempts: 3\n"
                "    commands:\n"
                "      - assertVisible:\n"
                "          selector:\n"
                "            text: Settings\n"
                "          timeoutMs: 500\n")
        for code, error in ((errors.BACKEND_FAILED, _backend_failed()),
                            (errors.UI_NOT_IDLE, _not_idle())):
            with self.subTest(code=code):
                snapshot, _calls = self.scripted(error)
                with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot):
                    with self.assertRaises(errors.AutonomError) as caught:
                        self.executor().run(self.build(body))
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.extra.get("failure_class"),
                                 flow_schema.INFRASTRUCTURE)


# Dumps each command makes when the fake adb prints the idle-state error once
# and then the tree, measured at the base commit 051c3d9: FlowDumpCountTests
# below was run, unchanged, in a copy of this tree whose `scripts/` and
# `plugins/` came from `git archive 051c3d9` (this tree's tests, fake adb and
# fixture; the same RunConfig, injected executor clock and patches), and
# passed with these numbers. There the idle-state text was a cut document that
# the dump read again after 0.5 s; here it is a bad read that gets the same
# second read. (waitForSettled: the first snapshot takes the two dumps, then
# three more reads of the same tree fill its quiet window.)
BASE_DUMPS = {"waitForSettled": 5, "scrollUntilVisible": 2, "assertVisible": 2,
              "inputText": 2, "runFlow when": 2}


class FlowDumpCountTests(_InProcess):
    """The real Android read path under each command: one never-idle dump,
    then the tree (a spinner that outlasts one uiautomator idle wait, then
    stops). The flow passes with as many dumps as at the base commit."""

    def fake_adb(self, dump: Path = UI_FIXTURE) -> Path:
        state = self.root / "state.json"
        log = self.root / "log.jsonl"
        state.write_text(json.dumps({"ui_dump": str(dump), "ui_dump_not_idle": 1}),
                         encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(state), AUTONOM_FAKE_LOG=str(log))
        return log

    @staticmethod
    def adb_rows(log: Path) -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def dumps(self, log: Path) -> int:
        return sum(1 for row in self.adb_rows(log) if "uiautomator" in row["argv"])

    def focused_dump(self) -> Path:
        """The fixture with its search field enabled and focused."""
        old = ('class="android.widget.EditText" package="com.example.app" content-desc="" '
               'clickable="true" enabled="false" focusable="true"')
        text = COMPLETE.replace(old, old.replace('enabled="false"', 'enabled="true"')
                                + ' focused="true"', 1)
        self.assertIn('focused="true"', text)
        path = self.root / "focused.xml"
        path.write_text(text, encoding="utf-8")
        return path

    def run_flow(self, name: str, body: str, dump: Path = UI_FIXTURE):
        log = self.fake_adb(dump)
        with mock.patch("autonom_lib.ui.type_text") as typed, \
             mock.patch("autonom_lib.ui.press_key") as pressed, \
             mock.patch("autonom_lib.ui.swipe") as swiped:
            result = self.executor().run(self.build(body))
        self.assertEqual(result.status, "passed", result.failure)
        self.assertEqual(self.dumps(log), BASE_DUMPS[name])
        return result, typed, pressed, swiped

    def test_wait_for_settled(self) -> None:
        result, _typed, _pressed, _swiped = self.run_flow(
            "waitForSettled", "- waitForSettled\n")
        self.assertIs(result.steps[0].settled, True)
        self.assertEqual([w for w in result.warnings if w.get("code") == "screen_not_settled"],
                         [])

    def test_scroll_until_visible(self) -> None:
        _result, _typed, _pressed, swiped = self.run_flow(
            "scrollUntilVisible",
            "- scrollUntilVisible:\n    selector:\n      text: Search input\n")
        swiped.assert_not_called()

    def test_assert_visible(self) -> None:
        self.run_flow("assertVisible",
                      "- assertVisible:\n    selector:\n      text: Search input\n")

    def test_input_text_focus_poll(self) -> None:
        _result, typed, _pressed, _swiped = self.run_flow(
            "inputText", "- inputText: hi\n", self.focused_dump())
        typed.assert_called_once()

    def test_run_flow_when(self) -> None:
        _result, _typed, pressed, _swiped = self.run_flow(
            "runFlow when",
            "- runFlow:\n    when:\n      visible:\n        text: Search input\n"
            "    commands:\n      - back\n")
        pressed.assert_called_once()


# --- AL-06: state in compact nodes and the outline ------------------------------


def _android_nodes() -> list[dict]:
    return ui.parse_compact_tree(CHECKABLE.read_text(encoding="utf-8"))


def _line(nodes: list[dict], **match) -> str:
    found = [node for node in nodes
             if all(node.get(key) == value for key, value in match.items())]
    if len(found) != 1:
        raise AssertionError(f"{match} matched {len(found)} nodes")
    return ui.outline_line(found[0])


def _flags(line: str) -> list[str]:
    return line.rsplit("[", 1)[1].rstrip("]").split() if line.endswith("]") else []


class AndroidCompactStateTests(unittest.TestCase):
    def test_editable_and_password_are_kept(self) -> None:
        nodes = {node["resource_id"] or node["class"]: node for node in _android_nodes()}
        email = nodes["com.android.settings:id/email"]
        self.assertIs(email["editable"], True)
        self.assertIs(email["password"], False)
        secret = nodes["com.android.settings:id/password"]
        self.assertIs(secret["editable"], True)
        self.assertIs(secret["password"], True)
        # a subclass of EditText, and a node with a hint
        self.assertIs(nodes["com.android.settings:id/proxy_host"]["editable"], True)
        self.assertIs(nodes["android.view.View"]["editable"], True)
        for plain in ("com.android.settings:id/summary", "com.android.settings:id/wifi_switch",
                      "android:id/switch_widget"):
            with self.subTest(node=plain):
                self.assertIs(nodes[plain]["editable"], False)
                self.assertIs(nodes[plain]["password"], False)

    def test_every_compact_node_has_both_keys(self) -> None:
        for node in ui_android.parse_all(UI_FIXTURE.read_text(encoding="utf-8")):
            self.assertIn("editable", node)
            self.assertIn("password", node)

    def test_editable_class_rule(self) -> None:
        for name in ("android.widget.EditText", "androidx.appcompat.widget.AppCompatEditText",
                     "com.google.android.material.textfield.TextInputEditText",
                     "android.widget.AutoCompleteTextView",
                     "android.widget.SearchView$SearchAutoComplete"):
            with self.subTest(name=name):
                self.assertTrue(ui_android.is_editable_class(name))
        for name in ("android.widget.TextView", "android.widget.Button", "", None):
            with self.subTest(name=name):
                self.assertFalse(ui_android.is_editable_class(name))

    def test_blank_hint_does_not_make_a_node_editable(self) -> None:
        xml = ('<hierarchy><node index="0" text="Label" class="android.widget.TextView" '
               'hint="  " bounds="[0,0][10,10]" /></hierarchy>')
        self.assertIs(ui_android.parse_all(xml)[0]["editable"], False)

    def test_a_focused_node_with_a_hint_takes_typed_text(self) -> None:
        def focused_view(hint: str) -> list[dict]:
            return ui_android.parse_all(
                f'<hierarchy><node index="0" text="" class="android.view.View" hint="{hint}" '
                'focused="true" bounds="[0,0][10,10]" /></hierarchy>')

        hinted = focused_view("Search settings")
        self.assertEqual(ui.typing_target("android", hinted), (hinted[0], "focused"))
        plain = focused_view("")
        self.assertEqual(ui.typing_target("android", plain),
                         (plain[0], "focused_not_editable"))


class OutlineStateTests(unittest.TestCase):
    def test_unchecked_switch(self) -> None:
        """AL-06-S01: the off switch's line says `unchecked`."""
        line = _line(_android_nodes(), resource_id="android:id/switch_widget")
        self.assertIn("unchecked", _flags(line))
        self.assertNotIn("checked", _flags(line))

    def test_checked_and_unchecked_controls(self) -> None:
        nodes = _android_nodes()
        self.assertEqual(_flags(_line(nodes, text="Wi-Fi")), ["clickable", "checked"])
        self.assertEqual(_flags(_line(nodes, text="Remember me")),
                         ["clickable", "unchecked", "disabled"])

    def test_text_fields(self) -> None:
        nodes = _android_nodes()
        self.assertEqual(_flags(_line(nodes, resource_id="com.android.settings:id/password")),
                         ["clickable", "focused", "editable", "password"])
        self.assertEqual(_flags(_line(nodes, resource_id="com.android.settings:id/email")),
                         ["clickable", "editable"])

    def test_other_flags_are_kept(self) -> None:
        nodes = _android_nodes()
        self.assertEqual(_flags(_line(nodes, text="Mobile network")),
                         ["clickable", "selected"])
        self.assertEqual(_flags(_line(nodes, text="Data usage")), [])
        node = {"ref": "n1", "role": "list", "bounds": [0, 0, 10, 10], "scrollable": True,
                "visible": False}
        self.assertEqual(_flags(ui.outline_line(node)), ["scrollable", "offscreen"])

    def test_non_checkable_nodes_say_nothing_about_checking(self) -> None:
        for line in ui.outline(_android_nodes()).splitlines():
            if "switch" in line.split()[1] or "checkbox" in line.split()[1]:
                continue
            with self.subTest(line=line):
                self.assertNotIn("unchecked", _flags(line))
        # a checked node that does not say it is checkable still reads checked
        self.assertEqual(_flags(ui.outline_line({"ref": "n1", "role": "node",
                                                  "checked": True})), ["checked"])

    def test_flag_order_is_fixed(self) -> None:
        node = {"ref": "n1", "role": "switch", "clickable": True, "scrollable": True,
                "focused": True, "selected": True, "checkable": True, "checked": False,
                "editable": True, "password": True, "enabled": False, "visible": False}
        self.assertEqual(ui.outline_flags(node),
                         ["clickable", "scrollable", "focused", "selected", "unchecked",
                          "editable", "password", "disabled", "offscreen"])

    def test_cli_dump_outline(self) -> None:
        """AL-06-S01 through the CLI: `ui tree --dump <file> --format outline`."""
        with tempfile.TemporaryDirectory() as scratch:
            completed = subprocess.run(
                [sys.executable, str(CLI), "ui", "tree", "--dump", str(CHECKABLE),
                 "--format", "outline"], capture_output=True, text=True, timeout=60,
                cwd=scratch, env={**os.environ, "AUTONOM_HOME": str(Path(scratch) / "home")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        outline = json.loads(completed.stdout)["outline"].splitlines()
        switch = [line for line in outline if "id=android:id/switch_widget" in line]
        self.assertEqual(len(switch), 1, outline)
        self.assertIn("unchecked", _flags(switch[0]))


class IosStateTests(unittest.TestCase):
    ELEMENTS = [
        {"type": "Application", "AXLabel": "App",
         "frame": {"x": 0, "y": 0, "width": 402, "height": 874}},
        {"type": "TextField", "AXLabel": "Email", "AXValue": "",
         "frame": {"x": 0, "y": 100, "width": 300, "height": 40}},
        {"type": "SecureTextField", "AXLabel": "Password", "AXValue": "••••",
         "frame": {"x": 0, "y": 150, "width": 300, "height": 40}},
        {"type": "Other", "role": "AXTextField", "subrole": "AXSecureTextField",
         "AXLabel": "PIN", "frame": {"x": 0, "y": 200, "width": 300, "height": 40}},
        {"type": "SearchField", "AXLabel": "Search",
         "frame": {"x": 0, "y": 250, "width": 300, "height": 40}},
        {"type": "Switch", "AXLabel": "Wi-Fi", "AXValue": "0",
         "frame": {"x": 0, "y": 300, "width": 60, "height": 30}},
        {"type": "Button", "AXLabel": "Save",
         "frame": {"x": 0, "y": 350, "width": 90, "height": 44}},
    ]

    def nodes(self) -> dict[str, dict]:
        return {node["desc"]: node for node in ui_ios.parse_all(json.dumps(self.ELEMENTS))}

    def test_text_fields_are_editable_and_secure_ones_password(self) -> None:
        nodes = self.nodes()
        expected = {"App": (False, False), "Email": (True, False), "Password": (True, True),
                    "PIN": (True, True), "Search": (True, False), "Wi-Fi": (False, False),
                    "Save": (False, False)}
        self.assertEqual({label: (node["editable"], node["password"])
                          for label, node in nodes.items()}, expected)

    def test_outline_prints_them(self) -> None:
        nodes = self.nodes()
        self.assertIn("editable", _flags(ui.outline_line(nodes["Email"])))
        self.assertNotIn("password", _flags(ui.outline_line(nodes["Email"])))
        self.assertEqual(_flags(ui.outline_line(nodes["Password"]))[-2:],
                         ["editable", "password"])
        self.assertIn("unchecked", _flags(ui.outline_line(nodes["Wi-Fi"])))

    def test_key_sets_still_match_android(self) -> None:
        ios = ui_ios.parse_all(json.dumps(self.ELEMENTS))
        android = ui_android.parse_all(UI_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(set(ios[0]), set(android[0]))

    def test_fixture_text_field(self) -> None:
        nodes = ui_ios.parse_all(IOS_TEXTFIELD.read_text(encoding="utf-8"))
        search = next(node for node in nodes if node.get("resource_id") == "search_field")
        self.assertIs(search["editable"], True)
        self.assertIs(search["password"], False)


if __name__ == "__main__":
    unittest.main()
