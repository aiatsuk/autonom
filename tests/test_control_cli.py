"""The control lease on the CLI (contract 5.1-5.4, 5.6).

- `autonom control status|acquire|release|renew` round trips with `env` output;
- every MUTATING verb (and every mutating BY ACTION action) is refused
  `control_held` under a foreign lease with zero fake-device calls; the
  refusal envelope names the holder, the generation and the capability; the
  journal records it with `ok: false`, the code and the operation id;
- READONLY verbs, `session stop` and unclaimed callers of an expired lease are
  admitted; a matching claim (flags or env, before or after the verb) is
  admitted with a `control` object; a stale claim is refused;
- `ui tree` skips its implicit accessibility recovery under a foreign lease;
- claim, label, ttl and operation id validation; `canvas serve --control-idle`;
- without a lease file the output is byte for byte the one of before;
- no existing option changes meaning (the abbreviation table of 5.1).

Every CLI run is a subprocess against `tests/fakes/fake_adb.py` (or in process
with the one tool call patched), in a private AUTONOM_HOME. No real device.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
SERIAL = "emulator-5590"
SECOND = "emulator-5592"
HUMAN = "canvas:3277:4242"
APP = "com.example.app"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

# `ui tap --x 1 --y 1` on the fake adb, as Autonom 0.35.1 (9acc89c) printed it.
RECORDED_UI_TAP = (
    '{\n  "ok": true,\n  "x": 1,\n  "y": 1,\n  "ref": null,\n  "backend": "adb",\n'
    '  "platform": "android",\n  "target_id": "emulator-5590",\n'
    '  "serial": "emulator-5590"\n}\n')


def load_cli(name: str = "autonom_cli_control"):
    spec = importlib.util.spec_from_file_location(name, CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ControlCliCase(EnvSandboxMixin, unittest.TestCase):
    """A private AUTONOM_HOME, a fake adb with two emulators, nothing real on PATH."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".autonom").mkdir()
        self.home = self.root / "home"
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.write_state()
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_IDB=None, AUTONOM_AXE=None, AUTONOM_IOS_HID=None,
                     AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None,
                     AUTONOM_TEST_MODE=None, AUTONOM_CONTROL_NOW_MS=None)
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)
        control_mod.end_invocation()
        self.addCleanup(control_mod.end_invocation)

    def write_state(self, **extra) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"],
                             [SECOND, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(UI_DUMP)}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def run_cli(self, *argv: str, env: dict | None = None) -> tuple[int, dict, str]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root,
            env={**self.env, **(env or {})}, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        return completed.returncode, json.loads(stream), completed.stdout

    def in_process(self, cli, *argv: str) -> tuple[int, dict]:
        """`main()` of a loaded CLI module in this process (no interpreter
        start: the refusal samples stay fast on a loaded machine)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        stream = out.getvalue() if out.getvalue().strip() else err.getvalue()
        self.assertNotIn("Traceback", stream)
        return code, json.loads(stream)

    def android(self, *argv: str, env: dict | None = None, serial: str = SERIAL
                ) -> tuple[int, dict]:
        code, payload, _ = self.run_cli("--adb", str(FAKE_ADB), "--serial", serial, *argv,
                                        env=env)
        return code, payload

    def start(self, serial: str = SERIAL, *extra: str) -> dict:
        code, payload = self.android("session", "start", "--app-id", APP, *extra,
                                     serial=serial)
        self.assertEqual(code, 0, payload)
        return payload["session"]

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def clear_calls(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def human_lease(self, serial: str = SERIAL) -> dict:
        # A long ttl: a loaded parallel run must not see the lease expire
        # (and admit) half-way through a test.
        return control_mod.acquire("android", serial, holder_id=HUMAN, kind="human",
                                   label="Canvas 3277", ttl_s=3600)

    def journal(self, session: dict) -> list[dict]:
        path = Path(session["artifacts_dir"]) / journal_mod.JOURNAL_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def assert_refusal(self, payload: dict, code: str = errors.CONTROL_HELD) -> None:
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], code, payload)
        self.assertEqual(payload["capability"], "control.lease")
        self.assertEqual(payload["platform"], "android")
        self.assertTrue(payload["hint"])
        self.assertIn("generation", payload)
        self.assertIn("holder", payload)


class ControlVerbTests(ControlCliCase):

    def test_acquire_status_renew_release_round_trip(self) -> None:
        code, granted = self.android("control", "acquire", "--holder", "agent:a",
                                     "--label", "worker a", "--ttl", "1200")
        self.assertEqual(code, 0, granted)
        self.assertTrue(granted["acquired"])
        self.assertFalse(granted["renewed"])
        self.assertEqual(granted["generation"], 1)
        self.assertEqual(granted["target_id"], SERIAL)
        self.assertEqual(granted["platform"], "android")
        self.assertEqual(granted["env"], {"AUTONOM_CONTROL_HOLDER": "agent:a",
                                          "AUTONOM_CONTROL_GENERATION": "1"})
        self.assertEqual(granted["lease"]["holder"],
                         {"kind": "agent", "id": "agent:a", "label": "worker a"})
        self.assertEqual(granted["lease"]["ttl_ms"], 1_200_000)

        code, status = self.android("control", "status")
        self.assertEqual(code, 0, status)
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["lease"]["state"], "held")
        self.assertEqual(status["lease"]["holder"]["id"], "agent:a")
        self.assertEqual(status["leases"], [status["lease"]])

        code, renewed = self.android("control", "renew", "--holder", "agent:a",
                                     "--generation", "1", "--ttl", "600")
        self.assertEqual(code, 0, renewed)
        self.assertTrue(renewed["renewed"])
        self.assertEqual(renewed["generation"], 1)
        self.assertEqual(renewed["env"]["AUTONOM_CONTROL_GENERATION"], "1")

        code, released = self.android("control", "release", "--holder", "agent:a",
                                      "--generation", "1")
        self.assertEqual(code, 0, released)
        self.assertTrue(released["released"])
        self.assertIsNone(released["lease"]["holder"])

        code, status = self.android("control", "status")
        self.assertEqual(status["lease"]["state"], "free")
        self.assertEqual(status["lease"]["generation"], 1)
        code, again = self.android("control", "acquire", "--holder", "agent:b")
        self.assertEqual(again["generation"], 2, "the generation survives a release")

        # No target and no session: every lease file; --all likewise.
        code, everything, _ = self.run_cli("control", "status")
        self.assertEqual(code, 0, everything)
        self.assertEqual(everything["count"], 1)
        self.assertNotIn("lease", everything)
        code, everything = self.android("control", "status", "--all")
        self.assertEqual(everything["count"], 1)
        self.assertEqual(self.calls(), [], "control verbs never touch the device")

    def test_the_claim_from_the_environment_names_the_holder(self) -> None:
        env = {"AUTONOM_CONTROL_HOLDER": "agent:env"}
        code, granted = self.android("control", "acquire", "--ttl", "3600", env=env)
        self.assertEqual(code, 0, granted)
        self.assertEqual(granted["lease"]["holder"]["id"], "agent:env")
        env["AUTONOM_CONTROL_GENERATION"] = "1"
        code, renewed = self.android("control", "renew", env=env)
        self.assertEqual(code, 0, renewed)
        code, released = self.android("control", "release", env=env)
        self.assertEqual(code, 0, released)

    def test_acquire_records_the_bound_session(self) -> None:
        session = self.start()
        code, granted = self.android("control", "acquire", "--holder", "agent:a")
        self.assertEqual(code, 0, granted)
        self.assertEqual(granted["lease"]["session_id"], session["session_id"])
        entries = [entry for entry in self.journal(session)
                   if entry.get("verb") == "control acquire"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["ok"])

    def test_a_missing_holder_is_a_usage_error(self) -> None:
        code, payload = self.android("control", "acquire")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.USAGE_ERROR)

    def test_invalid_ttl_holder_and_label_create_no_lease(self) -> None:
        for argv in (("--holder", "agent:a", "--ttl", "4"),
                     ("--holder", "agent:a", "--ttl", "3601"),
                     ("--holder", "bad holder!"),
                     ("--holder", "agent:a", "--label", "x" * 81)):
            with self.subTest(argv=argv):
                code, payload = self.android("control", "acquire", *argv)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
                self.assertFalse((self.home / "control").exists()
                                 and any((self.home / "control").glob("*.json")))

    def test_an_agent_cannot_take_over(self) -> None:
        self.human_lease()
        code, payload = self.android("control", "acquire", "--holder", "agent:a",
                                     "--takeover")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], errors.CONTROL_HELD)
        self.assertEqual(payload["takeover_requires"], "human")


class RefusalTests(ControlCliCase):

    def test_ui_tap_under_a_foreign_human_lease_is_refused_and_journaled(self) -> None:
        session = self.start()
        self.human_lease()
        self.clear_calls()
        code, payload = self.android("ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 2, payload)
        self.assert_refusal(payload)
        self.assertEqual(payload["holder"], {"kind": "human", "id": HUMAN,
                                             "label": "Canvas 3277"})
        self.assertEqual(payload["generation"], 1)
        self.assertEqual(payload["target_id"], SERIAL)
        self.assertEqual(self.calls(), [])
        entry = [item for item in self.journal(session) if item.get("verb") == "ui tap"][-1]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error_code"], errors.CONTROL_HELD)
        self.assertTrue(entry["operation_id"].startswith("op_"))

    def test_every_mutating_verb_is_refused_without_a_device_call(self) -> None:
        session = self.start()
        record = session_mod.load_by_id(session["session_id"])
        # network attach needs a running proxy, network stop an attached one:
        # a sleeping child stands in for the proxy process.
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        from autonom_lib.network import proxy as proxy_mod  # noqa: PLC0415
        proxy_mod.proxy_file(record).write_text(json.dumps({"pid": sleeper.pid,
                                                            "port": 18099}), encoding="utf-8")
        record.setdefault("network", {})["attached"] = True
        session_mod.save(record)
        media = self.root / "photo.png"
        media.write_bytes(b"\x89PNG")
        self.human_lease()
        samples = [
            ("devices", "shutdown"), ("session", "launch", APP),
            ("session", "force-stop", APP), ("session", "clear", APP),
            ("session", "uninstall", APP), ("ui", "tap", "--x", "1", "--y", "1"),
            ("ui", "swipe", "--from", "1,1", "--to", "2,2"), ("ui", "pinch", "--at", "1,1"),
            ("ui", "rotate"), ("ui", "shake"), ("ui", "type", "hello"),
            ("ui", "key", "KEYCODE_BACK"), ("open", "https://example.com/"),
            ("permissions", "grant", "camera", APP), ("location", "set", "1,2"),
            ("location", "clear"), ("media", "add", str(media)),
            ("network", "attach", "--i-understand-mitm"), ("network", "detach"),
            ("network", "stop"), ("metrics", "memory", "warn"),
            ("ui", "accessibility", "enable"), ("ui", "accessibility", "reset"),
            ("simulator", "battery", "set"), ("simulator", "network", "offline"),
            ("simulator", "push", "send"), ("simulator", "sms", "send"),
            ("simulator", "call", "incoming"), ("simulator", "biometric", "match"),
            ("simulator", "clipboard", "set"), ("simulator", "appearance", "dark"),
            ("simulator", "text-size", "large"), ("simulator", "status-bar", "pin"),
            ("simulator", "keyboard", "pin"), ("simulator", "animations", "pin"),
        ]
        cli = load_cli("autonom_cli_control_samples")
        parser = cli.build_parser()
        covered = set()
        for argv in samples:
            with self.subTest(argv=argv):
                self.clear_calls()
                code, payload = self.in_process(cli, "--adb", str(FAKE_ADB),
                                                "--serial", SERIAL, *argv)
                self.assertEqual(code, 2, payload)
                self.assert_refusal(payload)
                self.assertEqual(self.calls(), [])
                cli._ROOT_PARSER = parser
                parsed = parser.parse_args(list(argv))
                self.assertEqual(cli._control_mode(parsed), "mutating")
                covered.add(cli._verb_path(parsed))
        self.assertTrue(sleeper.poll() is None, "the stand-in proxy was stopped")
        self.assertEqual(covered - set(cli._CONTROL_BY_ACTION), set(cli._CONTROL_MUTATING))
        self.assertEqual(covered & set(cli._CONTROL_BY_ACTION), set(cli._CONTROL_BY_ACTION))

    def test_ios_verbs_are_refused_without_a_simctl_call(self) -> None:
        udid = "AAAAAAAA-1111-2222-3333-CCCCCCCCCCCC"
        self.write_state(simctl_devices={"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": udid, "name": "iPhone 17 Pro", "state": "Booted",
                 "isAvailable": True}]}})
        control_mod.acquire("ios", udid, holder_id=HUMAN, kind="human", label="Canvas 3277",
                            ttl_s=3600)
        cli = load_cli("autonom_cli_control_ios")
        for argv in (("ui", "tap", "--x", "1", "--y", "1"), ("simulator", "battery", "set"),
                     ("metrics", "memory", "warn"), ("devices", "shutdown")):
            with self.subTest(argv=argv):
                self.clear_calls()
                code, payload = self.in_process(cli, "--simctl", str(FAKE_SIMCTL),
                                                "--udid", udid, *argv)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.CONTROL_HELD)
                self.assertEqual(payload["platform"], "ios")
                self.assertEqual(payload["target_id"], udid)
                self.assertEqual(self.calls(), [])

    def test_session_start_with_launch_is_admitted_before_the_session_exists(self) -> None:
        self.human_lease(SECOND)
        self.clear_calls()
        code, payload = self.android("session", "start", "--app-id", APP, "--launch",
                                     serial=SECOND)
        self.assertEqual(code, 2, payload)
        self.assert_refusal(payload)
        self.assertEqual(self.calls(), [])
        self.assertIsNone(session_mod.live_for_target(SECOND, "android"))
        # A plain start is lifecycle: never refused by a lease.
        code, payload = self.android("session", "start", "--app-id", APP, serial=SECOND)
        self.assertEqual(code, 0, payload)

    def test_readonly_verbs_and_session_stop_are_admitted(self) -> None:
        self.start()
        self.human_lease()
        for argv in (("ui", "tree", "--dump", str(UI_DUMP)), ("screenshot",),
                     ("session", "list"), ("control", "status"), ("journal",),
                     ("ui", "accessibility", "status"), ("simulator", "clipboard", "get")):
            with self.subTest(argv=argv):
                code, payload = self.android(*argv)
                if argv[0] == "simulator":
                    # Android has no such query; what matters is that the
                    # lease did not refuse it.
                    self.assertNotIn(payload.get("error_code"), control_mod_codes())
                    continue
                self.assertEqual(code, 0, payload)
                self.assertNotIn("control", payload)
        code, payload = self.android("session", "stop")
        self.assertEqual(code, 0, payload)

    def test_host_verbs_are_never_refused(self) -> None:
        self.start()
        self.human_lease()
        code, payload = self.android("network", "mock", "list")
        self.assertNotIn(payload.get("error_code"), control_mod_codes())


def control_mod_codes() -> set[str]:
    return {errors.CONTROL_HELD, errors.CONTROL_GENERATION_STALE,
            errors.CONTROL_LEASE_EXPIRED, errors.CONTROL_NOT_HELD}


class ClaimTests(ControlCliCase):

    def acquire(self, holder: str = "agent:a", **kwargs) -> dict:
        kwargs.setdefault("ttl_s", 3600)
        return control_mod.acquire("android", SERIAL, holder_id=holder, kind="agent", **kwargs)

    def test_a_matching_claim_is_admitted_with_a_control_object(self) -> None:
        session = self.start()
        grant = self.acquire()
        positions = {
            "global": ("--control-holder", "agent:a", "--control-generation", "1",
                       "--operation-id", "op-global-1", "ui", "tap", "--x", "1", "--y", "1"),
            "leaf": ("ui", "tap", "--x", "1", "--y", "1", "--control-holder", "agent:a",
                     "--control-generation=1", "--operation-id", "op-leaf-1"),
        }
        for name, argv in positions.items():
            with self.subTest(position=name):
                self.clear_calls()
                code, payload = self.android(*argv)
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload["control"], {
                    "generation": grant["generation"], "holder_id": "agent:a",
                    "operation_id": f"op-{name}-1"})
                self.assertEqual(sum(1 for call in self.calls() if "tap" in call), 1)
                entry = [item for item in self.journal(session)
                         if item.get("verb") == "ui tap"][-1]
                self.assertTrue(entry["ok"])
                self.assertEqual(entry["operation_id"], f"op-{name}-1")
                # the claim is attribution: the entry names the operation,
                # its argv keeps the verb's own words only
                self.assertFalse(any(token.startswith(("--control-", "--operation-id"))
                                     for token in entry["argv"]), entry["argv"])
                self.assertIn("tap", entry["argv"])

    def test_the_claim_from_the_environment_and_a_generated_operation_id(self) -> None:
        session = self.start()
        self.acquire()
        code, payload = self.android(
            "ui", "key", "KEYCODE_BACK",
            env={"AUTONOM_CONTROL_HOLDER": "agent:a", "AUTONOM_CONTROL_GENERATION": "1"})
        self.assertEqual(code, 0, payload)
        operation = payload["control"]["operation_id"]
        self.assertRegex(operation, r"^op_[0-9a-f]{12}$")
        entry = [item for item in self.journal(session) if item.get("verb") == "ui key"][-1]
        self.assertEqual(entry["operation_id"], operation)

    def test_flags_win_over_the_environment(self) -> None:
        self.start()
        self.acquire()
        code, payload = self.android(
            "--control-holder", "agent:a", "ui", "key", "KEYCODE_BACK",
            env={"AUTONOM_CONTROL_HOLDER": "agent:other"})
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["control"]["holder_id"], "agent:a")

    def test_a_stale_generation_is_refused(self) -> None:
        self.start()
        self.acquire()
        control_mod.acquire("android", SERIAL, holder_id=HUMAN, kind="human", takeover=True,
                            ttl_s=3600)
        self.clear_calls()
        code, payload = self.android("--control-holder", "agent:a",
                                     "--control-generation", "1",
                                     "ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 2, payload)
        self.assert_refusal(payload, errors.CONTROL_GENERATION_STALE)
        self.assertEqual(payload["generation"], 2)
        self.assertEqual(payload["claimed_holder"], "agent:a")
        self.assertEqual(self.calls(), [])

    def test_a_claim_without_a_lease_is_not_held(self) -> None:
        code, payload = self.android("--control-holder", "agent:a",
                                     "ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.CONTROL_NOT_HELD)
        self.assertEqual(self.calls(), [])

    def test_an_expired_lease_admits_unclaimed_callers_and_refuses_its_holder(self) -> None:
        start = 1_800_000_000_000
        self.acquire(ttl_s=5, now_ms=start)
        later = {"AUTONOM_TEST_MODE": "1", "AUTONOM_CONTROL_NOW_MS": str(start + 6_000)}
        code, payload = self.android("ui", "tap", "--x", "1", "--y", "1", env=later)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["control"]["generation"], 1)
        self.assertIsNone(payload["control"]["holder_id"])
        code, payload = self.android("--control-holder", "agent:a", "ui", "tap",
                                     "--x", "1", "--y", "1", env=later)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.CONTROL_LEASE_EXPIRED)
        # The clock seam is ignored outside test mode: the real clock is far
        # past the lease, so the result is the same here, and a lease made
        # in the future stays held.
        control_mod.acquire("android", SECOND, holder_id="agent:b", kind="agent",
                            ttl_s=3600, now_ms=int(time.time() * 1000))
        code, payload = self.android(
            "ui", "tap", "--x", "1", "--y", "1", serial=SECOND,
            env={"AUTONOM_CONTROL_NOW_MS": str(10 ** 15)})
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.CONTROL_HELD)

    def test_a_generation_without_a_holder_is_a_usage_error(self) -> None:
        for argv, env in (
                (("--control-generation", "2", "ui", "tap", "--x", "1", "--y", "1"), None),
                (("ui", "tap", "--x", "1", "--y", "1"), {"AUTONOM_CONTROL_GENERATION": "2"})):
            with self.subTest(argv=argv, env=env):
                code, payload = self.android(*argv, env=env)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.USAGE_ERROR)
        self.assertEqual(self.calls(), [])

    def test_a_claim_flag_without_a_value_is_a_usage_error(self) -> None:
        for argv in (("ui", "tap", "--x", "1", "--y", "1", "--control-holder"),
                     ("--operation-id", "--serial", SERIAL, "ui", "key", "BACK"),
                     ("--control-generation", "--", "ui", "key", "BACK")):
            with self.subTest(argv=argv):
                code, payload, _ = self.run_cli("--adb", str(FAKE_ADB), *argv)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.USAGE_ERROR)
                self.assertIn("expected one argument", payload["error"])
        self.assertEqual(self.calls(), [])

    def test_invalid_claim_values(self) -> None:
        for argv in (("--control-holder", "bad holder", "ui", "key", "BACK"),
                     ("--control-holder", "agent:a", "--control-generation", "0",
                      "ui", "key", "BACK"),
                     ("--control-holder", "agent:a", "--control-generation", "x",
                      "ui", "key", "BACK"),
                     ("--operation-id", "bad id", "ui", "key", "BACK"),
                     ("--operation-id", "x" * 65, "ui", "key", "BACK")):
            with self.subTest(argv=argv):
                code, payload = self.android(*argv)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual(self.calls(), [])


class UiTreeRecoveryTests(ControlCliCase):

    def sparse(self) -> Path:
        sparse = self.root / "sparse.xml"
        sparse.write_text(
            '<hierarchy rotation="0"><node class="android.widget.FrameLayout" '
            'package="com.example.app" resource-id="android:id/content" '
            'bounds="[0,0][1080,2400]"><node class="android.view.View" '
            'package="com.example.app" bounds="[0,0][1080,2400]"/>'
            '</node></hierarchy>', encoding="utf-8")
        return sparse

    def prepare(self) -> None:
        self.start()
        self.write_state(ui_dump=str(self.sparse()), ui_dump_after_launch=str(UI_DUMP),
                         settings_secure={"accessibility_enabled": "0"})
        self.clear_calls()

    def device_writes(self) -> list[list[str]]:
        return [call for call in self.calls()
                if ("settings" in call and "put" in call) or "force-stop" in call
                or ("am" in call and "start" in call) or "monkey" in call]

    def test_recovery_is_skipped_under_a_foreign_lease(self) -> None:
        self.prepare()
        self.human_lease()
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertIn("sparse_accessibility_tree",
                      [warning["code"] for warning in payload.get("warnings", [])])
        self.assertNotIn("accessibility_recovery", payload)
        self.assertNotIn("control", payload)
        self.assertEqual(self.device_writes(), [])
        self.assertEqual(json.loads(self.state.read_text())["settings_secure"],
                         {"accessibility_enabled": "0"})

    def test_without_a_lease_the_recovery_runs_as_before(self) -> None:
        self.prepare()
        code, payload = self.android("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["accessibility_recovery"]["recovered"])
        self.assertTrue(self.device_writes())
        self.assertFalse((self.home / "control").exists())

    def test_the_holder_of_the_lease_may_recover(self) -> None:
        self.prepare()
        control_mod.acquire("android", SERIAL, holder_id="agent:a", kind="agent", ttl_s=3600)
        code, payload = self.android("--control-holder", "agent:a", "ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["accessibility_recovery"]["recovered"])


class CompatibilityTests(ControlCliCase):

    def test_without_a_lease_ui_tap_prints_what_it_printed_before(self) -> None:
        code, _payload, stdout = self.run_cli("--adb", str(FAKE_ADB), "--serial", SERIAL,
                                              "ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, RECORDED_UI_TAP)
        self.assertFalse((self.home / "control").exists(), "admission created files")

    def test_in_process_output_equals_a_run_with_the_control_module_disabled(self) -> None:
        cli = load_cli("autonom_cli_control_compat")
        argv = ["--adb", str(FAKE_ADB), "--serial", SERIAL, "ui", "tap", "--x", "1", "--y", "1"]

        def run(disabled: bool) -> str:
            out = io.StringIO()
            patches = [mock.patch.object(cli.control_mod, "admit_once", return_value=None),
                       mock.patch.object(cli, "_with_control", side_effect=lambda data: data)
                       ] if disabled else []
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                stack.enter_context(contextlib.redirect_stdout(out))
                self.assertEqual(cli.main(list(argv)), 0)
            return out.getvalue()

        self.assertEqual(run(False), run(True))

    def test_admission_reads_one_small_file_fast(self) -> None:
        grant = control_mod.acquire("android", SERIAL, holder_id="agent:a", kind="agent")
        claim = control_mod.Claim("agent:a", grant["generation"])
        for _ in range(20):
            control_mod.admit("android", SERIAL, claim)
        samples = []
        for _ in range(200):
            started = time.perf_counter()
            control_mod.admit("android", SERIAL, claim)
            samples.append((time.perf_counter() - started) * 1000)
        self.assertLess(statistics.median(samples), 5.0)

    def test_the_operation_id_flag_alone_changes_nothing_without_a_lease(self) -> None:
        code, payload = self.android("--operation-id", "op-1", "ui", "tap", "--x", "1",
                                     "--y", "1")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("control", payload)


class AbbreviationTests(unittest.TestCase):
    """No existing option changes meaning (contract 5.1): every spelling below
    parses exactly as at 9acc89c (recorded), both after the verb and before it."""

    SPELLINGS = ("--o", "--ou", "--c", "--ca", "--co", "--con", "--cont", "--op", "--ope",
                 "--la", "--l", "--v", "--val", "--j", "--head", "--tok", "--to", "--ser",
                 "--se", "--s", "--plat", "--p", "--u", "--ud", "--t", "--ta", "--a", "--ad",
                 "--i", "--id", "--g", "--ge", "--ho", "--k", "--ti", "--m", "--d", "--tr",
                 "--w", "--f", "--e")
    # spelling -> the dest it reached at 9acc89c; every other spelling was a usage error.
    RECORDED = {
        ("session start", "leaf"): {"--la": "launch", "--ser": "serial", "--se": "serial",
                                    "--u": "udid", "--ud": "udid", "--t": "target",
                                    "--ta": "target", "--ad": "adb"},
        ("session start", "global"): {"--ser": "serial", "--se": "serial", "--u": "udid",
                                      "--ud": "udid", "--t": "target", "--ta": "target",
                                      "--ad": "adb"},
        ("ui tap", "leaf"): {"--ser": "serial", "--se": "serial", "--u": "udid",
                             "--ud": "udid", "--ta": "target", "--a": "adb", "--ad": "adb"},
        ("ui tap", "global"): {"--ser": "serial", "--se": "serial", "--u": "udid",
                               "--ud": "udid", "--t": "target", "--ta": "target",
                               "--ad": "adb"},
        ("report export", "leaf"): {"--o": "out", "--ou": "out", "--se": "session",
                                    "--s": "session"},
        ("report export", "global"): {"--ser": "serial", "--se": "serial", "--u": "udid",
                                      "--ud": "udid", "--t": "target", "--ta": "target",
                                      "--ad": "adb"},
        ("metrics snapshot", "leaf"): {"--o": "out", "--ou": "out", "--la": "label",
                                       "--l": "label", "--ser": "serial", "--se": "serial",
                                       "--u": "udid", "--ud": "udid", "--ad": "adb"},
        ("metrics snapshot", "global"): {"--ser": "serial", "--se": "serial", "--u": "udid",
                                         "--ud": "udid", "--t": "target", "--ta": "target",
                                         "--ad": "adb"},
        ("canvas serve", "leaf"): {"--c": "captures_dir", "--ca": "captures_dir",
                                   "--tok": "token", "--to": "token", "--ser": "serial",
                                   "--se": "serial", "--u": "udid", "--ud": "udid",
                                   "--ta": "target", "--a": "adb", "--ad": "adb",
                                   "--d": "device", "--w": "workspace"},
        ("canvas serve", "global"): {"--ser": "serial", "--se": "serial", "--u": "udid",
                                     "--ud": "udid", "--t": "target", "--ta": "target",
                                     "--ad": "adb"},
    }
    NEW_DESTS = {"control_holder", "control_generation", "operation_id", "control_idle"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = load_cli("autonom_cli_control_abbrev")
        cls.parser = cls.cli.build_parser()

    def parse(self, argv: list[str]) -> dict | None:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                namespace = self.parser.parse_args(argv)
            except SystemExit:
                return None
        return {key: value for key, value in vars(namespace).items()
                if key != "func" and key not in self.NEW_DESTS}

    def test_recorded_spellings_keep_their_meaning(self) -> None:
        for (verb, position), resolved in self.RECORDED.items():
            path = verb.split()
            base = self.parse(path)
            self.assertIsNotNone(base, verb)
            for spelling in self.SPELLINGS:
                argv = ([*path, spelling, "VAL1"] if position == "leaf"
                        else [spelling, "VAL1", *path])
                with self.subTest(argv=argv):
                    parsed = self.parse(argv)
                    if spelling not in resolved:
                        self.assertIsNone(parsed, "was a usage error at 9acc89c")
                        continue
                    self.assertIsNotNone(parsed)
                    changed = {key: value for key, value in parsed.items()
                               if base.get(key) != value}
                    self.assertEqual(list(changed), [resolved[spelling]])
                    self.assertIn("VAL1", json.dumps(changed))

    def test_the_new_flags_are_exact_only_and_work_on_both_sides(self) -> None:
        for argv in (["--control-holder", "agent:a", "ui", "tap"],
                     ["ui", "tap", "--control-holder", "agent:a"],
                     ["ui", "tap", "--control-holder=agent:a"]):
            with self.subTest(argv=argv):
                namespace = self.parser.parse_args(argv)
                self.assertEqual(namespace.control_holder, "agent:a")
        namespace = self.parser.parse_args(["report", "export", "--operation-id", "op-1",
                                            "--control-generation", "3"])
        self.assertEqual((namespace.operation_id, namespace.control_generation), ("op-1", "3"))
        for argv in (["--control-h", "x", "ui", "tap"], ["ui", "tap", "--operation", "x"],
                     ["canvas", "serve", "--control", "30"]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.parse(argv))
        namespace = self.parser.parse_args(["canvas", "serve", "--control-idle", "30"])
        self.assertEqual(namespace.control_idle, "30")
        # after `--` nothing is hoisted: it stays the typed text
        namespace = self.parser.parse_args(["ui", "type", "--", "--control-holder"])
        self.assertIsNone(namespace.control_holder)
        self.assertEqual(namespace.text, "--control-holder")

    def test_the_new_flags_are_in_the_exact_only_set(self) -> None:
        for flag in ("--control-holder", "--control-generation", "--operation-id"):
            self.assertIn(flag, journal_mod.EXACT_ONLY_OPTIONS)


class CanvasControlIdleTests(EnvSandboxMixin, unittest.TestCase):
    """`canvas serve --control-idle`: validated before node, passed only when given."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.set_env(AUTONOM_HOME=str(self.root / "home"), AUTONOM_ADB=None,
                     AUTONOM_SIMCTL=None, AUTONOM_CONTROL_HOLDER=None,
                     AUTONOM_CONTROL_GENERATION=None)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)
        self.cli = load_cli("autonom_cli_control_canvas")
        self.commands: list[list[str]] = []

    def serve(self, *argv: str) -> tuple[int, str]:
        def supervised(command, **_kwargs):
            self.commands.append(list(command))
            return 0

        err = io.StringIO()
        with mock.patch.object(self.cli, "_canvas_node", return_value="/fake/node"), \
             mock.patch.object(self.cli, "_canvas_scrcpy_server", return_value=None), \
             mock.patch.object(self.cli, "_canvas_idb_lookup",
                               return_value={"available": False, "path": None,
                                             "reason": "not in tests"}), \
             mock.patch.object(self.cli.processes_mod, "run_supervised",
                               side_effect=supervised), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = self.cli.main(["--adb", str(FAKE_ADB), "--simctl", str(FAKE_SIMCTL),
                                  *argv])
        return code, err.getvalue()

    def single_argv(self) -> list[str]:
        """The single-device node argv of 9acc89c for these flags."""
        return ["/fake/node", str(self.cli.CANVAS_SCRIPT), "--platform", "android",
                "--target", SERIAL, "--port", "3999", "--transport", "screencap",
                "--adb", str(FAKE_ADB)]

    def workspace_argv(self) -> list[str]:
        """The workspace node argv of 9acc89c for these flags."""
        return ["/fake/node", str(self.cli.CANVAS_SCRIPT), "--workspace", "ctltest",
                "--port", "3999", "--transport", "screencap", "--adb", str(FAKE_ADB),
                "--simctl", str(FAKE_SIMCTL), "--python", sys.executable,
                "--autonom", str(CLI.resolve())]

    def test_without_the_flag_the_node_argv_is_the_one_of_before(self) -> None:
        code, err = self.serve("--serial", SERIAL, "canvas", "serve", "--port", "3999",
                               "--transport", "screencap")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.commands[-1], self.single_argv())
        code, err = self.serve("canvas", "serve", "--split", "--workspace", "ctltest",
                               "--port", "3999", "--transport", "screencap")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.commands[-1], self.workspace_argv())

    def test_the_flag_is_passed_in_both_modes(self) -> None:
        code, err = self.serve("--serial", SERIAL, "canvas", "serve", "--port", "3999",
                               "--transport", "screencap", "--control-idle", "30")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.commands[-1], self.single_argv() + ["--control-idle", "30"])
        code, err = self.serve("canvas", "serve", "--split", "--workspace", "ctltest",
                               "--port", "3999", "--transport", "screencap",
                               "--control-idle", "3600")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.commands[-1], self.workspace_argv() + ["--control-idle", "3600"])

    def test_out_of_range_values_are_refused_before_node_starts(self) -> None:
        for value in ("14", "3601", "abc", "-20", "15.5"):
            with self.subTest(value=value):
                code, err = self.serve("--serial", SERIAL, "canvas", "serve",
                                       "--control-idle", value)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(err)["error_code"], errors.INVALID_VALUE)
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
