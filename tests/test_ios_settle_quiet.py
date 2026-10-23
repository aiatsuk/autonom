"""Quiet-based settle on iOS (AL-08): `ui wait --settled` on the idb route asks
`idb ui quiet` first and then reads the tree exactly once; when idb has no
such command or the call fails, it polls as before within what is left of
the budget. XCUITest, Android and callers with their own reader keep polling.

The oracle is what was dispatched: the fake idb logs every argv, so each CLI
case counts `ui quiet` and `ui describe-all` calls and checks the reply's
`method` and `snapshots`. The fake answers `ui quiet` only when a case turns
its quiet mode on (`idb_quiet`); otherwise it refuses the command the way an
idb without it does. The CLI subprocesses get a PATH holding only a
``python3`` link and the system directories, so a real idb cannot be reached.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
ENOENT = ("Failed to connect to companion at address DomainSocketAddress(path='/tmp/idb/"
          f"{UDID}_companion.sock'): [Errno 2] No such file or directory")
EXITED = "The application with pid 4242 exited before it went quiet"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_idb, session, ui, ui_ios  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _verb(argv: list[str]) -> list[str]:
    """The verb of a logged idb argv, with top-level options stripped."""
    rest = list(argv)
    while rest and rest[0] in ("--companion", "--companion-path", "--log", "--compression"):
        rest = rest[2:]
    return rest


class CliBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.registry = self.root / "idb-state.json"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.root / "home"),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
            AUTONOM_AXE=None, AUTONOM_IOS_HID=None, AUTONOM_IDB_COMPANION=None,
            AUTONOM_IDB_STATE_FILE=str(self.registry), AUTONOM_UI_BACKEND=None,
        )
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        self.set_state()

    def set_state(self, **kwargs: Any) -> None:
        kwargs.setdefault("simctl_devices", BOOTED)
        kwargs.setdefault("idb_describe_all", str(IOS_FIXTURE))
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def ios(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--udid", UDID, *argv],
            capture_output=True, text=True, env=self.env, timeout=120, cwd=self.tmp.name,
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [_verb(row["argv"]) for row in rows if row["tool"] == "idb"]

    def count(self, *verb: str) -> int:
        return [argv[:len(verb)] for argv in self.calls()].count(list(verb))

    def quiet_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls() if argv[:2] == ["ui", "quiet"]]


# --- AL-08 through the CLI -----------------------------------------------------------------------


class QuietSettleCliTests(CliBase):
    def test_quiet_supported_reads_once(self) -> None:  # AL-08-S01
        self.set_state(idb_quiet="quiet")
        code, payload = self.ios("ui", "wait", "--settled")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["ok"], payload["settled"], payload["method"],
                          payload["snapshots"]), (True, True, "idb-quiet", 1))
        self.assertEqual(payload["quiet_state"], "quiet")
        self.assertNotIn("warnings", payload)
        self.assertNotIn("quiet_fallback", payload)
        self.assertEqual(self.count("ui", "describe-all"), 1)
        # the documented argv: idb's own timeout is the whole budget, in seconds
        self.assertEqual(self.quiet_calls(), [
            ["ui", "quiet", "5", "--json", "--quiet-window-ms", "500", "--udid", UDID]])

    def test_budget_and_window_reach_idb(self) -> None:
        self.set_state(idb_quiet="quiet")
        code, payload = self.ios("ui", "wait", "--settled", "--timeout-ms", "2250",
                                 "--quiet-ms", "300")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["timeout_ms"], payload["quiet_ms"]), (2250, 300))
        self.assertEqual(payload["stable_ms"], 300, "idb held quiet for the whole window")
        self.assertEqual(self.quiet_calls(), [
            ["ui", "quiet", "2.25", "--json", "--quiet-window-ms", "300", "--udid", UDID]])

    def test_busy_at_the_deadline_is_unsettled_with_the_read_made(self) -> None:
        self.set_state(idb_quiet="busy", idb_quiet_signals=["run_loop_idle"])
        code, payload = self.ios("ui", "wait", "--settled", "--timeout-ms", "1000")
        self.assertEqual(code, 1, payload)
        self.assertEqual((payload["ok"], payload["settled"], payload["method"],
                          payload["snapshots"]), (False, False, "idb-quiet", 1))
        self.assertEqual((payload["quiet_state"], payload["busy_signals"]),
                         ("busy", ["run_loop_idle"]))
        self.assertEqual(self.count("ui", "describe-all"), 1, "the read is still made")
        self.assertEqual(self.count("ui", "quiet"), 1, "an answer is not retried")
        (warning,) = payload["warnings"]
        self.assertEqual(warning["code"], "screen_not_settled")
        self.assertEqual(warning["error"],
                         "idb reported the app still busy (run_loop_idle) after 1000 ms")
        self.assertEqual(warning["hint"].count("raise"), 1, warning)

    def test_settling_at_the_deadline_says_so(self) -> None:
        self.set_state(idb_quiet="settling")
        code, payload = self.ios("ui", "wait", "--settled", "--timeout-ms", "800",
                                 "--quiet-ms", "600")
        self.assertEqual(code, 1, payload)
        self.assertEqual((payload["settled"], payload["method"], payload["snapshots"],
                          payload["quiet_state"]), (False, "idb-quiet", 1, "settling"))
        self.assertNotIn("busy_signals", payload)
        (warning,) = payload["warnings"]
        self.assertTrue(warning["error"].startswith("idb reported the app settling"), warning)
        self.assertIn("600 ms quiet window", warning["error"])

    def test_quiet_unsupported_polls_as_before(self) -> None:  # AL-08-S02
        # the fake's default: `ui quiet` is an unknown choice, as on fb-idb 1.1.7
        code, payload = self.ios("ui", "wait", "--settled", "--quiet-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["settled"], payload["method"]), (True, "poll"))
        # the old settle result: two identical snapshots, no change
        self.assertGreaterEqual(payload["snapshots"], 2)
        self.assertEqual(payload["changes"], 0)
        self.assertEqual(payload["quiet_fallback"]["code"], errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("ui quiet", payload["quiet_fallback"]["error"])
        self.assertEqual(self.count("ui", "quiet"), 1, "asked once, not retried")
        self.assertEqual(self.count("ui", "describe-all"), payload["snapshots"])
        self.assertEqual(self.count("list-targets"), 0, "a refusal is not a dead companion")

    def test_a_failed_quiet_call_polls(self) -> None:
        self.set_state(idb_quiet="quiet", idb_fail={"ui quiet": [1, EXITED]})
        code, payload = self.ios("ui", "wait", "--settled", "--quiet-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["settled"], payload["method"]), (True, "poll"))
        self.assertEqual(payload["quiet_fallback"],
                         {"code": errors.BACKEND_FAILED, "error": EXITED})
        self.assertEqual(self.count("ui", "describe-all"), payload["snapshots"])

    def test_a_dead_companion_is_pruned_and_quiet_asked_again(self) -> None:  # AL-04 x AL-08
        self.set_state(idb_quiet="quiet", idb_fail_once={"ui quiet": [1, ENOENT]})
        code, payload = self.ios("ui", "wait", "--settled")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["method"], payload["snapshots"]), ("idb-quiet", 1))
        self.assertEqual(self.count("ui", "quiet"), 2)
        self.assertEqual(self.count("list-targets"), 1)
        self.assertEqual(self.count("ui", "describe-all"), 1)
        # the retry carries only what the failed call and the prune left of
        # the 5 s budget, never the whole budget again (RISK-012)
        first, again = self.quiet_calls()
        self.assertEqual(first[2], "5")
        self.assertLess(float(again[2]), 5.0, again)
        self.assertGreater(float(again[2]), 0.0, again)
        self.assertEqual(again[3:], first[3:])

    def test_a_zero_budget_polls_once_without_asking(self) -> None:
        # idb reads a 0 s timeout as "no limit", so a zero budget is never sent
        self.set_state(idb_quiet="quiet")
        code, payload = self.ios("ui", "wait", "--settled", "--timeout-ms", "0")
        self.assertEqual(code, 1, payload)
        self.assertEqual((payload["settled"], payload["method"], payload["snapshots"]),
                         (False, "poll", 1))
        self.assertNotIn("quiet_fallback", payload)
        self.assertEqual(self.count("ui", "quiet"), 0)

    def test_a_remote_companion_is_asked_through_its_endpoint(self) -> None:
        self.set_state(idb_quiet="quiet")
        code, payload = self.ios("--idb-host", "10.0.0.5", "--idb-port", "10882",
                                 "ui", "wait", "--settled")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["method"], "idb-quiet")
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        quiet = [row["argv"] for row in rows if "quiet" in row["argv"]]
        self.assertEqual(len(quiet), 1)
        self.assertEqual(quiet[0][:4], ["--companion", "10.0.0.5:10882", "ui", "quiet"])


# --- in process: routes, the budget, the wrapper -----------------------------------------------


class InProcessBase(EnvSandboxMixin, unittest.TestCase):
    TARGET = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.registry = self.root / "idb-state.json"
        # our own live pid as the companion: the call tracks nothing (AL-02)
        self.registry.write_text(json.dumps([{"udid": UDID, "is_local": True,
                                              "pid": os.getpid()}]), encoding="utf-8")
        self.set_env(AUTONOM_HOME=str(self.root / "home"), AUTONOM_UI_BACKEND=None,
                     AUTONOM_IDB=str(FAKE_IDB), AUTONOM_IDB_COMPANION=None,
                     AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_IDB_STATE_FILE=str(self.registry))
        token = session.select(None)
        self.addCleanup(session._SELECTED.reset, token)  # noqa: SLF001
        self.reset()
        self.addCleanup(self.reset)
        self.write_state()

    @staticmethod
    def reset() -> None:
        ui_ios.set_preference(None)
        ios_idb.reset_quiet_support()
        if hasattr(ui._LAST_SNAPSHOT, "value"):  # noqa: SLF001
            del ui._LAST_SNAPSHOT.value  # noqa: SLF001

    def write_state(self, **kwargs: Any) -> None:
        kwargs.setdefault("idb_describe_all", str(IOS_FIXTURE))
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def quiet_count(self) -> int:
        if not self.log.exists():
            return 0
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return sum(1 for row in rows if row["argv"][:2] == ["ui", "quiet"])


class QuietRouteTests(InProcessBase):
    def test_the_read_after_quiet_is_the_last_snapshot(self) -> None:
        """Observe (AL-11) reuses the settle read instead of reading again."""
        self.write_state(idb_quiet="quiet")
        result = ui.settle(self.TARGET, timeout_ms=3000, quiet_ms=200)
        self.assertEqual((result["method"], result["snapshots"]), ("idb-quiet", 1))
        held = ui.last_snapshot(self.TARGET)
        self.assertTrue(held)
        self.assertEqual(held[0]["role"], "app")

    def test_unsupported_is_remembered_for_the_process(self) -> None:
        for _ in range(2):
            result = ui.settle(self.TARGET, timeout_ms=2000, quiet_ms=0)
            self.assertEqual(result["method"], "poll")
            self.assertEqual(result["quiet_fallback"]["code"], errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(self.quiet_count(), 1, "the second settle did not ask again")

    def test_other_failures_are_not_remembered(self) -> None:
        self.write_state(idb_quiet="quiet", idb_fail={"ui quiet": [1, EXITED]})
        ui.settle(self.TARGET, timeout_ms=2000, quiet_ms=0)
        self.write_state(idb_quiet="quiet")
        result = ui.settle(self.TARGET, timeout_ms=2000, quiet_ms=0)
        self.assertEqual(result["method"], "idb-quiet")
        self.assertEqual(self.quiet_count(), 2)

    def test_xcuitest_route_keeps_polling(self) -> None:
        nodes = [{"ref": "n0", "role": "app", "bounds": [0, 0, 10, 10]}]
        with mock.patch.object(ui_ios, "selected_backend", return_value="xcuitest"), \
                mock.patch.object(ui_ios, "wait_quiet") as quiet, \
                mock.patch.object(ui, "snapshot", return_value=nodes):
            result = ui.settle(self.TARGET, timeout_ms=2000, quiet_ms=0)
        quiet.assert_not_called()
        self.assertEqual((result["method"], result["settled"]), ("poll", True))
        self.assertNotIn("quiet_fallback", result)

    def test_android_keeps_polling(self) -> None:
        android = Target(ANDROID, "emulator-5554", "/bin/false", {"serial": "emulator-5554"})
        nodes = [{"ref": "n0", "role": "text", "text": "x"}]
        with mock.patch.object(ui_ios, "wait_quiet") as quiet, \
                mock.patch.object(ui, "snapshot", return_value=nodes):
            result = ui.settle(android, timeout_ms=2000, quiet_ms=0)
        quiet.assert_not_called()
        self.assertEqual(result["method"], "poll")

    def test_a_caller_with_its_own_reader_keeps_polling(self) -> None:
        """The Flow engine reads through `snapshot_fn` (INV-4: flows as before)."""
        reads = []

        def take() -> list[dict[str, Any]]:
            reads.append(1)
            return [{"ref": "n0", "role": "text", "text": "x"}]

        with mock.patch.object(ui_ios, "wait_quiet") as quiet:
            result = ui.settle(self.TARGET, timeout_ms=2000, quiet_ms=0, snapshot_fn=take)
            quiet.assert_not_called()
            self.assertEqual(result["method"], "poll")
            self.assertGreaterEqual(len(reads), 2)
            quiet.return_value = {"quiet": True, "state": "quiet", "signals": []}
            reads.clear()
            opted = ui.settle(self.TARGET, timeout_ms=2000, quiet_ms=0, snapshot_fn=take,
                              quiet_signal=True)
        self.assertEqual((opted["method"], opted["snapshots"], len(reads)), ("idb-quiet", 1, 1))

    def test_quiet_signal_false_always_polls(self) -> None:
        self.write_state(idb_quiet="quiet")
        result = ui.settle(self.TARGET, timeout_ms=2000, quiet_ms=0, quiet_signal=False)
        self.assertEqual(result["method"], "poll")
        self.assertEqual(self.quiet_count(), 0)

    def test_the_fallback_polls_only_the_budget_left(self) -> None:
        """A quiet call that failed after 1.5 s of a 1 s budget leaves no room:
        one read, unsettled, and the elapsed time counts the failed call."""
        now = [0.0]

        def failing(*_args: Any, **_kwargs: Any) -> dict:
            now[0] += 1.5
            raise errors.AutonomError(errors.BACKEND_FAILED, "stream ended")

        def take() -> list[dict[str, Any]]:
            now[0] += 0.25
            return [{"ref": "n0", "role": "text", "text": "x"}]

        with mock.patch.object(ui_ios, "wait_quiet", side_effect=failing), \
                mock.patch.object(ui, "snapshot", side_effect=lambda _t: take()):
            result = ui.settle(self.TARGET, timeout_ms=1000, quiet_ms=0, interval_ms=100,
                               clock=lambda: now[0],
                               sleep=lambda s: now.__setitem__(0, now[0] + s))
        self.assertEqual((result["settled"], result["method"], result["snapshots"]),
                         (False, "poll", 1))
        self.assertEqual(result["elapsed_ms"], 1750)
        self.assertEqual(result["quiet_fallback"], {"code": errors.BACKEND_FAILED,
                                                    "error": "stream ended"})

    def test_the_fallback_keeps_the_rest_of_the_budget(self) -> None:
        now = [0.0]

        def refused(*_args: Any, **_kwargs: Any) -> dict:
            now[0] += 0.5
            raise errors.AutonomError(errors.UNSUPPORTED_ON_PLATFORM, "no ui quiet")

        def take() -> list[dict[str, Any]]:
            now[0] += 0.25
            return [{"ref": "n0", "role": "text", "text": "x"}]

        with mock.patch.object(ui_ios, "wait_quiet", side_effect=refused), \
                mock.patch.object(ui, "snapshot", side_effect=lambda _t: take()):
            result = ui.settle(self.TARGET, timeout_ms=3000, quiet_ms=500, interval_ms=250,
                               clock=lambda: now[0],
                               sleep=lambda s: now.__setitem__(0, now[0] + s))
        # 0.5 s refused, then reads ending at 0.75 and 1.25 s: the same tree
        # for 500 ms, within the 3 s budget the refusal left 2.5 s of
        self.assertEqual((result["settled"], result["method"], result["snapshots"]),
                         (True, "poll", 2))
        self.assertEqual(result["elapsed_ms"], 1250)

    def test_the_quiet_path_times_its_one_read(self) -> None:
        now = [0.0]

        def quiet(*_args: Any, **_kwargs: Any) -> dict:
            now[0] += 0.75
            return {"quiet": True, "state": "quiet", "signals": []}

        def take() -> list[dict[str, Any]]:
            now[0] += 0.25
            return [{"ref": "n0", "role": "text", "text": "x"}]

        with mock.patch.object(ui_ios, "wait_quiet", side_effect=quiet), \
                mock.patch.object(ui, "snapshot", side_effect=lambda _t: take()):
            result = ui.settle(self.TARGET, timeout_ms=3000, quiet_ms=400,
                               clock=lambda: now[0], sleep=lambda s: None)
        self.assertEqual(result, {"settled": True, "method": "idb-quiet", "snapshots": 1,
                                  "changes": 0, "elapsed_ms": 1000, "dump_ms": 250,
                                  "stable_ms": 400, "quiet_state": "quiet"})


class UiQuietWrapperTests(InProcessBase):
    def test_answers(self) -> None:
        self.write_state(idb_quiet="quiet")
        self.assertEqual(ios_idb.ui_quiet(self.TARGET, 1500, 250),
                         {"quiet": True, "state": "quiet", "signals": []})
        self.write_state(idb_quiet="busy")
        self.assertEqual(ios_idb.ui_quiet(self.TARGET, 1500, 250),
                         {"quiet": False, "state": "busy",
                          "signals": ["animations_inactive", "run_loop_idle"]})
        self.write_state(idb_quiet="settling")
        self.assertEqual(ios_idb.ui_quiet(self.TARGET, 1500, 250),
                         {"quiet": False, "state": "settling", "signals": []})

    def test_seconds_are_never_zero(self) -> None:
        self.assertEqual([ios_idb._quiet_seconds(ms) for ms in (0, 1, 999, 1000, 1234, 60000)],  # noqa: SLF001
                         ["0.001", "0.001", "0.999", "1", "1.234", "60"])

    def test_a_hung_call_is_a_backend_failure(self) -> None:
        hung = subprocess.TimeoutExpired(["idb"], 6.0)
        with mock.patch.object(ios_idb.subprocess, "run", side_effect=hung):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_idb.ui_quiet(self.TARGET, 1000, 0)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertIn("did not answer within 6 s", caught.exception.message)

    def test_a_client_that_cannot_start_is_a_backend_failure(self) -> None:
        with mock.patch.object(ios_idb.subprocess, "run",
                               side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_idb.ui_quiet(self.TARGET, 1000, 0)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertIn("could not run", caught.exception.message)

    def test_exit_0_without_quiet_is_not_a_settle(self) -> None:
        silent = subprocess.CompletedProcess(["idb"], 0, stdout="", stderr="")
        with mock.patch.object(ios_idb, "run_idb", return_value=silent):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_idb.ui_quiet(self.TARGET, 1000, 0)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)

    def test_exit_1_without_timed_out_is_a_failure(self) -> None:
        """Only idb's `timed_out` answer is accepted; another exit 1 (the app
        exited, the stream ended) is a failure the caller falls back from."""
        self.write_state(idb_quiet="busy", idb_fail={"ui quiet": [1, EXITED]})
        with self.assertRaises(errors.AutonomError) as caught:
            ios_idb.ui_quiet(self.TARGET, 1000, 0)
        self.assertEqual((caught.exception.code, caught.exception.message),
                         (errors.BACKEND_FAILED, EXITED))

    def test_unimplemented_companion_is_unsupported(self) -> None:
        self.write_state(idb_quiet="quiet", idb_fail={
            "ui quiet": [1, "GRPCError: StatusCode.UNIMPLEMENTED: Method not found"]})
        for _ in range(2):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_idb.ui_quiet(self.TARGET, 1000, 0)
            self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(self.quiet_count(), 1)


RESET = "GRPCError: StatusCode.UNAVAILABLE: Connection reset by peer"
QUIET_LINE = '{"event": "state", "pid": 4242, "state": "quiet"}\n'


class QuietRetryBudgetTests(InProcessBase):
    """The AL-04 retry of `ui quiet` after a broken connection carries only
    what is left before the settle deadline, and is not sent when nothing is
    left (RISK-012: "timeout = remaining budget")."""

    NODES = [{"ref": "n0", "role": "text", "text": "x"}]

    def setUp(self) -> None:
        super().setUp()
        self.now = [0.0]
        self.sent: list[tuple[list[str], float | None]] = []
        self.pruned = 0

    def fake_run(self, quiet_answers: list[Any], prune_s: float = 0.0) -> Any:
        """`subprocess.run` for ios_idb: each `ui quiet` takes the next of
        ``quiet_answers`` — ``(seconds it takes, returncode, stdout,
        stderr)`` or an exception to raise — and a prune takes ``prune_s``."""
        answers = list(quiet_answers)

        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            if command[1:2] == ["list-targets"]:
                self.pruned += 1
                self.now[0] += prune_s
                return subprocess.CompletedProcess(command, 0)
            if command[1:3] != ["ui", "quiet"]:
                raise AssertionError(f"unexpected idb call {command}")
            self.sent.append((list(command[1:]), kwargs.get("timeout")))
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            took, returncode, stdout, stderr = answer
            self.now[0] += took
            return subprocess.CompletedProcess(command, returncode, stdout=stdout,
                                               stderr=stderr)

        return mock.patch.object(ios_idb.subprocess, "run", side_effect=run)

    def settle(self, timeout_ms: int, quiet_ms: int) -> dict[str, Any]:
        def take(_target: Any) -> list[dict[str, Any]]:
            self.now[0] += 0.25
            return self.NODES

        with mock.patch.object(ui, "snapshot", side_effect=take):
            return ui.settle(self.TARGET, timeout_ms=timeout_ms, quiet_ms=quiet_ms,
                             interval_ms=100, clock=lambda: self.now[0],
                             sleep=lambda s: self.now.__setitem__(0, self.now[0] + s))

    def test_the_retry_sends_only_the_budget_left(self) -> None:
        """The review's case: idb waits 4.75 s of a 5 s budget, the stream
        drops (after the connection, so only a read comes back), the prune
        takes 0.125 s: the retry asks for the last 0.125 s, not 5 s again."""
        with self.fake_run([(4.75, 1, "", RESET), (0.0625, 0, QUIET_LINE, "")], prune_s=0.125):
            result = self.settle(5000, 500)
        self.assertEqual(self.pruned, 1)
        self.assertEqual(self.sent, [
            (["ui", "quiet", "5", "--json", "--quiet-window-ms", "500", "--udid", UDID], 10.0),
            (["ui", "quiet", "0.125", "--json", "--quiet-window-ms", "500", "--udid", UDID],
             5.125),
        ])
        self.assertEqual((result["settled"], result["method"], result["snapshots"]),
                         (True, "idb-quiet", 1))
        # 4.75 + 0.125 + 0.0625 s of idb, then the one 0.25 s read
        self.assertEqual(result["elapsed_ms"], 5187)

    def test_no_retry_when_nothing_is_left(self) -> None:
        """The stream dropped after the budget had passed (inside idb's
        grace): nothing is sent again, and the fallback polls what is left —
        nothing, so one read and an unsettled answer."""
        with self.fake_run([(5.25, 1, "", RESET)], prune_s=0.125):
            result = self.settle(5000, 500)
        self.assertEqual(len(self.sent), 1, "ui quiet was not sent again")
        self.assertEqual((result["settled"], result["method"], result["snapshots"]),
                         (False, "poll", 1))
        self.assertEqual(result["elapsed_ms"], 5625)
        fallback = result["quiet_fallback"]
        self.assertEqual(fallback["code"], errors.IDB_COMPANION_UNAVAILABLE)
        self.assertTrue(fallback["error"].startswith(RESET), fallback)
        self.assertIn("not asked again: nothing was left of the 5000 ms budget",
                      fallback["error"])

    def test_a_prune_that_used_the_rest_leaves_no_retry(self) -> None:
        with self.fake_run([(0.5, 1, "", ENOENT)], prune_s=0.5):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_idb.ui_quiet(self.TARGET, 1000, 0, clock=lambda: self.now[0])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(caught.exception.code, errors.IDB_COMPANION_UNAVAILABLE)
        self.assertIn("not asked again", caught.exception.message)
        self.assertTrue(caught.exception.hint, "the hint survives the note")

    def test_without_a_deadline_the_budget_starts_at_the_call(self) -> None:
        self.now[0] = 100.0
        with self.fake_run([(0.5, 1, "", ENOENT), (0.0, 0, QUIET_LINE, "")], prune_s=0.25):
            answer = ios_idb.ui_quiet(self.TARGET, 1500, 200, clock=lambda: self.now[0])
        self.assertEqual(answer, {"quiet": True, "state": "quiet", "signals": []})
        self.assertEqual([(argv[2], limit) for argv, limit in self.sent],
                         [("1.5", 6.5), ("0.75", 5.75)])

    def test_a_hung_retry_names_what_it_was_sent(self) -> None:
        hung = subprocess.TimeoutExpired(["idb"], 7.0)
        with self.fake_run([(1.0, 1, "", RESET), hung]):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_idb.ui_quiet(self.TARGET, 3000, 0, clock=lambda: self.now[0])
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual([(argv[2], limit) for argv, limit in self.sent],
                         [("3", 8.0), ("2", 7.0)])
        self.assertIn("did not answer within 7 s (its own timeout was 2 s)",
                      caught.exception.message)

    def test_an_answer_is_never_resent(self) -> None:
        """idb's own "timed out" (exit 1, `timed_out`) is an answer: no prune,
        no second call, whatever the budget."""
        busy = ('{"event": "state", "pid": 4242, "signals": ["run_loop_idle"], '
                '"state": "busy"}\n{"event": "timed_out", "pid": 4242}\n')
        with self.fake_run([(1.0, 1, busy, "")]):
            result = self.settle(1000, 200)
        self.assertEqual((len(self.sent), self.pruned), (1, 0))
        self.assertEqual((result["settled"], result["method"], result["busy_signals"]),
                         (False, "idb-quiet", ["run_loop_idle"]))


class UnquietMessageTests(unittest.TestCase):
    def test_one_statement_and_one_hint_per_state(self) -> None:
        cases = {
            "busy": {"method": "idb-quiet", "quiet_state": "busy",
                     "busy_signals": ["run_loop_idle"], "snapshots": 1},
            "settling": {"method": "idb-quiet", "quiet_state": "settling", "snapshots": 1},
            None: {"method": "idb-quiet", "quiet_state": None, "snapshots": 1},
        }
        for state, result in cases.items():
            with self.subTest(state=state):
                error, hint = ui.unsettled_message(result, 2000, 500,
                                                   timeout_name="timeoutMs")
                self.assertTrue(error.startswith("idb reported the app"), error)
                self.assertNotIn("snapshot", error)
                self.assertEqual(hint.count("raise timeoutMs"), 1, hint)

    def test_poll_results_keep_their_messages(self) -> None:
        result = {"method": "poll", "snapshots": 1, "changes": 0, "dump_ms": 2000,
                  "stable_ms": 0}
        error, _hint = ui.unsettled_message(result, 3000, 500)
        self.assertEqual(error, "could not confirm the screen settled: only 1 snapshot fit "
                                "in 3000 ms (a dump takes ~2000 ms); raise --timeout-ms")


if __name__ == "__main__":
    unittest.main()
