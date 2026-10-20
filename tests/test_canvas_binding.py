"""Canvas children bound to an explicit session and admitted on the control
lease (contract section 4): the bridge (`autonom_canvas_bridge.py`) and the
tools process (`autonom_canvas_tools.py`).

Fakes only: `fake_adb.py` stands in for adb, the Android targets are made-up
emulator serials, and `AUTONOM_HOME` (sessions, lease files, mocks) and the
captures folder are temporary directories passed to every child process. No
device is used and nothing outside the temporary directories is written.
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

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BRIDGE = SCRIPTS / "autonom_canvas_bridge.py"
sys.path.insert(0, str(SCRIPTS))

import autonom_canvas_bridge as bridge  # noqa: E402
import autonom_canvas_tools as tools_mod  # noqa: E402
from autonom_lib import control, errors, session  # noqa: E402
from autonom_lib.network import proxy  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FAKE_ADB = str(ROOT / "tests/fakes/fake_adb.py")
SERIAL = "emulator-5590"
OTHER_SERIAL = "emulator-5592"
APP = "com.example.app"
CANVAS = "canvas:3277:41234"
AGENT = "agent:worker-1"
TOOL_ENV = ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_MITMDUMP",
            "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG", "AUTONOM_CONTROL_HOLDER",
            "AUTONOM_CONTROL_GENERATION", "AUTONOM_CONTROL_NOW_MS", "AUTONOM_TEST_MODE",
            "AUTONOM_CAPTURES_DIR", "XDG_STATE_HOME")
CONTROL_CODES = (errors.CONTROL_HELD, errors.CONTROL_GENERATION_STALE,
                 errors.CONTROL_LEASE_EXPIRED, errors.CONTROL_NOT_HELD)
SESSION_CODES = (errors.SESSION_STOPPED, errors.SESSION_TARGET_MISMATCH,
                 errors.SESSION_NOT_FOUND)
RUNNING = {"running": True, "pid": 4242, "port": 8899, "proxy_host": "127.0.0.1",
           "capture_bodies": False}

_REAL_STDIN = None


def setUpModule() -> None:
    """The consent gate prompts when stdin is a terminal; never look like one."""
    global _REAL_STDIN
    _REAL_STDIN = sys.stdin
    sys.stdin = io.StringIO()


def tearDownModule() -> None:
    if _REAL_STDIN is not None:
        sys.stdin = _REAL_STDIN


def android(serial: str = SERIAL) -> Target:
    return Target("android", serial, FAKE_ADB, {"serial": serial})


class BindingCase(EnvSandboxMixin, unittest.TestCase):
    """A sandboxed home and captures folder, fake adb, and call helpers."""

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.set_env(**{key: None for key in TOOL_ENV})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = self.tmp / "state.json"
        self.log = self.tmp / "calls.jsonl"
        self.state.write_text(json.dumps({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"],
                        [OTHER_SERIAL, "device", "product:sdk_gphone64_arm64"]]}),
            encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_CAPTURES_DIR=str(self.tmp / "captures"))
        token = session.select(None)
        self.addCleanup(session._SELECTED.reset, token)
        self.target = android()

    # --- helpers ---------------------------------------------------------------

    def start(self, serial: str = SERIAL, *, alongside: bool = False) -> dict:
        return session.start_session(FAKE_ADB, serial=serial, app_id=APP,
                                     alongside=alongside)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def input_calls(self) -> list[list[str]]:
        # `dumpsys input` reads the screen rotation; it sends no input.
        return [argv for argv in self.calls()
                if "input" in argv and "dumpsys" not in argv]

    def journal(self, record: dict) -> list[dict]:
        path = Path(record["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def current_bytes(self) -> bytes | None:
        path = session.artifacts_root() / "current.json"
        return path.read_bytes() if path.exists() else None

    def bridge_answer(self, message: dict, target: Target | None = None) -> dict:
        """One request through the bridge's own error envelope (as `main` does)."""
        try:
            result = bridge.dispatch(target or self.target, message)
            return {"id": message.get("id"), "ok": True, "result": result}
        except errors.AutonomError as exc:
            return {"id": message.get("id"), **exc.as_dict()}

    def tap(self, **envelope: object) -> dict:
        return self.bridge_answer({"id": 1, "op": "tap", "origin": "human",
                                   "payload": {"x": 10, "y": 20}, **envelope})

    def human_lease(self, holder: str = CANVAS) -> dict:
        return control.acquire("android", SERIAL, holder_id=holder, kind="human",
                               label="Canvas 3277", ttl_s=30)

    def assert_control_refusal(self, answer: dict, code: str) -> None:
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error_code"], code, answer)
        self.assertEqual(answer["capability"], "control.lease")
        for key in ("holder", "generation", "error", "hint"):
            self.assertIn(key, answer)
        self.assertEqual(answer["target_id"], SERIAL)

    def assert_session_refusal(self, answer: dict, code: str, session_id: str) -> None:
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error_code"], code, answer)
        self.assertEqual(answer["session_id"], session_id)
        self.assertIsInstance(answer["error"], str)
        self.assertTrue(answer.get("hint"))


# --- bridge: session binding -------------------------------------------------------


class BridgeSessionBindingTests(BindingCase):
    def test_bound_live_session_journals_into_that_session_only(self) -> None:
        primary = self.start(OTHER_SERIAL)
        beside = self.start(SERIAL, alongside=True)
        current_before = self.current_bytes()
        answer = self.tap(session_id=beside["session_id"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(len(self.input_calls()), 1)
        self.assertEqual([entry.get("verb") for entry in self.journal(beside)], ["ui tap"])
        self.assertEqual(self.journal(primary), [])
        self.assertEqual(self.current_bytes(), current_before)
        self.assertNotIn("control", answer["result"])

    def test_the_bound_current_session_also_journals(self) -> None:
        record = self.start()
        answer = self.tap(session_id=record["session_id"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([entry.get("verb") for entry in self.journal(record)], ["ui tap"])

    def test_stopped_bound_session_is_refused_with_zero_device_calls(self) -> None:
        record = self.start()
        session.stop_session(reap=False, session_id=record["session_id"])
        self.log.unlink(missing_ok=True)
        for op, payload in (("tap", {"x": 1, "y": 2}),
                            ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}),
                            ("key", {"key": "KEYCODE_BACK"}),
                            ("text", {"text": "hello"}),
                            ("screen-size", {})):
            with self.subTest(op=op):
                answer = self.bridge_answer({"id": 2, "op": op, "origin": "human",
                                             "payload": payload,
                                             "session_id": record["session_id"]})
                self.assert_session_refusal(answer, errors.SESSION_STOPPED,
                                            record["session_id"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journal(record), [])

    def test_a_session_stopped_only_on_disk_is_refused(self) -> None:
        record = self.start()
        path = Path(record["artifacts_dir"]) / "session.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["stopped_at"] = "2026-10-10T00:00:00Z"
        path.write_text(json.dumps(stored), encoding="utf-8")
        answer = self.tap(session_id=record["session_id"])
        self.assert_session_refusal(answer, errors.SESSION_STOPPED, record["session_id"])
        self.assertEqual(self.input_calls(), [])

    def test_a_session_of_another_target_is_refused_with_zero_device_calls(self) -> None:
        record = self.start(OTHER_SERIAL)
        self.log.unlink(missing_ok=True)
        answer = self.tap(session_id=record["session_id"])
        self.assert_session_refusal(answer, errors.SESSION_TARGET_MISMATCH,
                                    record["session_id"])
        self.assertEqual(answer["target_id"], OTHER_SERIAL)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journal(record), [])

    def test_an_unknown_or_malformed_session_id_is_not_found(self) -> None:
        for value in ("s_0000000000", "../etc", "", "s_bad/../x"):
            with self.subTest(session_id=value):
                answer = self.tap(session_id=value)
                self.assert_session_refusal(answer, errors.SESSION_NOT_FOUND, value)
        self.assertEqual(self.calls(), [])

    def test_a_non_string_session_id_is_an_invalid_message(self) -> None:
        for value in (7, ["s_1"], {"id": "s_1"}, True):
            with self.subTest(session_id=value):
                answer = self.tap(session_id=value)
                self.assertEqual(answer["error_code"], errors.FLOW_COMMAND_INVALID, answer)
        self.assertEqual(self.calls(), [])

    def test_a_refused_record_journals_nothing(self) -> None:
        record = self.start()
        session.stop_session(reap=False, session_id=record["session_id"])
        answer = self.bridge_answer({
            "id": 3, "op": "record", "origin": "human", "session_id": record["session_id"],
            "payload": {"kind": "key", "key": "KEYCODE_BACK", "transport": "scrcpy"}})
        self.assert_session_refusal(answer, errors.SESSION_STOPPED, record["session_id"])
        self.assertEqual(self.journal(record), [])

    def test_without_session_id_the_lookup_is_todays(self) -> None:
        record = self.start()
        for envelope in ({}, {"session_id": None, "control": None}):
            with self.subTest(envelope=envelope):
                answer = self.tap(**envelope)
                self.assertTrue(answer["ok"], answer)
                self.assertEqual(set(answer["result"]), {"ok", "x", "y", "detail"})
        self.assertEqual([entry.get("verb") for entry in self.journal(record)],
                         ["ui tap", "ui tap"])
        self.assertEqual(len(self.input_calls()), 2)
        self.assertFalse((self.home / "control").exists())

    def test_without_any_session_the_tap_still_runs_unjournaled(self) -> None:
        answer = self.tap()
        self.assertEqual(answer, {"id": 1, "ok": True, "result": {"ok": True, "x": 10, "y": 20}})
        self.assertEqual(len(self.input_calls()), 1)


# --- bridge: pre-test guard refusals -----------------------------------------------


class BridgeGuardRefusalTests(BindingCase):
    def test_a_damaged_pretest_refusal_is_journaled_like_a_spent_budget(self) -> None:
        record = self.start()
        for code in (errors.BUDGET_EXHAUSTED, errors.PRETEST_DAMAGED):
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                before = len(self.journal(record))
                refusal = errors.AutonomError(code, f"{code}: no input was sent")
                with mock.patch("autonom_lib.pretest.guard.before_mutation",
                                side_effect=refusal):
                    answer = self.bridge_answer({
                        "id": 4, "op": "tap", "origin": "agent",
                        "payload": {"x": 10, "y": 20},
                        "session_id": record["session_id"]})
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(answer["error_code"], code)
                self.assertEqual(self.input_calls(), [])
                added = self.journal(record)[before:]
                self.assertEqual(
                    [(entry.get("verb"), entry.get("ok"), entry.get("error_code"),
                      entry.get("origin")) for entry in added],
                    [("ui tap", False, code, "agent")])

    def test_every_guard_refusal_code_is_journaled(self) -> None:
        self.assertEqual(set(bridge.GUARD_REFUSALS),
                         {errors.BUDGET_EXHAUSTED, errors.FORBIDDEN_ACTION,
                          errors.APP_NOT_ALLOWED, errors.PRETEST_DAMAGED})


# --- bridge: control admission -----------------------------------------------------


class BridgeAdmissionTests(BindingCase):
    def test_a_foreign_human_lease_refuses_every_actuating_op_with_zero_calls(self) -> None:
        record = self.start()
        lease = self.human_lease()
        self.log.unlink(missing_ok=True)
        claims = (None, {"holder_id": AGENT}, {"holder_id": AGENT, "generation": None})
        for op, payload in (("tap", {"x": 1, "y": 2}),
                            ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}),
                            ("key", {"key": "KEYCODE_BACK"}),
                            ("text", {"text": "hello"})):
            for claim in claims:
                with self.subTest(op=op, claim=claim):
                    answer = self.bridge_answer({"id": 4, "op": op, "origin": "agent",
                                                 "payload": payload, "control": claim})
                    self.assert_control_refusal(answer, errors.CONTROL_HELD)
                    self.assertEqual(answer["generation"], lease["generation"])
                    self.assertEqual(answer["holder"]["id"], CANVAS)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journal(record), [])

    def test_the_holders_claim_is_admitted_and_the_result_carries_control(self) -> None:
        record = self.start()
        lease = self.human_lease()
        for claim in ({"holder_id": CANVAS, "generation": lease["generation"]},
                      {"holder_id": CANVAS}):
            with self.subTest(claim=claim):
                answer = self.tap(control=claim)
                self.assertTrue(answer["ok"], answer)
                attribution = answer["result"]["control"]
                self.assertEqual(attribution["generation"], lease["generation"])
                self.assertRegex(attribution["operation_id"], r"^op_[0-9a-f]{12}$")
                detail = json.loads((Path(record["artifacts_dir"])
                                     / answer["result"]["detail"]).read_text(encoding="utf-8"))
                self.assertEqual(detail["payload"]["control"] if "payload" in detail
                                 else detail["control"], attribution)
        self.assertEqual(len(self.input_calls()), 2)

    def test_text_and_key_carry_control_too(self) -> None:
        lease = self.human_lease()
        claim = {"holder_id": CANVAS, "generation": lease["generation"]}
        for op, payload in (("text", {"text": "hi"}), ("key", {"key": "KEYCODE_HOME"}),
                            ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4})):
            with self.subTest(op=op):
                answer = self.bridge_answer({"id": 5, "op": op, "origin": "human",
                                             "payload": payload, "control": claim})
                self.assertTrue(answer["ok"], answer)
                self.assertEqual(answer["result"]["control"]["generation"], lease["generation"])

    def test_a_stale_generation_is_refused(self) -> None:
        first = self.human_lease()
        control.release("android", SERIAL, holder_id=CANVAS, generation=first["generation"])
        second = self.human_lease()
        self.assertEqual(second["generation"], first["generation"] + 1)
        self.log.unlink(missing_ok=True)
        answer = self.tap(control={"holder_id": CANVAS, "generation": first["generation"]})
        self.assert_control_refusal(answer, errors.CONTROL_GENERATION_STALE)
        self.assertEqual(answer["generation"], second["generation"])
        self.assertEqual(answer["claimed_holder"], CANVAS)
        self.assertEqual(answer["claimed_generation"], first["generation"])
        self.assertEqual(self.calls(), [])

    def test_a_claim_without_a_lease_file_is_not_held(self) -> None:
        answer = self.tap(control={"holder_id": AGENT, "generation": 1})
        self.assert_control_refusal(answer, errors.CONTROL_NOT_HELD)
        self.assertIsNone(answer["holder"])
        self.assertIsNone(answer["generation"])
        self.assertEqual(self.calls(), [])

    def test_an_expired_own_lease_is_refused(self) -> None:
        self.set_env(AUTONOM_TEST_MODE="1", AUTONOM_CONTROL_NOW_MS="1000000")
        lease = self.human_lease()
        self.set_env(AUTONOM_CONTROL_NOW_MS=str(lease["lease"]["expires_at_ms"]))
        answer = self.tap(control={"holder_id": CANVAS, "generation": lease["generation"]})
        self.assert_control_refusal(answer, errors.CONTROL_LEASE_EXPIRED)
        self.assertEqual(self.input_calls(), [])

    def test_a_free_lease_admits_unclaimed_input_without_a_control_key(self) -> None:
        lease = self.human_lease()
        control.release("android", SERIAL, holder_id=CANVAS, generation=lease["generation"])
        answer = self.tap()
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("control", answer["result"])

    def test_record_and_screen_size_are_never_refused_by_a_lease(self) -> None:
        record = self.start()
        self.human_lease()
        for kind_payload in ({"kind": "key", "key": "KEYCODE_BACK", "transport": "scrcpy"},
                             {"kind": "control", "mode": "lost", "owner": "human"},
                             {"kind": "control", "mode": "idle"}):
            with self.subTest(payload=kind_payload):
                answer = self.bridge_answer({"id": 6, "op": "record", "origin": "agent",
                                             "payload": kind_payload,
                                             "control": {"holder_id": AGENT, "generation": 1}})
                self.assertTrue(answer["ok"], answer)
                self.assertNotIn("control", answer["result"])
        size = self.bridge_answer({"id": 7, "op": "screen-size", "origin": "agent"})
        self.assertTrue(size["ok"], size)
        self.assertEqual(len(self.journal(record)), 3)

    def test_lost_and_idle_are_control_modes_and_owners_are_unchanged(self) -> None:
        self.assertEqual(bridge.CONTROL_MODES,
                         ("pause", "resume", "takeover", "release", "lost", "idle"))
        self.assertEqual(bridge.CONTROL_OWNERS, ("shared", "human", "agent", "replay", "system"))
        answer = self.bridge_answer({"id": 8, "op": "record", "origin": "human",
                                     "payload": {"kind": "control", "mode": "steal"}})
        self.assertEqual(answer["error_code"], errors.FLOW_COMMAND_INVALID)

    def test_a_malformed_control_object_is_refused_before_any_device_call(self) -> None:
        self.start()
        for value in ("canvas:1:2", 3, [CANVAS], {}, {"holder_id": ""},
                      {"holder_id": "bad id"}, {"holder_id": "x" * 129},
                      {"holder_id": CANVAS, "generation": 0},
                      {"holder_id": CANVAS, "generation": "3"},
                      {"holder_id": CANVAS, "generation": True},
                      {"holder_id": CANVAS, "generation": 1.5},
                      {"generation": 1}):
            with self.subTest(control=value):
                answer = self.tap(control=value)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(answer["error_code"], errors.FLOW_COMMAND_INVALID, answer)
                answer = self.bridge_answer({"id": 9, "op": "record", "origin": "human",
                                             "control": value, "payload": {
                                                 "kind": "key", "key": "KEYCODE_BACK"}})
                self.assertEqual(answer["error_code"], errors.FLOW_COMMAND_INVALID, answer)
        self.assertEqual(self.input_calls(), [])

    def test_the_session_check_comes_before_the_lease_check(self) -> None:
        record = self.start()
        session.stop_session(reap=False, session_id=record["session_id"])
        self.human_lease()
        answer = self.tap(session_id=record["session_id"])
        self.assertEqual(answer["error_code"], errors.SESSION_STOPPED)


# --- bridge: control ops -----------------------------------------------------------


class BridgeControlOpsTests(BindingCase):
    def op(self, op: str, payload: object = None, **envelope: object) -> dict:
        message = {"id": 11, "op": op, "origin": "human", **envelope}
        if payload is not None:
            message["payload"] = payload
        return self.bridge_answer(message)

    def test_acquire_renew_release_status_round_trip_keeps_the_generation(self) -> None:
        self.assertEqual(self.op("control-status", {}),
                         {"id": 11, "ok": True, "result": {"ok": True, "lease": None}})
        self.assertFalse((self.home / "control").exists())
        acquired = self.op("control-acquire", {"holder_id": CANVAS, "label": "Canvas 3277",
                                               "kind": "human", "ttl_s": 30, "takeover": False,
                                               "session_id": "s_0123456789"})
        self.assertTrue(acquired["ok"], acquired)
        grant = acquired["result"]
        self.assertTrue(grant["acquired"])
        self.assertEqual(grant["generation"], 1)
        self.assertEqual(grant["lease"]["holder"],
                         {"kind": "human", "id": CANVAS, "label": "Canvas 3277"})
        self.assertEqual(grant["lease"]["session_id"], "s_0123456789")
        self.assertEqual(grant["lease"]["ttl_ms"], 30_000)

        renewed = self.op("control-renew", {"holder_id": CANVAS, "generation": 1, "ttl_s": 30})
        self.assertTrue(renewed["ok"], renewed)
        self.assertTrue(renewed["result"]["renewed"])
        self.assertEqual(renewed["result"]["generation"], 1)

        status = self.op("control-status", {})["result"]
        self.assertEqual(status["lease"]["state"], "held")
        self.assertEqual(status["lease"]["holder"]["id"], CANVAS)

        released = self.op("control-release", {"holder_id": CANVAS, "generation": 1})
        self.assertTrue(released["ok"], released)
        self.assertTrue(released["result"]["released"])
        status = self.op("control-status")["result"]["lease"]
        self.assertEqual((status["state"], status["generation"], status["holder"]),
                         ("free", 1, None))

        again = self.op("control-acquire", {"holder_id": CANVAS, "label": "Canvas 3277",
                                            "kind": "human", "ttl_s": 30})
        self.assertEqual(again["result"]["generation"], 2)

    def test_the_kind_defaults_to_human(self) -> None:
        grant = self.op("control-acquire", {"holder_id": CANVAS})["result"]
        self.assertEqual(grant["lease"]["holder"]["kind"], "human")
        self.assertEqual(grant["lease"]["ttl_ms"], control.HUMAN_TTL_S * 1000)

    def test_control_ops_journal_nothing_and_never_touch_the_device(self) -> None:
        record = self.start()
        self.log.unlink(missing_ok=True)
        self.op("control-acquire", {"holder_id": CANVAS, "kind": "human"})
        self.op("control-renew", {"holder_id": CANVAS, "generation": 1})
        self.op("control-status", {})
        self.op("control-release", {"holder_id": CANVAS, "generation": 1})
        self.assertEqual(self.journal(record), [])
        self.assertEqual(self.calls(), [])

    def test_control_ops_ignore_a_stopped_bound_session(self) -> None:
        record = self.start()
        session.stop_session(reap=False, session_id=record["session_id"])
        answer = self.op("control-acquire", {"holder_id": CANVAS},
                         session_id=record["session_id"])
        self.assertTrue(answer["ok"], answer)
        answer = self.op("control-release", {"holder_id": CANVAS, "generation": 1},
                         session_id=record["session_id"])
        self.assertTrue(answer["ok"], answer)

    def test_takeover_and_its_refusals(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, kind="agent", ttl_s=60)
        held = self.op("control-acquire", {"holder_id": CANVAS, "kind": "human"})
        self.assert_control_refusal(held, errors.CONTROL_HELD)
        self.assertEqual(held["holder"]["id"], AGENT)
        taken = self.op("control-acquire", {"holder_id": CANVAS, "kind": "human",
                                            "takeover": True})
        self.assertTrue(taken["ok"], taken)
        self.assertTrue(taken["result"]["taken_over"])
        self.assertEqual(taken["result"]["generation"], 2)
        self.assertEqual(taken["result"]["lease"]["previous_holder"]["id"], AGENT)
        refused = self.op("control-acquire", {"holder_id": AGENT, "kind": "agent",
                                              "takeover": True})
        self.assert_control_refusal(refused, errors.CONTROL_HELD)
        self.assertEqual(refused["takeover_requires"], "human")

    def test_release_and_renew_refusals(self) -> None:
        answer = self.op("control-release", {"holder_id": CANVAS, "generation": 1})
        self.assert_control_refusal(answer, errors.CONTROL_NOT_HELD)
        answer = self.op("control-renew", {"holder_id": CANVAS, "generation": 1})
        self.assert_control_refusal(answer, errors.CONTROL_NOT_HELD)
        self.assertFalse((self.home / "control").exists())
        self.op("control-acquire", {"holder_id": CANVAS})
        answer = self.op("control-renew", {"holder_id": CANVAS, "generation": 5})
        self.assert_control_refusal(answer, errors.CONTROL_GENERATION_STALE)
        answer = self.op("control-release", {"holder_id": AGENT, "generation": 1})
        self.assert_control_refusal(answer, errors.CONTROL_NOT_HELD)

    def test_invalid_control_op_payloads(self) -> None:
        for op, payload, code in (
                ("control-acquire", {}, errors.INVALID_VALUE),
                ("control-acquire", {"holder_id": "bad id"}, errors.INVALID_VALUE),
                ("control-acquire", {"holder_id": CANVAS, "kind": "robot"}, errors.INVALID_VALUE),
                ("control-acquire", {"holder_id": CANVAS, "ttl_s": 1}, errors.INVALID_VALUE),
                ("control-acquire", {"holder_id": CANVAS, "label": "a\nb"}, errors.INVALID_VALUE),
                ("control-acquire", {"holder_id": CANVAS, "takeover": "yes"},
                 errors.FLOW_COMMAND_INVALID),
                ("control-acquire", {"holder_id": CANVAS, "session_id": "a b"},
                 errors.INVALID_VALUE),
                ("control-renew", {"holder_id": CANVAS}, errors.INVALID_VALUE),
                ("control-release", {"holder_id": CANVAS, "generation": 0},
                 errors.INVALID_VALUE),
                ("control-status", [1], errors.FLOW_COMMAND_INVALID)):
            with self.subTest(op=op, payload=payload):
                answer = self.op(op, payload)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(answer["error_code"], code, answer)
        self.assertFalse(list((self.home / "control").glob("*.json"))
                         if (self.home / "control").exists() else [])

    def test_control_ops_still_need_a_valid_origin(self) -> None:
        answer = self.bridge_answer({"id": 12, "op": "control-status", "origin": "root"})
        self.assertEqual(answer["error_code"], errors.FLOW_COMMAND_INVALID)


# --- bridge: the real process over NDJSON ------------------------------------------


class BridgeProcessTests(BindingCase):
    def run_bridge(self, messages: list[dict]) -> list[dict]:
        env = {key: value for key, value in os.environ.items() if key not in TOOL_ENV}
        env.update({"AUTONOM_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
                    "AUTONOM_FAKE_STATE": str(self.state), "AUTONOM_FAKE_LOG": str(self.log)})
        stdin = "".join(json.dumps(message) + "\n" for message in messages)
        completed = subprocess.run(
            [sys.executable, str(BRIDGE), "--platform", "android", "--target", SERIAL,
             "--tool", FAKE_ADB],
            input=stdin, env=env, cwd=self.tmp, capture_output=True, text=True,
            timeout=120, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]

    def test_binding_and_lease_over_the_wire(self) -> None:
        record = self.start()
        stopped = self.start(OTHER_SERIAL, alongside=True)
        session.stop_session(reap=False, session_id=stopped["session_id"])
        self.log.unlink(missing_ok=True)
        answers = self.run_bridge([
            {"id": 1, "op": "tap", "origin": "agent", "payload": {"x": 1, "y": 1}},
            {"id": 2, "op": "control-acquire", "origin": "human",
             "payload": {"holder_id": CANVAS, "label": "Canvas 3277", "kind": "human",
                         "ttl_s": 30, "takeover": False}},
            {"id": 3, "op": "tap", "origin": "agent", "payload": {"x": 2, "y": 2}},
            {"id": 4, "op": "tap", "origin": "human", "payload": {"x": 3, "y": 3},
             "session_id": record["session_id"],
             "control": {"holder_id": CANVAS, "generation": 1}},
            {"id": 5, "op": "tap", "origin": "human", "payload": {"x": 4, "y": 4},
             "session_id": stopped["session_id"],
             "control": {"holder_id": CANVAS, "generation": 1}},
            {"id": 6, "op": "tap", "origin": "human", "payload": {"x": 5, "y": 5},
             "control": {"holder_id": CANVAS, "generation": "1"}},
            {"id": 7, "op": "control-release", "origin": "human",
             "payload": {"holder_id": CANVAS, "generation": 1}},
            {"id": 8, "op": "control-status", "origin": "human", "payload": {}},
        ])
        self.assertEqual([answer["id"] for answer in answers], list(range(1, 9)))
        self.assertTrue(answers[0]["ok"], answers[0])
        self.assertNotIn("control", answers[0]["result"])
        self.assertTrue(answers[1]["ok"] and answers[1]["result"]["acquired"], answers[1])
        self.assertEqual(answers[2]["error_code"], errors.CONTROL_HELD)
        self.assertEqual(answers[2]["capability"], "control.lease")
        self.assertEqual(answers[2]["holder"]["id"], CANVAS)
        self.assertEqual(answers[2]["generation"], 1)
        self.assertTrue(answers[3]["ok"], answers[3])
        self.assertEqual(answers[3]["result"]["control"]["generation"], 1)
        self.assertEqual(answers[4]["error_code"], errors.SESSION_STOPPED)
        self.assertEqual(answers[4]["session_id"], stopped["session_id"])
        self.assertEqual(answers[5]["error_code"], errors.FLOW_COMMAND_INVALID)
        self.assertTrue(answers[6]["result"]["released"], answers[6])
        self.assertEqual(answers[7]["result"]["lease"]["generation"], 1)
        self.assertIsNone(answers[7]["result"]["lease"]["holder"])
        taps = [argv for argv in self.input_calls() if "tap" in argv]
        self.assertEqual(len(taps), 2)  # requests 1 and 4 only
        self.assertEqual([entry.get("verb") for entry in self.journal(record)],
                         ["ui tap", "ui tap"])


# --- tools process -----------------------------------------------------------------


class ToolsBindingTests(BindingCase):
    def setUp(self) -> None:
        super().setUp()
        self.tools = tools_mod.Tools(self.target, device_name="Pixel 8")
        self.addCleanup(self.tools.shutdown)

    def call(self, op: str, payload: object = None, **envelope: object) -> dict:
        message = {"id": 7, "op": op, "origin": "human", **envelope}
        if payload is not None:
            message["payload"] = payload
        return json.loads(tools_mod.encode(tools_mod.respond(self.tools, json.dumps(message))))

    def test_leased_tools_ops_are_the_contract_list(self) -> None:
        self.assertEqual(set(tools_mod.LEASED_TOOLS_OPS), {
            "permissions.set", "location.set", "location.clear", "simulate",
            "network.attach", "network.detach", "network.stop", "app.install",
            "app.launch", "app.open_url", "app.locale"})
        self.assertEqual(len(tools_mod.LEASED_TOOLS_OPS), 11)
        self.assertTrue(set(tools_mod.LEASED_TOOLS_OPS) <= set(tools_mod.OPS))
        never = {"network.start", "capture.screenshot", "record.start", "record.stop",
                 *tools_mod.READ_OPS, *tools_mod.ACTION_READ_OPS,
                 *(op for op in tools_mod.OPS if op.startswith(("mocks.", "captures.")))}
        self.assertFalse(never & set(tools_mod.LEASED_TOOLS_OPS))

    def test_app_launch_is_refused_under_a_foreign_lease_with_zero_calls(self) -> None:
        record = self.start()
        self.human_lease()
        self.log.unlink(missing_ok=True)
        for op, payload in (("app.launch", {"app_id": APP}),
                            ("app.open_url", {"url": "myapp://x"}),
                            ("permissions.set", {"action": "grant", "app_id": APP,
                                                 "permission": "camera"}),
                            ("location.set", {"latitude": 1.0, "longitude": 2.0})):
            with self.subTest(op=op):
                answer = self.call(op, payload)
                self.assert_control_refusal(answer, errors.CONTROL_HELD)
                self.assertEqual(answer["id"], 7)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journal(record), [])

    def test_host_side_and_observation_ops_are_not_refused_by_a_lease(self) -> None:
        self.start()
        self.human_lease()
        answer = self.call("mocks.add", {"url_glob": "https://api.example.com/x",
                                         "status": 200})
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("control", answer["result"])
        with mock.patch.object(proxy, "start", return_value=dict(RUNNING)):
            answer = self.call("network.start", {"acknowledged": True})
        self.assertTrue(answer["ok"], answer)
        answer = self.call("capture.screenshot", {})
        self.assertTrue(answer["ok"], answer)
        for op in ("context", "mocks.list", "captures.list", "record.status"):
            with self.subTest(op=op):
                self.assertTrue(self.call(op)["ok"])

    def test_the_holders_claim_admits_a_leased_tools_op(self) -> None:
        record = self.start()
        lease = self.human_lease()
        answer = self.call("app.launch", {"app_id": APP},
                           control={"holder_id": CANVAS, "generation": lease["generation"]},
                           session_id=record["session_id"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["control"]["generation"], lease["generation"])
        entry = self.journal(record)[-1]
        self.assertEqual(entry["verb"], "canvas app.launch")
        self.assertTrue(entry["ok"])

    def test_without_a_lease_tools_answers_are_todays(self) -> None:
        self.start()
        answer = self.call("app.launch", {"app_id": APP})
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("control", answer["result"])
        self.assertFalse((self.home / "control").exists())

    def test_context_names_the_bound_session(self) -> None:
        self.assertIsNone(self.call("context")["result"]["session_id"])
        primary = self.start(OTHER_SERIAL)
        beside = self.start(SERIAL, alongside=True)
        context = self.call("context", session_id=beside["session_id"])["result"]
        self.assertEqual(context["session_id"], beside["session_id"])
        self.assertEqual(context["session"]["id"], beside["session_id"])
        self.assertEqual(self.call("context")["result"]["session_id"], beside["session_id"])
        answer = self.call("context", session_id=primary["session_id"])
        self.assert_session_refusal(answer, errors.SESSION_TARGET_MISMATCH,
                                    primary["session_id"])

    def test_a_stopped_bound_session_refuses_device_and_read_ops(self) -> None:
        record = self.start()
        session.stop_session(reap=False, session_id=record["session_id"])
        self.log.unlink(missing_ok=True)
        for op, payload in (("app.launch", {"app_id": APP}), ("context", None),
                            ("location.get", None), ("mocks.list", None)):
            with self.subTest(op=op):
                answer = self.call(op, payload, session_id=record["session_id"])
                self.assert_session_refusal(answer, errors.SESSION_STOPPED,
                                            record["session_id"])
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.tools.bound_session_id)

    def test_a_malformed_envelope_is_refused(self) -> None:
        self.start()
        for envelope in ({"control": "canvas"}, {"control": {"holder_id": 3}},
                         {"control": {"holder_id": CANVAS, "generation": -1}},
                         {"session_id": 9}):
            with self.subTest(envelope=envelope):
                answer = self.call("app.launch", {"app_id": APP}, **envelope)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(answer["error_code"], errors.FLOW_COMMAND_INVALID, answer)
                self.assertIn("hint", answer)
                self.assertIn("capability", answer)
        self.assertEqual(self.calls(), [])

    def test_a_stale_tools_claim_is_refused(self) -> None:
        first = self.human_lease()
        control.release("android", SERIAL, holder_id=CANVAS, generation=first["generation"])
        self.human_lease()
        answer = self.call("app.launch", {"app_id": APP},
                           control={"holder_id": CANVAS, "generation": first["generation"]})
        self.assert_control_refusal(answer, errors.CONTROL_GENERATION_STALE)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
