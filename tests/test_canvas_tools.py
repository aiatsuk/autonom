"""Mobile Canvas tools process (`scripts/autonom_canvas_tools.py`): the NDJSON
protocol, every op's success and refusal paths, the simulate allowlist, the
session scope and the journal (wc-001..wc-005, wc-007).

Fakes only: `fake_adb.py` and `fake_simctl.py` stand in for the device tools,
the Android target is a made-up emulator serial, and `AUTONOM_HOME` is a
temporary directory. No device, proxy or host setting is touched; the two
proxy calls that would start or probe `mitmdump` are patched.
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
TOOLS = SCRIPTS / "autonom_canvas_tools.py"
sys.path.insert(0, str(SCRIPTS))

import autonom_canvas_tools as tools_mod  # noqa: E402
from autonom_lib import device_state, errors, session  # noqa: E402
from autonom_lib.network import mocks as mocks_mod  # noqa: E402
from autonom_lib.network import proxy  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FAKE_ADB = str(ROOT / "tests/fakes/fake_adb.py")
FAKE_SIMCTL = str(ROOT / "tests/fakes/fake_simctl.py")
SERIAL = "emulator-5580"
OTHER_SERIAL = "emulator-5582"
PHONE = "R58M123ABC"
UDID = "00000000-0000-0000-0000-00000000F00D"
APP = "com.example.app"
SECRET = "SECRET-7f3a-do-not-journal"
TOOL_ENV = ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_MITMDUMP",
            "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG")

DUMPSYS_PACKAGE = f"""Packages:
  Package [{APP}] (4a1b2c):
    userId=10123
    declared permissions:
      {APP}.permission.C2D_MESSAGE: prot=signature
    requested permissions:
      android.permission.INTERNET
      android.permission.CAMERA
      android.permission.RECORD_AUDIO
      android.permission.ACCESS_BACKGROUND_LOCATION: restricted=true
      android.permission.ACCESS_FINE_LOCATION
    install permissions:
      android.permission.INTERNET: granted=true
    User 0: ceDataInode=1 installed=true hidden=false
      gids=[3003]
      runtime permissions:
        android.permission.ACCESS_FINE_LOCATION: granted=false, flags=[ USER_SENSITIVE_WHEN_GRANTED ]
        android.permission.CAMERA: granted=true, flags=[ USER_SET ]
        android.permission.RECORD_AUDIO: granted=false, flags=[ ]
        android.permission.ACCESS_BACKGROUND_LOCATION: granted=false, flags=[ ]
      disabledComponents:
        {APP}.Hidden
    User 10: ceDataInode=2 installed=true
      runtime permissions:
        android.permission.CAMERA: granted=false, flags=[ ]
"""

_REAL_STDIN = None


def setUpModule() -> None:
    """The consent gate prompts when stdin is a terminal; these tests inject
    consent through `acknowledged`, so stdin must never look like one."""
    global _REAL_STDIN
    _REAL_STDIN = sys.stdin
    sys.stdin = io.StringIO()


def tearDownModule() -> None:
    if _REAL_STDIN is not None:
        sys.stdin = _REAL_STDIN


def android(serial: str = SERIAL) -> Target:
    return Target("android", serial, FAKE_ADB, {"serial": serial})


def ios() -> Target:
    return Target("ios", UDID, FAKE_SIMCTL, {"udid": UDID})


class ToolsCase(EnvSandboxMixin, unittest.TestCase):
    """A sandboxed home, fake device tools, and a Tools bound to `target()`."""

    def target(self) -> Target:
        return android()

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.set_env(**{key: None for key in TOOL_ENV})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = self.tmp / "state.json"
        self.log = self.tmp / "calls.jsonl"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log))
        self.tools = tools_mod.Tools(self.target())

    # --- helpers ---------------------------------------------------------------

    def fake_state(self, **values: object) -> None:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update(values)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def call(self, op: str, payload: object = None, origin: str = "human",
             request_id: object = 7) -> dict:
        message = {"id": request_id, "op": op, "origin": origin}
        if payload is not None:
            message["payload"] = payload
        response = tools_mod.respond(self.tools, json.dumps(message))
        # Every reply must survive the wire.
        return json.loads(tools_mod.encode(response))

    def ok(self, op: str, payload: object = None, origin: str = "human") -> dict:
        response = self.call(op, payload, origin)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["id"], 7)
        return response["result"]

    def refused(self, op: str, payload: object, code: str, origin: str = "human") -> dict:
        response = self.call(op, payload, origin)
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error_code"], code, response)
        self.assertEqual(response["id"], 7)
        self.assertIn("hint", response)
        self.assertIn("capability", response)
        self.assertIsInstance(response["error"], str)
        return response

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    def start_session(self, target_id: str = SERIAL, app_id: str | None = APP,
                      platform: str = "android") -> dict:
        if platform == "android":
            return session.start_session(FAKE_ADB, serial=target_id, app_id=app_id)
        return session.start_session(FAKE_SIMCTL, platform="ios", target_id=target_id,
                                     app_id=app_id)

    def journal(self, record: dict) -> list[dict]:
        path = Path(record["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def journal_text(self, record: dict) -> str:
        path = Path(record["artifacts_dir"]) / "journal.ndjson"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_flows(self, record: dict, flows: list[dict]) -> None:
        path = Path(record["artifacts_dir"]) / "network" / "flows.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(flow) + "\n" for flow in flows),
                        encoding="utf-8")


# --- protocol ----------------------------------------------------------------------


class ProcessProtocolTests(EnvSandboxMixin, unittest.TestCase):
    """The real process: one reply per line, in order, and nothing else on stdout."""

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "state.json").write_text("{}", encoding="utf-8")

    def env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key not in TOOL_ENV}
        env.update({"AUTONOM_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
                    "AUTONOM_FAKE_STATE": str(self.tmp / "state.json"),
                    "AUTONOM_FAKE_LOG": str(self.tmp / "calls.jsonl")})
        return env

    def run_tools(self, stdin: bytes, *target: str) -> subprocess.CompletedProcess:
        argv = list(target) or ["--platform", "android", "--target", SERIAL,
                                "--tool", FAKE_ADB]
        return subprocess.run([sys.executable, str(TOOLS), *argv], input=stdin,
                              capture_output=True, timeout=120, env=self.env())

    def test_bad_lines_never_stop_the_process_and_replies_stay_in_order(self) -> None:
        lines = [
            {"id": 1, "op": "context", "origin": "human"},
            "this is not json",
            [1, 2, 3],
            {"id": 4, "op": "no.such.op", "origin": "human"},
            {"id": 5, "op": "context", "origin": "root"},
            {"id": 6, "op": "location.set", "origin": "human", "payload": [52, 4]},
            {"id": 7, "op": "simulate", "origin": "agent",
             "payload": {"control": "clipboard", "action": "set"}},
            {"id": 8, "op": "mocks.list", "origin": "human"},
        ]
        raw = b"".join((item if isinstance(item, str) else json.dumps(item)).encode() + b"\n"
                       for item in lines)
        # A blank line gets no reply; a line that is not UTF-8 gets an error reply.
        raw += b"\n\xff\xfe broken\n" + json.dumps(
            {"id": 9, "op": "context", "origin": "system"}).encode() + b"\n"
        completed = self.run_tools(raw)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = [json.loads(line) for line in completed.stdout.decode().splitlines()]
        self.assertEqual(len(replies), len(lines) + 2, completed.stdout)
        self.assertEqual([reply["id"] for reply in replies],
                         [1, None, None, 4, 5, 6, 7, 8, None, 9])
        self.assertEqual([reply["ok"] for reply in replies],
                         [True, False, False, False, False, False, False, True, False, True])
        for reply in replies:
            if not reply["ok"]:
                self.assertEqual(reply["error_code"], errors.FLOW_COMMAND_INVALID, reply)
                self.assertIn("hint", reply)
                self.assertIn("capability", reply)
        self.assertEqual(replies[0]["result"]["platform"], "android")
        self.assertIn("valid ops", replies[3]["error"])
        self.assertIn("mocks.clear", replies[3]["error"])

    def test_library_and_child_output_never_reach_the_protocol_stream(self) -> None:
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import autonom_canvas_tools as t\n"
            "out = t._protocol_stream()\n"
            "print('library noise')\n"
            "os.system('echo child noise')\n"
            "out.write('{\"id\": 1}\\n')\n"
            "out.flush()\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                   timeout=60, env=self.env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.decode().splitlines(), ['{"id": 1}'])
        self.assertIn("library noise", completed.stderr.decode())
        self.assertIn("child noise", completed.stderr.decode())

    def test_ios_process_answers_context(self) -> None:
        stdin = json.dumps({"id": 3, "op": "context", "origin": "human"}).encode() + b"\n"
        completed = self.run_tools(stdin, "--platform", "ios", "--target", UDID,
                                   "--tool", FAKE_SIMCTL)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (reply,) = [json.loads(line) for line in completed.stdout.decode().splitlines()]
        self.assertTrue(reply["ok"], reply)
        self.assertIn("photos", reply["result"]["privacy_services"])


class DispatchTests(ToolsCase):
    """Unknown ops, origins and payload types are refused before any work."""

    def test_unknown_op_names_the_valid_ops(self) -> None:
        response = self.refused("location.teleport", {}, errors.FLOW_COMMAND_INVALID)
        for op in tools_mod.OPS:
            self.assertIn(op, response["error"])
        self.assertEqual(self.calls(), [])

    def test_the_op_list_is_the_contract(self) -> None:
        self.assertEqual(set(tools_mod.READ_OPS), {
            "context", "permissions.list", "location.get", "biometric.status", "network.status",
            "network.requests", "network.request", "mocks.list"})
        self.assertEqual(set(tools_mod.MUTATING_OPS), {
            "permissions.set", "location.set", "location.clear", "simulate",
            "network.start", "network.capture", "network.attach", "network.detach", "network.stop",
            "mocks.add", "mocks.update", "mocks.enable", "mocks.disable",
            "mocks.remove", "mocks.clear"})

    def test_an_invalid_origin_is_refused_like_the_bridge(self) -> None:
        record = self.start_session()
        for origin in (None, "", "root", "Human", 1, True):
            with self.subTest(origin=origin):
                response = self.call("location.set", {"latitude": 1, "longitude": 2},
                                     origin=origin)  # type: ignore[arg-type]
                self.assertEqual(response["error_code"], errors.FLOW_COMMAND_INVALID)
                self.assertIn("origin", response["error"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.journal(record), [])

    def test_a_payload_that_is_not_an_object_is_refused(self) -> None:
        for payload in ([1, 2], "x", 3, True):
            with self.subTest(payload=payload):
                self.refused("location.set", payload, errors.FLOW_COMMAND_INVALID)
                self.refused("context", payload, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.calls(), [])

    def test_a_request_that_is_not_an_object_has_no_id(self) -> None:
        for line in ("[]", "42", '"context"', "null", "{bad"):
            with self.subTest(line=line):
                response = tools_mod.respond(self.tools, line)
                self.assertIsNone(response["id"])
                self.assertEqual(response["error_code"], errors.FLOW_COMMAND_INVALID)

    def test_request_ids_are_echoed_and_odd_ones_dropped(self) -> None:
        self.assertEqual(self.call("mocks.list", request_id=12)["id"], 12)
        self.assertEqual(self.call("mocks.list", request_id="a-1")["id"], "a-1")
        self.assertIsNone(self.call("mocks.list", request_id=True)["id"])
        self.assertIsNone(self.call("mocks.list", request_id={"x": 1})["id"])

    def test_an_unexpected_exception_becomes_backend_failed(self) -> None:
        with mock.patch.object(device_state, "get_location",
                               side_effect=RuntimeError("boom")):
            response = self.refused("location.get", {}, errors.BACKEND_FAILED)
        self.assertEqual(response["error"], "boom")
        self.assertIsNone(response["hint"])
        self.assertIsNone(response["capability"])
        # and the next request is served normally
        self.assertTrue(self.call("mocks.list")["ok"])

    def test_a_null_payload_is_an_empty_one(self) -> None:
        response = tools_mod.respond(self.tools, json.dumps(
            {"id": 1, "op": "mocks.list", "origin": "human", "payload": None}))
        self.assertTrue(response["ok"], response)


# --- context -----------------------------------------------------------------------


class ContextTests(ToolsCase):
    def test_android_emulator_without_a_session(self) -> None:
        result = self.ok("context")
        self.assertEqual(result["platform"], "android")
        self.assertEqual(result["target_id"], SERIAL)
        self.assertTrue(result["emulator"])
        self.assertIsNone(result["session"])
        self.assertFalse(result["network_available"])
        # best-known app: the foreground package the fake reports
        self.assertEqual(result["app_id"], APP)
        self.assertIsNone(result["privacy_services"])
        self.assertEqual(result["android_permission_aliases"]["camera"],
                         ["android.permission.CAMERA"])
        self.assertEqual(result["simulate"], {
            "biometric": ["match", "nonmatch", "settings"], "battery": ["set", "reset"],
            "network": ["online", "offline"], "appearance": ["light", "dark"],
            "text-size": ["0.85", "1.0", "1.3", "1.5", "2.0"], "animations": ["pin", "reset"]})
        self.assertTrue(result["location_readable"])
        self.assertFalse(result["location_clearable"])
        self.assertTrue(result["permissions_readable"])
        self.assertTrue(set(result["simulate"]) <= set(tools_mod.SIMULATE_CONTROLS))

    def test_a_launcher_in_front_is_no_default_app(self) -> None:
        self.fake_state(foreground_app="com.google.android.apps.nexuslauncher")
        self.assertIsNone(self.ok("context")["app_id"])

    def test_a_session_on_this_target_provides_the_app_and_the_network(self) -> None:
        record = self.start_session(app_id="com.example.other")
        result = self.ok("context")
        self.assertEqual(result["session"], {"id": record["session_id"],
                                             "app_id": "com.example.other"})
        self.assertEqual(result["app_id"], "com.example.other")
        self.assertTrue(result["network_available"])

    def test_a_session_on_another_target_is_not_used(self) -> None:
        self.start_session(OTHER_SERIAL, app_id="com.example.other")
        result = self.ok("context")
        self.assertIsNone(result["session"])
        self.assertFalse(result["network_available"])
        self.assertEqual(result["app_id"], APP)

    def test_a_physical_device_offers_no_simulations(self) -> None:
        self.tools = tools_mod.Tools(android(PHONE))
        result = self.ok("context")
        self.assertFalse(result["emulator"])
        self.assertEqual(result["simulate"], {})


class IosContextTests(ToolsCase):
    def target(self) -> Target:
        return ios()

    def test_ios_context(self) -> None:
        result = self.ok("context")
        self.assertEqual(result["platform"], "ios")
        self.assertTrue(result["emulator"])
        self.assertIn("photos", result["privacy_services"])
        self.assertIsNone(result["android_permission_aliases"])
        self.assertFalse(result["location_readable"])
        self.assertTrue(result["location_clearable"])
        self.assertFalse(result["permissions_readable"])
        self.assertIsNone(result["app_id"])
        self.assertEqual(result["simulate"], {
            "push": ["send"], "biometric": ["enroll", "unenroll", "match", "nonmatch"],
            "battery": ["set", "reset"], "appearance": ["light", "dark"],
            "text-size": ["small", "large", "extra-extra-extra-large", "accessibility-extra-extra-extra-large"]})

    def test_ios_session_on_this_simulator(self) -> None:
        record = self.start_session(UDID, platform="ios")
        result = self.ok("context")
        self.assertEqual(result["session"]["id"], record["session_id"])
        self.assertEqual(result["app_id"], APP)


# --- permissions (wc-001) -------------------------------------------------------------


class AndroidPermissionTests(ToolsCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_state(fail={f"-s {SERIAL} shell dumpsys package {APP}": [0, DUMPSYS_PACKAGE]})

    def test_list_reads_runtime_permissions_and_granted_state(self) -> None:
        result = self.ok("permissions.list", {"app_id": APP})
        self.assertEqual(result["app_id"], APP)
        self.assertTrue(result["readable"])
        self.assertEqual(result["permissions"], [
            {"name": "android.permission.CAMERA", "alias": "camera", "granted": True},
            {"name": "android.permission.RECORD_AUDIO", "alias": "microphone",
             "granted": False},
            {"name": "android.permission.ACCESS_BACKGROUND_LOCATION", "alias": None,
             "granted": False},
            {"name": "android.permission.ACCESS_FINE_LOCATION", "alias": "location",
             "granted": False},
        ])
        self.assertEqual(self.calls(), [["-s", SERIAL, "shell", "dumpsys", "package", APP]])

    def test_list_defaults_to_the_session_app(self) -> None:
        self.start_session(app_id=APP)
        self.assertEqual(self.ok("permissions.list", {})["app_id"], APP)

    def test_list_without_any_app_id_is_refused(self) -> None:
        self.refused("permissions.list", {}, errors.INVALID_VALUE)
        self.start_session(OTHER_SERIAL, app_id=APP)  # another target's app is no default
        self.refused("permissions.list", {}, errors.INVALID_VALUE)
        self.assertEqual(self.calls(), [])

    def test_list_of_an_app_that_is_not_installed(self) -> None:
        self.fake_state(fail={f"-s {SERIAL} shell dumpsys package com.nope": [
            0, "Unable to find package: com.nope\n"]})
        response = self.refused("permissions.list", {"app_id": "com.nope"},
                                errors.APP_NOT_INSTALLED)
        self.assertIn("pm list packages", response["hint"])

    def test_app_ids_and_services_that_could_reach_the_shell_are_refused(self) -> None:
        for bad in ("com.x; reboot", "com.x$(id)", "-f", "a b", "x" * 300):
            with self.subTest(app_id=bad):
                self.refused("permissions.list", {"app_id": bad}, errors.INVALID_VALUE)
                self.refused("permissions.set", {"app_id": bad, "action": "grant",
                                                 "service": "camera"}, errors.INVALID_VALUE)
        self.refused("permissions.set", {"app_id": APP, "action": "grant",
                                         "service": "camera;reboot"}, errors.INVALID_VALUE)
        self.refused("permissions.list", {"app_id": 5}, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.calls(), [])

    def test_revoke_changes_only_this_package_and_is_journaled(self) -> None:
        record = self.start_session(app_id=APP)
        result = self.ok("permissions.set", {"app_id": APP, "action": "revoke",
                                             "service": "android.permission.CAMERA"})
        self.assertEqual(result["action"], "revoke")
        self.assertEqual(result["permissions"], ["android.permission.CAMERA"])
        self.assertEqual(self.calls(), [["-s", SERIAL, "shell", "pm", "revoke", APP,
                                         "android.permission.CAMERA"]])
        (entry,) = self.journal(record)
        self.assertEqual(entry["verb"], "canvas permissions.set")
        self.assertEqual(entry["origin"], "human")
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["argv"], ["canvas", "permissions.set", "revoke",
                                         "android.permission.CAMERA", APP])
        self.assertEqual(entry["result"], {"app_id": APP})

    def test_reset_all_resets_this_package_only(self) -> None:
        result = self.ok("permissions.set", {"app_id": APP, "action": "reset",
                                             "service": "all"})
        self.assertEqual(result["scope"], "package")
        self.assertEqual(result["revoked"], ["android.permission.CAMERA"])
        self.assertFalse(any("reset-permissions" in argv for argv in self.calls()))

    def test_bad_actions_are_refused_before_the_device(self) -> None:
        record = self.start_session()
        for action in (None, "", "toggle", 1):
            with self.subTest(action=action):
                self.refused("permissions.set", {"app_id": APP, "action": action,
                                                 "service": "camera"},
                             errors.FLOW_COMMAND_INVALID)
        self.refused("permissions.set", {"app_id": APP, "action": "grant"},
                     errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.calls(), [])
        entries = self.journal(record)
        self.assertTrue(entries and all(not entry["ok"] for entry in entries))
        self.assertEqual({entry["error_code"] for entry in entries},
                         {errors.FLOW_COMMAND_INVALID})

    def test_an_unknown_permission_is_named(self) -> None:
        self.fake_state(fail={f"-s {SERIAL} shell pm grant {APP} android.permission.NOPE": [
            0, "Exception occurred while executing 'grant':\n"
               "java.lang.IllegalArgumentException: Unknown permission: "
               "android.permission.NOPE"]})
        self.refused("permissions.set", {"app_id": APP, "action": "grant",
                                         "service": "android.permission.NOPE"},
                     errors.INVALID_VALUE)


class IosPermissionTests(ToolsCase):
    def target(self) -> Target:
        return ios()

    def test_list_has_one_unreadable_entry_per_service(self) -> None:
        result = self.ok("permissions.list", {"app_id": APP})
        self.assertFalse(result["readable"])
        names = [entry["name"] for entry in result["permissions"]]
        self.assertIn("photos", names)
        self.assertNotIn("all", names)
        self.assertTrue(all(entry["granted"] is None for entry in result["permissions"]))
        self.assertIn("cannot read", result["note"])
        self.assertFalse(any("privacy" in argv and "grant" in argv for argv in self.calls()))

    def test_list_needs_no_app_id(self) -> None:
        self.assertIsNone(self.ok("permissions.list", {})["app_id"])

    def test_grant_runs_simctl_privacy(self) -> None:
        record = self.start_session(UDID, platform="ios")
        self.ok("permissions.set", {"app_id": APP, "service": "photos", "action": "grant"},
                origin="agent")
        self.assertIn(["simctl", "privacy", UDID, "grant", "photos", APP], self.calls())
        (entry,) = self.journal(record)
        self.assertEqual(entry["origin"], "agent")
        self.assertEqual(entry["verb"], "canvas permissions.set")

    def test_grant_defaults_to_the_session_app(self) -> None:
        self.start_session(UDID, app_id="com.example.session", platform="ios")
        self.ok("permissions.set", {"service": "photos", "action": "grant"})
        self.assertIn(["simctl", "privacy", UDID, "grant", "photos", "com.example.session"],
                      self.calls())

    def test_reset_all_needs_no_app(self) -> None:
        self.ok("permissions.set", {"service": "all", "action": "reset"})
        self.assertIn(["simctl", "privacy", UDID, "reset", "all"], self.calls())

    def test_grant_without_an_app_is_refused(self) -> None:
        self.refused("permissions.set", {"service": "photos", "action": "grant"},
                     errors.INVALID_VALUE)
        self.assertFalse(any("grant" in argv for argv in self.calls()))

    def test_unknown_service_lists_the_valid_ones(self) -> None:
        response = self.refused("permissions.set", {"app_id": APP, "service": "telepathy",
                                                    "action": "grant"},
                                errors.UNKNOWN_PRIVACY_SERVICE)
        self.assertIn("photos", response["hint"])
        self.assertIn("photos", response["supported_services"])


# --- location (wc-002) ---------------------------------------------------------------


class AndroidLocationTests(ToolsCase):
    def test_set_with_a_session_injects_and_remembers_the_fix(self) -> None:
        record = self.start_session()
        result = self.ok("location.set", {"latitude": 52.3702, "longitude": 4.8952})
        self.assertEqual(result["via"], "emulator_console")
        self.assertEqual(result["serial"], SERIAL)
        geo = [argv for argv in self.calls() if argv[2:5] == ["emu", "geo", "fix"]]
        self.assertEqual(geo, [["-s", SERIAL, "emu", "geo", "fix", "4.8952000", "52.3702000"]])
        saved = session.load_current()
        self.assertEqual(saved["location_requested"]["latitude"], 52.3702)
        (entry,) = self.journal(record)
        self.assertEqual(entry["verb"], "canvas location.set")
        self.assertEqual(entry["argv"], ["canvas", "location.set", "52.3702,4.8952"])
        self.assertEqual(entry["result"], {"via": "emulator_console"})

    def test_get_reports_requested_versus_delivered(self) -> None:
        self.start_session()
        self.ok("location.set", {"latitude": 10, "longitude": 20})
        result = self.ok("location.get")
        self.assertEqual(result["provider"], "fused")
        self.assertFalse(result["delivered"])
        self.assertEqual((result["requested"]["latitude"], result["requested"]["longitude"]),
                         (10.0, 20.0))
        self.ok("location.set", {"latitude": 55.751244, "longitude": 37.618423})
        self.assertTrue(self.ok("location.get")["delivered"])

    def test_get_without_a_session_has_no_requested_value(self) -> None:
        self.start_session(OTHER_SERIAL)
        result = self.ok("location.get")
        self.assertNotIn("requested", result)

    def test_invalid_coordinates_never_reach_the_device(self) -> None:
        for payload in ({"latitude": 91, "longitude": 0}, {"latitude": 0, "longitude": -181},
                        {"latitude": "52", "longitude": 4}, {"latitude": 52},
                        {}, {"latitude": True, "longitude": 4},
                        {"latitude": 10 ** 400, "longitude": 4}):
            with self.subTest(payload=payload):
                self.refused("location.set", payload, errors.INVALID_COORDINATES)
        self.assertEqual(self.calls(), [])

    def test_android_has_no_clear(self) -> None:
        response = self.refused("location.clear", {}, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("location set", response["hint"])

    def test_a_physical_device_is_refused_with_a_hint(self) -> None:
        self.tools = tools_mod.Tools(android(PHONE))
        response = self.refused("location.set", {"latitude": 1, "longitude": 2},
                                errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("mock-location", response["hint"])


class IosLocationTests(ToolsCase):
    def target(self) -> Target:
        return ios()

    def test_set_and_clear(self) -> None:
        result = self.ok("location.set", {"latitude": 52.3702, "longitude": 4.8952})
        self.assertEqual(result["via"], "simctl")
        self.assertEqual(self.ok("location.clear"), {"location": None, "platform": "ios",
                                                     "target_id": UDID})
        self.assertIn(["simctl", "location", UDID, "set", "52.3702,4.8952"], self.calls())
        self.assertIn(["simctl", "location", UDID, "clear"], self.calls())

    def test_there_is_no_read_back(self) -> None:
        self.refused("location.get", {}, errors.UNSUPPORTED_ON_PLATFORM)


# --- simulate (wc-003) ---------------------------------------------------------------


class AndroidSimulateTests(ToolsCase):
    def test_biometric_status_is_read_only_without_control(self):
        result = self.ok("biometric.status")
        self.assertEqual(result["biometry"], "fingerprint")
        self.assertIn("enrolled", result)


    def test_controls_outside_the_allowlist_never_reach_the_device(self) -> None:
        for control in ("clipboard", "status-bar", "sms", "call", "keyboard", None, 3):
            with self.subTest(control=control):
                response = self.refused("simulate", {"control": control, "action": "set"},
                                        errors.FLOW_COMMAND_INVALID)
                self.assertIn("push, biometric, battery, network, appearance",
                              response["hint"])
        self.assertEqual(self.calls(), [])

    def test_appearance_round_trip(self) -> None:
        record = self.start_session()
        dark = self.ok("simulate", {"control": "appearance", "action": "dark"})
        self.assertEqual(dark["observed"], "yes")
        self.assertEqual(json.loads(self.state.read_text())["night_mode"], "yes")
        light = self.ok("simulate", {"control": "appearance", "action": "light"})
        self.assertEqual(light["observed"], "no")
        self.assertEqual([entry["argv"] for entry in self.journal(record)], [
            ["canvas", "simulate", "appearance", "dark"],
            ["canvas", "simulate", "appearance", "light"]])

    def test_battery_values_are_journaled(self) -> None:
        record = self.start_session()
        result = self.ok("simulate", {"control": "battery", "action": "set",
                                      "values": {"level": 42}})
        self.assertEqual(result["observed_level"], 42)
        self.assertEqual(self.journal(record)[0]["argv"],
                         ["canvas", "simulate", "battery", "set", "level=42"])

    def test_push_on_android_is_the_library_refusal(self) -> None:
        response = self.refused("simulate", {"control": "push", "action": "send",
                                             "values": {"app_id": APP,
                                                        "payload": {"aps": {}}}},
                                errors.UNSUPPORTED_CAPABILITY)
        self.assertTrue(response["hint"])
        self.assertEqual(response["capability"], "simulator.push")

    def test_an_unknown_action_is_the_library_refusal(self) -> None:
        response = self.refused("simulate", {"control": "appearance", "action": "purple"},
                                errors.INVALID_SIMULATOR_ACTION)
        self.assertIn("light", response["hint"])
        self.assertEqual(self.calls(), [])

    def test_bad_values_are_refused(self) -> None:
        for values in ([1], "level=5", {"level": [1]}, {"level": {"x": 1}},
                       {"colour": "red"}, {"level": "x" * 300}):
            with self.subTest(values=values):
                self.refused("simulate", {"control": "battery", "action": "set",
                                          "values": values}, errors.FLOW_COMMAND_INVALID)
        self.refused("simulate", {"control": "battery", "action": ""},
                     errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.calls(), [])

    def test_a_physical_device_is_refused_by_the_library(self) -> None:
        self.tools = tools_mod.Tools(android(PHONE))
        self.refused("simulate", {"control": "appearance", "action": "dark"},
                     errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(self.calls(), [])


class IosSimulateTests(ToolsCase):
    def test_largest_accessibility_category_and_biometric_status(self):
        result = self.ok("simulate", {"control": "text-size", "action": "accessibility-extra-extra-extra-large"})
        self.assertEqual(result["value"], "accessibility-extra-extra-extra-large")
        status = self.ok("biometric.status")
        self.assertIn(status["biometry"], ("face", "touch", "unknown"))
        self.assertIn("enrolled", status)

    def target(self) -> Target:
        return ios()

    def test_push_is_sent_and_only_its_length_is_journaled(self) -> None:
        record = self.start_session(UDID, platform="ios")
        payload = {"aps": {"alert": SECRET}}
        result = self.ok("simulate", {"control": "push", "action": "send",
                                      "values": {"payload": payload}})
        self.assertEqual(result["app_id"], APP)
        self.assertEqual(result["app_id_source"], "session")
        self.assertTrue(any(argv[:4] == ["simctl", "push", UDID, APP] for argv in self.calls()))
        (entry,) = self.journal(record)
        size = len(json.dumps(payload, separators=(",", ":")).encode())
        self.assertEqual(entry["argv"], ["canvas", "simulate", "push", "send",
                                         f"payload_len={size}", f"app_id={APP}"])
        self.assertNotIn(SECRET, self.journal_text(record))
        everything = "".join(path.read_text(encoding="utf-8", errors="replace")
                             for path in Path(record["artifacts_dir"]).rglob("*")
                             if path.is_file())
        self.assertNotIn(SECRET, everything)

    def test_a_push_payload_over_4_kib_never_reaches_simctl(self) -> None:
        big = {"aps": {"alert": "x" * 4100}}
        for payload in (big, json.dumps(big)):
            with self.subTest(kind=type(payload).__name__):
                response = self.refused("simulate", {
                    "control": "push", "action": "send",
                    "values": {"app_id": APP, "payload": payload}},
                    errors.FLOW_COMMAND_INVALID)
                self.assertIn("4096", response["error"])
        self.refused("simulate", {"control": "push", "action": "send",
                                  "values": {"app_id": APP, "payload": [1]}},
                     errors.FLOW_COMMAND_INVALID)
        self.refused("simulate", {"control": "push", "action": "send",
                                  "values": {"app_id": "bad id", "payload": {}}},
                     errors.INVALID_VALUE)
        self.assertEqual(self.calls(), [])

    def test_push_without_an_app_is_the_library_refusal(self) -> None:
        self.start_session(UDID, app_id=None, platform="ios")
        response = self.refused("simulate", {"control": "push", "action": "send",
                                             "values": {"payload": {"aps": {}}}},
                                errors.INVALID_VALUE)
        self.assertEqual(response["missing"], ["app_id"])

    def test_network_shaping_is_unsupported_on_ios(self) -> None:
        self.refused("simulate", {"control": "network", "action": "offline"},
                     errors.UNSUPPORTED_CAPABILITY)

    def test_appearance_reads_back(self) -> None:
        self.assertEqual(self.ok("simulate", {"control": "appearance",
                                              "action": "dark"})["observed"], "dark")


# --- network (wc-004) ----------------------------------------------------------------


FLOWS = [
    {"id": "f1", "method": "GET", "host": "api.example.com", "status": 200,
     "url": "https://api.example.com/v1/items?token=abc", "path": "/v1/items",
     "request_headers_preview": {"authorization": "Bearer abc.def"},
     "started_at": "2026-10-07T10:00:00Z"},
    {"id": "f2", "method": "POST", "host": "api.example.com", "status": 418,
     "url": "https://api.example.com/autonom-tools-test", "path": "/autonom-tools-test",
     "mocked": True, "mock_id": "m_1", "started_at": "2026-10-07T10:00:01Z"},
    {"id": "f3", "method": "GET", "host": "cdn.example.com", "status": 200,
     "url": "https://cdn.example.com/a.png", "path": "/a.png",
     "started_at": "2026-10-07T10:00:02Z"},
]
RUNNING = {"running": True, "pid": 4242, "port": 8899, "proxy_host": "127.0.0.1",
           "capture_bodies": False, "started_at": "2026-10-07T10:00:00Z"}


class NetworkScopeTests(ToolsCase):
    """wc-004-S01: no session on this target, no network op."""

    OPS = ("network.status", "network.requests", "network.request", "network.start", "network.capture",
           "network.attach", "network.detach", "network.stop")

    def assert_all_refused(self) -> None:
        for op in self.OPS:
            with self.subTest(op=op):
                response = self.refused(op, {"id": "f1", "acknowledged": True},
                                        errors.NO_ACTIVE_SESSION)
                self.assertIn(f"autonom session start --serial {SERIAL}", response["hint"])

    def test_without_any_session(self) -> None:
        with mock.patch.object(proxy, "start", side_effect=AssertionError("started")):
            self.assert_all_refused()
        self.assertIsNone(session.load_current())  # no session was created
        self.assertEqual(self.calls(), [])

    def test_with_a_session_on_another_target(self) -> None:
        other = self.start_session(OTHER_SERIAL)
        with mock.patch.object(proxy, "start", side_effect=AssertionError("started")):
            self.assert_all_refused()
        self.assertEqual(self.journal(other), [])

    def test_a_stopped_session_does_not_count(self) -> None:
        record = self.start_session()
        # a pointer that still names a stopped session (written by an older stop)
        pointer = session.artifacts_root() / "current.json"
        pointer.write_text(json.dumps({**record, "stopped_at": "2026-10-07T10:00:00Z"}),
                           encoding="utf-8")
        self.refused("network.status", {}, errors.NO_ACTIVE_SESSION)
        session.save(record)
        self.assertTrue(self.ok("network.status") is not None)
        session.stop_session(reap=False)
        self.refused("network.status", {}, errors.NO_ACTIVE_SESSION)

    def test_ios_hint_names_the_udid(self) -> None:
        self.tools = tools_mod.Tools(ios())
        response = self.refused("network.status", {}, errors.NO_ACTIVE_SESSION)
        self.assertIn(f"--platform ios --udid {UDID}", response["hint"])


class NetworkTests(ToolsCase):
    def setUp(self) -> None:
        super().setUp()
        self.record = self.start_session()

    def test_status_reports_the_proxy_and_mocks(self) -> None:
        result = self.ok("network.status")
        self.assertFalse(result["proxy"]["running"])
        self.assertIn(result["attached"], (True, False, "unknown"))
        self.assertIn("mocks", result)
        self.assertNotIn("ok", result)

    def test_requests_list_with_cursor_and_filters(self) -> None:
        self.write_flows(self.record, FLOWS)
        result = self.ok("network.requests")
        self.assertEqual([flow["id"] for flow in result["requests"]], ["f3", "f2", "f1"])
        self.assertEqual(result["total_matched"], 3)
        self.assertFalse(result["truncated"])
        after = self.ok("network.requests", {"since_id": "f1"})
        self.assertEqual([flow["id"] for flow in after["requests"]], ["f3", "f2"])
        self.assertEqual([f["id"] for f in self.ok("network.requests", {
            "host": "api.example.com", "method": "post", "status": 418,
            "mocked": True})["requests"]], ["f2"])
        limited = self.ok("network.requests", {"max": 1})
        self.assertEqual(limited["count"], 1)
        self.assertTrue(limited["truncated"])
        # previews come back redacted
        first = [flow for flow in result["requests"] if flow["id"] == "f1"][0]
        self.assertNotIn("abc.def", json.dumps(first))
        self.assertNotIn("token=abc", first["url"])

    def test_requests_payload_types(self) -> None:
        for payload in ({"max": 0}, {"max": 201}, {"max": "5"}, {"max": True},
                        {"max": 1.5}, {"status": "200"}, {"mocked": "yes"},
                        {"host": 5}, {"method": "GET /"}, {"since_id": ["f1"]}):
            with self.subTest(payload=payload):
                self.refused("network.requests", payload, errors.FLOW_COMMAND_INVALID)

    def test_request_details_are_never_full(self) -> None:
        self.write_flows(self.record, FLOWS)
        result = self.ok("network.request", {"id": "f1", "full": True})
        self.assertEqual(set(result), {"request"})
        self.assertEqual(result["request"]["id"], "f1")
        self.assertNotIn("abc.def", json.dumps(result))
        self.refused("network.request", {"id": "nope"}, errors.FLOW_NOT_FOUND)
        self.refused("network.request", {}, errors.FLOW_COMMAND_INVALID)

    def test_copy_body_is_on_demand_and_validates_side(self) -> None:
        self.write_flows(self.record, [{"id": "f_0001", "response_body_preview": "hello"},
                                      {"id": "f_0002", "response_body_preview": "hello…[truncated]"}])
        self.assertEqual(self.ok("network.request", {"id": "f_0001", "body": "response"}),
                         {"complete": True, "body": "hello"})
        self.assertEqual(self.ok("network.request", {"id": "f_0002", "body": "response"}),
                         {"complete": False, "reason": "not_captured"})
        self.refused("network.request", {"id": "f_0001", "body": "../req"}, errors.FLOW_COMMAND_INVALID)

    def test_start_without_acknowledged_true_is_the_consent_error(self) -> None:
        with mock.patch.object(proxy, "start", side_effect=AssertionError("started")):
            for payload in ({}, {"acknowledged": False}, {"acknowledged": "true"},
                            {"acknowledged": 1}):
                with self.subTest(payload=payload):
                    response = self.refused("network.start", payload,
                                            errors.CONSENT_REQUIRED)
                    self.assertIn("--i-understand-mitm", response["hint"])
        self.assertEqual(session.load_current()["consent_log"], [])

    def test_start_records_consent_and_never_captures_bodies(self) -> None:
        with mock.patch.object(proxy, "start", return_value=dict(RUNNING)) as start:
            result = self.ok("network.start", {"acknowledged": True, "capture_bodies": True})
        self.assertEqual(start.call_args.kwargs["capture_bodies"], False)
        self.assertIsNone(start.call_args.kwargs["port"])
        self.assertEqual(result["port"], 8899)
        self.assertNotIn("ok", result)
        saved = session.load_current()
        self.assertEqual([entry["operation"] for entry in saved["consent_log"]],
                         ["mitm_proxy"])
        self.assertTrue(saved["network"]["enabled"])
        entry = self.journal(self.record)[-1]
        self.assertEqual(entry["argv"], ["canvas", "network.start", "--i-understand-mitm"])
        self.assertEqual(entry["result"], {"port": 8899})

    def test_start_keeps_the_preferred_fixture_port(self) -> None:
        self.record.setdefault("network", {})["preferred_proxy_port"] = 61093
        session.save(self.record)
        with mock.patch.object(proxy, "start", return_value=dict(RUNNING)) as start:
            self.ok("network.start", {"acknowledged": True})
        self.assertEqual(start.call_args.kwargs["port"], 61093)

    def test_attach_needs_a_running_proxy(self) -> None:
        self.refused("network.attach", {"acknowledged": True}, errors.PROXY_NOT_RUNNING)

    def test_attach_without_consent_changes_nothing(self) -> None:
        with mock.patch.object(proxy, "status", return_value=dict(RUNNING)):
            self.refused("network.attach", {}, errors.CONSENT_REQUIRED)
        self.assertFalse(any("put" in argv for argv in self.calls()))

    def test_attach_detach_and_stop(self) -> None:
        with mock.patch.object(proxy, "status", return_value=dict(RUNNING)):
            result = self.ok("network.attach", {"acknowledged": True, "install_ca": True,
                                                "system_ca": True})
        self.assertEqual(result["mode"], "automated")
        self.assertNotIn("ca_installed", result)
        self.assertEqual(json.loads(self.state.read_text())["settings"]["http_proxy"],
                         "10.0.2.2:8899")
        self.assertTrue(session.load_current()["network"]["attached"])
        detached = self.ok("network.detach")
        self.assertTrue(detached["was_attached"])
        self.assertFalse(session.load_current()["network"]["attached"])
        stopped = self.ok("network.stop")
        self.assertFalse(stopped["was_running"])
        self.assertFalse(session.load_current()["network"]["enabled"])
        self.assertEqual([entry["verb"] for entry in self.journal(self.record)],
                         ["canvas network.attach", "canvas network.detach",
                          "canvas network.stop"])


# --- mocks (wc-005) ------------------------------------------------------------------


    def test_capture_starts_and_attaches_once_and_reuses_verified_attachment(self) -> None:
        with mock.patch.object(proxy, "start", return_value=dict(RUNNING)) as start, \
                mock.patch.object(tools_mod.cli, "network_attach_payload",
                                  return_value={"ok": True, "mode": "automated"}) as attach, \
                mock.patch.object(tools_mod.network_attachment, "attachment_evidence",
                                  side_effect=[{"attached": False}, {"attached": True}]):
            result = self.ok("network.capture", {"acknowledged": True})
        self.assertTrue(result["attached"])
        self.assertFalse(result["attachment_reused"])
        self.assertEqual(start.call_count, 1)
        self.assertEqual(attach.call_count, 1)
        self.assertTrue(attach.call_args.kwargs["acknowledged"])
        self.assertNotIn("install_ca", attach.call_args.kwargs)
        self.assertIn("canvas network.capture", " ".join(self.journal(self.record)[-1]["argv"]))
        with mock.patch.object(proxy, "start", return_value=dict(RUNNING)), \
                mock.patch.object(tools_mod.cli, "network_attach_payload",
                                  side_effect=AssertionError("re-attached")), \
                mock.patch.object(tools_mod.network_attachment, "attachment_evidence",
                                  return_value={"attached": True, "evidence": "transparent_proxy"}):
            reused = self.ok("network.capture", {"acknowledged": True})
        self.assertTrue(reused["attachment_reused"])

    def test_capture_consent_and_partial_failure_do_not_claim_capture(self) -> None:
        with mock.patch.object(proxy, "start", side_effect=AssertionError("started")):
            self.refused("network.capture", {}, errors.CONSENT_REQUIRED)
        with mock.patch.object(proxy, "start", return_value=dict(RUNNING)), \
                mock.patch.object(tools_mod.network_attachment, "attachment_evidence",
                                  return_value={"attached": False}), \
                mock.patch.object(tools_mod.cli, "network_attach_payload",
                                  side_effect=errors.AutonomError(errors.BACKEND_FAILED, "connection failed")), \
                mock.patch.object(proxy, "stop", side_effect=AssertionError("stopped existing proxy")):
            reply = self.refused("network.capture", {"acknowledged": True}, errors.BACKEND_FAILED)
        self.assertIn("Proxy is running", reply["error"])
        self.assertFalse(self.journal(self.record)[-1]["ok"])


class MockTests(ToolsCase):
    def registry(self) -> list[dict]:
        return mocks_mod.load()

    def add(self, **payload: object) -> dict:
        payload.setdefault("url_glob", "*/autonom-tools-test*")
        return self.ok("mocks.add", payload)

    def test_add_list_hits_and_remove(self) -> None:
        record = self.start_session()
        mock_rule = self.add(status=418, body="mocked")
        identifier = mock_rule["id"]
        self.assertEqual(mock_rule["mock"]["id"], identifier)
        self.assertEqual(mock_rule["response"]["status"], 418)
        self.assertTrue(mock_rule["registry"].endswith("registry.json"))
        body_path = Path(mock_rule["response"]["body_path"])
        self.assertEqual(body_path.read_text(encoding="utf-8"), "mocked")
        self.write_flows(record, [dict(FLOWS[1], mock_id=identifier)])
        listed = self.ok("mocks.list")
        self.assertEqual([(rule["id"], rule["hits"]) for rule in listed["mocks"]],
                         [(identifier, 1)])
        self.assertTrue(listed["registry"].endswith("registry.json"))
        self.assertEqual(self.ok("mocks.remove", {"id": identifier})["removed"], identifier)
        self.assertEqual(self.registry(), [])
        self.assertFalse(body_path.exists())

    def test_hits_count_only_the_session_on_this_target(self) -> None:
        other = self.start_session(OTHER_SERIAL)
        identifier = self.add()["id"]
        self.write_flows(other, [dict(FLOWS[1], mock_id=identifier)])
        self.assertEqual(self.ok("mocks.list")["mocks"][0]["hits"], 0)

    def test_a_json_body_gets_a_json_content_type(self) -> None:
        rule = self.add(body='{"a": 1}')
        self.assertEqual(rule["response"]["headers"], {"Content-Type": "application/json"})
        # Other headers do not stop the JSON type (A.5)...
        rule = self.add(body='{"a": 1}', headers={"X-Test": "1"})
        self.assertEqual(rule["response"]["headers"],
                         {"X-Test": "1", "Content-Type": "application/json"})
        # ...but an explicit Content-Type still wins, in any letter case.
        rule = self.add(body='{"a": 1}', headers={"Content-Type": "text/plain"})
        self.assertEqual(rule["response"]["headers"], {"Content-Type": "text/plain"})
        rule = self.add(body='{"a": 1}', headers={"content-type": "text/plain", "X-Test": "1"})
        self.assertEqual(rule["response"]["headers"],
                         {"content-type": "text/plain", "X-Test": "1"})
        self.assertEqual(self.add()["response"]["status"], 200)

    def test_an_empty_or_blank_body_gets_no_json_content_type(self) -> None:
        for body in ("", "   ", "\n\t"):
            with self.subTest(body=body):
                rule = self.add(status=204, body=body)
                self.assertEqual(rule["response"]["headers"], {})
        rule = self.add(body="  [1]")
        self.assertEqual(rule["response"]["headers"], {"Content-Type": "application/json"})

    def test_validation_leaves_the_registry_unchanged(self) -> None:
        self.add(note="keep")
        before = json.loads(mocks_mod.registry_file().read_text(encoding="utf-8"))
        cases = [
            ({"status": 99}, errors.INVALID_VALUE),
            ({"status": 600}, errors.INVALID_VALUE),
            ({"status": "200"}, errors.FLOW_COMMAND_INVALID),
            ({"status": True}, errors.FLOW_COMMAND_INVALID),
            ({"body": "é" * 16385}, errors.INVALID_VALUE),  # 32770 bytes in UTF-8
            ({"body": "{bad json"}, errors.INVALID_VALUE),
            ({"body": 5}, errors.FLOW_COMMAND_INVALID),
            ({"headers": ["X: 1"]}, errors.FLOW_COMMAND_INVALID),
            ({"headers": {"X": 1}}, errors.FLOW_COMMAND_INVALID),
            ({"headers": {"": "1"}}, errors.FLOW_COMMAND_INVALID),
            ({"url_glob": ""}, errors.SELECTOR_REQUIRED),
            ({"url_glob": "   "}, errors.SELECTOR_REQUIRED),
            ({"url_glob": None}, errors.SELECTOR_REQUIRED),
            ({"url_glob": 5}, errors.FLOW_COMMAND_INVALID),
            ({"method": "GET /x"}, errors.FLOW_COMMAND_INVALID),
            ({"ignore_query": "yes"}, errors.FLOW_COMMAND_INVALID),
            ({"note": "x" * 600}, errors.FLOW_COMMAND_INVALID),
        ]
        for extra, code in cases:
            with self.subTest(extra=extra):
                payload = {"url_glob": "*/x*", **extra}
                self.refused("mocks.add", payload, code)
        self.refused("mocks.add", {"status": 200}, errors.SELECTOR_REQUIRED)
        after = json.loads(mocks_mod.registry_file().read_text(encoding="utf-8"))
        self.assertEqual(after, before)
        self.assertEqual(len(list(mocks_mod.bodies_dir().iterdir())), 0)

    def test_a_body_of_exactly_32_kib_is_accepted(self) -> None:
        self.add(body="a" * (32 * 1024))

    def test_update_enable_disable(self) -> None:
        first = self.add(status=200, body="one", method="get", host="api.example.com")
        second = self.add(url_glob="*/two*")
        self.assertEqual(first["match"]["method"], "GET")
        updated = self.ok("mocks.update", {"id": first["id"], "status": 503,
                                           "body": "two", "note": "edited"})
        self.assertEqual(updated["response"]["status"], 503)
        self.assertEqual(updated["note"], "edited")
        self.assertEqual(updated["match"]["url_glob"], "*/autonom-tools-test*")
        self.assertEqual(Path(updated["response"]["body_path"]).read_text(), "two")
        cleared = self.ok("mocks.update", {"id": first["id"], "method": None, "host": ""})
        # a field sent as null or "" is cleared; a field not sent is unchanged
        self.assertIsNone(cleared["match"]["host"])
        self.assertIsNone(cleared["match"]["method"])
        self.assertEqual(cleared["response"]["status"], 503)
        self.assertFalse(self.ok("mocks.disable", {"id": second["id"]})["enabled"])
        self.assertFalse([r for r in self.registry() if r["id"] == second["id"]][0]["enabled"])
        enabled = self.ok("mocks.enable", {"id": second["id"]})
        self.assertTrue(enabled["enabled"])
        self.assertTrue(enabled["mock"]["enabled"])
        listed = {rule["id"]: rule for rule in self.ok("mocks.list")["mocks"]}
        self.assertEqual(listed[first["id"]]["response"]["status"], 503)

    def test_update_validation(self) -> None:
        identifier = self.add()["id"]
        self.refused("mocks.update", {"id": identifier, "status": 600}, errors.INVALID_VALUE)
        self.refused("mocks.update", {"id": identifier, "url_glob": ""},
                     errors.SELECTOR_REQUIRED)
        self.refused("mocks.update", {"status": 200}, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.registry()[0]["response"]["status"], 200)

    def test_unknown_ids_are_mock_not_found(self) -> None:
        for op in ("mocks.update", "mocks.enable", "mocks.disable", "mocks.remove"):
            with self.subTest(op=op):
                self.refused(op, {"id": "m_999", "status": 200}, errors.MOCK_NOT_FOUND)
                self.refused(op, {"id": 3}, errors.FLOW_COMMAND_INVALID)

    def test_clear_removes_everything(self) -> None:
        self.add()
        self.add(url_glob="*/two*", body="x")
        result = self.ok("mocks.clear")
        self.assertEqual(result["removed"], 2)
        self.assertEqual(self.registry(), [])
        self.assertEqual(self.ok("mocks.list")["mocks"], [])

    def test_mock_bodies_are_never_journaled(self) -> None:
        record = self.start_session()
        rule = self.add(body=SECRET, headers={"X-Token": "header-value"}, note="a note",
                        url_glob="https://api.example.com/v1/x?token=abc")
        self.ok("mocks.update", {"id": rule["id"], "body": SECRET + "2"})
        self.ok("mocks.disable", {"id": rule["id"]})
        self.ok("mocks.clear", origin="agent")
        entries = self.journal(record)
        self.assertEqual([entry["verb"] for entry in entries],
                         ["canvas mocks.add", "canvas mocks.update", "canvas mocks.disable",
                          "canvas mocks.clear"])
        self.assertEqual([entry["origin"] for entry in entries],
                         ["human", "human", "human", "agent"])
        text = self.journal_text(record)
        for secret in (SECRET, "header-value", "a note", "token=abc"):
            self.assertNotIn(secret, text)
        self.assertIn(f"body_len={len(SECRET)}", entries[0]["argv"])
        self.assertIn("headers=1", entries[0]["argv"])

    def test_mocks_need_no_session_and_journal_nothing_without_one(self) -> None:
        other = self.start_session(OTHER_SERIAL)
        self.add(body="x")
        self.assertEqual(self.journal(other), [])


# --- mock conditions, rewrite edits and timing (MR-12, MR-13) ------------------------

REWRITE_RULE = {
    "url_glob": "*/profile*", "mode": "rewrite", "method": "get", "host": "api.example.com",
    "status": 503, "headers": {"Retry-After": "5"}, "note": "edit the profile",
    "query": [{"name": "page", "value": "2"}, {"name": "debug", "value": None}],
    "request_headers": [{"name": "X-Client", "value": "ios*"}],
    "body_contains": ["needle"],
    "body_json": [{"path": "user.role", "value": "admin"}],
    "remove_headers": ["ETag"],
    "json_set": [{"path": "user.plan", "value": "pro"}],
    "json_remove": ["user.trial_ends"],
    "replace": [{"find": "Free", "with": "Pro"}],
    "delay_s": 1.5,
}
# Where a stored rule keeps each list key.
LIST_KEYS = {
    "query": ("match", "query"), "request_headers": ("match", "headers"),
    "body_contains": ("match", "body", "contains"), "body_json": ("match", "body", "json"),
    "remove_headers": ("rewrite", "remove_headers"), "json_set": ("rewrite", "json_set"),
    "json_remove": ("rewrite", "json_remove"), "replace": ("rewrite", "replace"),
}


def stored_at(rule: dict, path: tuple[str, ...]) -> object:
    for step in path:
        rule = rule.get(step) if isinstance(rule, dict) else None
    return rule


class MockRuleFieldTests(ToolsCase):
    """`mocks.add` / `mocks.update` keys beyond the response: mode, request
    conditions, rewrite edits and timing; the absent-key rule on update;
    masking in every reply; counts-only journal words."""

    def add(self, **payload: object) -> dict:
        payload.setdefault("url_glob", "*/autonom-tools-test*")
        return self.ok("mocks.add", payload)

    def add_full(self) -> dict:
        return self.add(**{**REWRITE_RULE})

    def registry(self) -> list[dict]:
        return mocks_mod.load()

    def stored(self, identifier: str) -> dict:
        (rule,) = [rule for rule in self.registry() if rule["id"] == identifier]
        return rule

    def registry_text(self) -> str:
        return mocks_mod.registry_file().read_text(encoding="utf-8")

    def without_stamp(self, rule: dict) -> dict:
        return {key: value for key, value in rule.items() if key != "updated_at"}

    # --- add: every key ---------------------------------------------------------

    def test_add_stores_every_key(self) -> None:
        reply = self.add_full()
        rule = self.stored(reply["id"])
        self.assertEqual(rule["mode"], "rewrite")
        self.assertEqual(rule["match"]["method"], "GET")
        self.assertEqual(rule["match"]["host"], "api.example.com")
        self.assertEqual(rule["response"]["status"], 503)
        self.assertEqual(rule["response"]["headers"], {"Retry-After": "5"})
        self.assertEqual(rule["match"]["query"], REWRITE_RULE["query"])
        self.assertEqual(rule["match"]["headers"], REWRITE_RULE["request_headers"])
        self.assertEqual(rule["match"]["body"],
                         {"contains": ["needle"], "json": REWRITE_RULE["body_json"]})
        self.assertEqual(rule["rewrite"], {
            "remove_headers": ["ETag"], "json_set": REWRITE_RULE["json_set"],
            "json_remove": ["user.trial_ends"], "replace": REWRITE_RULE["replace"]})
        self.assertEqual(rule["delay_s"], 1.5)
        self.assertNotIn("hang", rule)
        self.assertEqual(reply["mock"]["mode"], "rewrite")

    def test_each_key_alone(self) -> None:
        cases = [
            ({"query": [{"name": "page", "value": "2"}]}, ("match", "query"),
             [{"name": "page", "value": "2"}]),
            ({"query": [{"name": "debug", "value": None}]}, ("match", "query"),
             [{"name": "debug", "value": None}]),
            ({"request_headers": [{"name": "X-Client", "value": None}]}, ("match", "headers"),
             [{"name": "X-Client", "value": None}]),
            ({"body_contains": ["a", "b"]}, ("match", "body", "contains"), ["a", "b"]),
            ({"body_json": [{"path": "items.0.id", "value": 7}]}, ("match", "body", "json"),
             [{"path": "items.0.id", "value": 7}]),
            ({"mode": "rewrite", "remove_headers": [" ETag "]}, ("rewrite", "remove_headers"),
             ["ETag"]),
            ({"mode": "rewrite", "json_set": [{"path": "a.b", "value": {"c": [1, None]}}]},
             ("rewrite", "json_set"), [{"path": "a.b", "value": {"c": [1, None]}}]),
            ({"mode": "rewrite", "json_remove": ["a.0"]}, ("rewrite", "json_remove"), ["a.0"]),
            ({"mode": "rewrite", "replace": [{"find": "x", "with": ""}]}, ("rewrite", "replace"),
             [{"find": "x", "with": ""}]),
            ({"mode": "rewrite"}, ("mode",), "rewrite"),
            ({"mode": "respond"}, ("mode",), None),
            ({"delay_s": 2}, ("delay_s",), 2.0),
            ({"hang": True}, ("hang",), True),
        ]
        for extra, path, expected in cases:
            with self.subTest(extra=extra):
                rule = self.stored(self.add(**extra)["id"])
                self.assertEqual(stored_at(rule, path), expected)

    def test_a_rewrite_rule_without_a_status_keeps_the_real_status(self) -> None:
        rule = self.stored(self.add(mode="rewrite", json_set=[{"path": "a", "value": 1}])["id"])
        self.assertIsNone(rule["response"]["status"])
        # A fake response still defaults to 200, a null status included.
        self.assertEqual(self.stored(self.add()["id"])["response"]["status"], 200)
        self.assertEqual(self.stored(self.add(status=None)["id"])["response"]["status"], 200)
        self.assertEqual(self.stored(self.add(mode="respond")["id"])["response"]["status"], 200)

    def test_a_rule_without_the_new_keys_is_stored_as_before(self) -> None:
        rule = self.stored(self.add(status=418, body="x")["id"])
        for absent in ("mode", "rewrite", "delay_s", "hang"):
            self.assertNotIn(absent, rule)
        self.assertEqual(set(rule["match"]),
                         {"url_glob", "method", "host", "ignore_query"})
        listed = self.ok("mocks.list")["mocks"][0]
        self.assertEqual({key: value for key, value in listed.items() if key != "hits"}, rule)

    def test_null_and_empty_conditions_store_nothing(self) -> None:
        rule = self.stored(self.add(query=None, request_headers=[], body_contains=[],
                                    body_json=None)["id"])
        self.assertEqual(set(rule["match"]),
                         {"url_glob", "method", "host", "ignore_query"})

    # --- update: the absent-key rule -------------------------------------------

    def test_update_keeps_conditions_when_only_the_status_is_sent(self) -> None:
        """MR-13-S01."""
        rule = self.add(query=[{"name": "a", "value": "1"}, {"name": "b", "value": "2"}],
                        body_json=[{"path": "user.id", "value": 7}])
        before = self.stored(rule["id"])
        updated = self.ok("mocks.update", {"id": rule["id"], "status": 503})
        self.assertEqual(updated["response"]["status"], 503)
        stored = self.stored(rule["id"])
        self.assertEqual(stored["match"]["query"], before["match"]["query"])
        self.assertEqual(stored["match"]["body"], before["match"]["body"])
        self.assertEqual(len(stored["match"]["query"]) + len(stored["match"]["body"]["json"]), 3)

    def test_update_without_a_key_changes_nothing_else(self) -> None:
        rule = self.add_full()
        updates = [({"status": 504}, ("response", "status"), 504),
                   ({"note": "n"}, ("note",), "n"),
                   ({"method": "post"}, ("match", "method"), "POST"),
                   ({"headers": {"X-A": "1"}}, ("response", "headers"), {"X-A": "1"}),
                   ({"ignore_query": True}, ("match", "ignore_query"), True)]
        for extra, path, value in updates:
            with self.subTest(extra=extra):
                before = self.without_stamp(self.stored(rule["id"]))
                self.ok("mocks.update", {"id": rule["id"], **extra})
                after = self.without_stamp(self.stored(rule["id"]))
                expected = json.loads(json.dumps(before))
                target = expected
                for step in path[:-1]:
                    target = target[step]
                target[path[-1]] = value
                self.assertEqual(after, expected)

    def test_a_null_list_or_timing_is_the_same_as_absent(self) -> None:
        rule = self.add_full()
        before = self.without_stamp(self.stored(rule["id"]))
        self.ok("mocks.update", {"id": rule["id"], "mode": None, "delay_s": None, "hang": None,
                                 **{key: None for key in LIST_KEYS}})
        self.assertEqual(self.without_stamp(self.stored(rule["id"])), before)

    def test_an_empty_list_clears_only_that_list(self) -> None:
        for key, path in LIST_KEYS.items():
            with self.subTest(key=key):
                rule = self.add_full()
                before = self.without_stamp(self.stored(rule["id"]))
                self.assertTrue(stored_at(before, path), key)
                self.ok("mocks.update", {"id": rule["id"], key: []})
                after = self.without_stamp(self.stored(rule["id"]))
                self.assertIsNone(stored_at(after, path), key)
                for other, other_path in LIST_KEYS.items():
                    if other != key:
                        self.assertEqual(stored_at(after, other_path),
                                         stored_at(before, other_path), (key, other))
                self.assertEqual(after["mode"], "rewrite")
                self.assertEqual(after["delay_s"], 1.5)
                self.ok("mocks.remove", {"id": rule["id"]})

    def test_clearing_every_edit_leaves_no_rewrite_block_and_no_empty_body_block(self) -> None:
        rule = self.add_full()
        self.ok("mocks.update", {"id": rule["id"], "body_contains": [], "body_json": [],
                                 "remove_headers": [], "json_set": [], "json_remove": [],
                                 "replace": []})
        stored = self.stored(rule["id"])
        self.assertNotIn("rewrite", stored)
        self.assertNotIn("body", stored["match"])

    def test_a_list_that_is_sent_replaces_the_whole_list(self) -> None:
        rule = self.add_full()
        self.ok("mocks.update", {"id": rule["id"], "query": [{"name": "sort", "value": "asc"}],
                                 "json_set": [{"path": "n", "value": 2}]})
        stored = self.stored(rule["id"])
        self.assertEqual(stored["match"]["query"], [{"name": "sort", "value": "asc"}])
        self.assertEqual(stored["rewrite"]["json_set"], [{"path": "n", "value": 2}])
        self.assertEqual(stored["rewrite"]["remove_headers"], ["ETag"])

    def test_update_can_narrow_a_rule_with_a_condition_later(self) -> None:
        """MR-11-S01 through Canvas."""
        rule = self.add(url_glob="*/items*", query=[{"name": "page", "value": "2"}], status=500)
        self.ok("mocks.update", {"id": rule["id"],
                                 "request_headers": [{"name": "X-Client", "value": "ios*"}]})
        stored = self.stored(rule["id"])
        self.assertEqual(stored["match"]["query"], [{"name": "page", "value": "2"}])
        self.assertEqual(stored["match"]["headers"], [{"name": "X-Client", "value": "ios*"}])
        self.assertEqual(stored["response"]["status"], 500)

    # --- update: mode, status and body ------------------------------------------

    def test_mode_switches_with_the_edits_it_needs(self) -> None:
        rule = self.add(status=200)
        self.ok("mocks.update", {"id": rule["id"], "mode": "rewrite",
                                 "json_set": [{"path": "a", "value": 1}]})
        stored = self.stored(rule["id"])
        self.assertEqual(stored["mode"], "rewrite")
        self.assertEqual(stored["rewrite"]["json_set"], [{"path": "a", "value": 1}])
        before = self.registry_text()
        # Back to a fake response with the edits still stored: refused, unchanged.
        self.refused("mocks.update", {"id": rule["id"], "mode": "respond"}, errors.INVALID_VALUE)
        self.assertEqual(self.registry_text(), before)
        self.ok("mocks.update", {"id": rule["id"], "mode": "respond", "json_set": []})
        stored = self.stored(rule["id"])
        self.assertNotIn("mode", stored)
        self.assertNotIn("rewrite", stored)

    def test_update_edits_need_the_stored_or_sent_rewrite_mode(self) -> None:
        rule = self.add()
        before = self.registry_text()
        self.refused("mocks.update", {"id": rule["id"], "replace": [{"find": "a", "with": "b"}]},
                     errors.INVALID_VALUE)
        self.assertEqual(self.registry_text(), before)

    def test_clear_status_returns_a_rewrite_rule_to_the_real_status(self) -> None:
        rule = self.add(mode="rewrite", status=503, json_set=[{"path": "a", "value": 1}])
        self.assertEqual(self.stored(rule["id"])["response"]["status"], 503)
        self.ok("mocks.update", {"id": rule["id"], "clear_status": True})
        self.assertIsNone(self.stored(rule["id"])["response"]["status"])
        # A status in the same update is a conflict; a fake response needs one.
        plain = self.add(status=201)
        before = self.registry_text()
        self.refused("mocks.update", {"id": rule["id"], "status": 500, "clear_status": True},
                     errors.INVALID_VALUE)
        self.refused("mocks.update", {"id": plain["id"], "clear_status": True},
                     errors.INVALID_VALUE)
        self.assertEqual(self.registry_text(), before)
        self.assertEqual(self.stored(plain["id"])["response"]["status"], 201)

    def test_clear_body_drops_a_stored_body(self) -> None:
        rule = self.add(mode="rewrite", body="whole")
        path = Path(self.stored(rule["id"])["response"]["body_path"])
        self.assertTrue(path.exists())
        self.ok("mocks.update", {"id": rule["id"], "clear_body": True})
        self.assertIsNone(self.stored(rule["id"])["response"]["body_path"])
        self.assertFalse(path.exists())
        self.refused("mocks.update", {"id": rule["id"], "body": "x", "clear_body": True},
                     errors.INVALID_VALUE)
        for flag in ("clear_status", "clear_body"):
            self.refused("mocks.update", {"id": rule["id"], flag: "yes"},
                         errors.FLOW_COMMAND_INVALID)

    def test_a_new_body_and_new_body_edits_exclude_each_other(self) -> None:
        rule = self.add(mode="rewrite", json_set=[{"path": "a", "value": 1}])
        before = self.registry_text()
        self.refused("mocks.update", {"id": rule["id"], "body": "{}",
                                      "json_remove": ["a"]}, errors.INVALID_VALUE)
        self.assertEqual(self.registry_text(), before)
        # A whole body alone replaces the stored body edits.
        self.ok("mocks.update", {"id": rule["id"], "body": "{}"})
        self.assertNotIn("rewrite", self.stored(rule["id"]))

    # --- timing ----------------------------------------------------------------

    def test_delay_and_hang_replace_each_other_on_update(self) -> None:
        rule = self.add(delay_s=2.5)
        self.assertEqual(self.stored(rule["id"])["delay_s"], 2.5)
        self.ok("mocks.update", {"id": rule["id"], "hang": True})
        stored = self.stored(rule["id"])
        self.assertIs(stored["hang"], True)
        self.assertNotIn("delay_s", stored)
        self.ok("mocks.update", {"id": rule["id"], "delay_s": 3})
        stored = self.stored(rule["id"])
        self.assertEqual(stored["delay_s"], 3.0)
        self.assertNotIn("hang", stored)
        self.ok("mocks.update", {"id": rule["id"], "hang": True})
        self.ok("mocks.update", {"id": rule["id"], "hang": False})
        stored = self.stored(rule["id"])
        self.assertNotIn("hang", stored)
        self.assertNotIn("delay_s", stored)
        self.ok("mocks.update", {"id": rule["id"], "delay_s": 1})
        self.ok("mocks.update", {"id": rule["id"], "note": "x"})
        self.assertEqual(self.stored(rule["id"])["delay_s"], 1.0)

    def test_a_rewrite_rule_takes_a_delay_but_never_a_hang(self) -> None:
        rule = self.add(mode="rewrite", delay_s=0.5)
        self.assertEqual(self.stored(rule["id"])["delay_s"], 0.5)
        held = self.add(hang=True)
        before = self.registry_text()
        self.refused("mocks.update", {"id": rule["id"], "hang": True}, errors.INVALID_VALUE)
        self.refused("mocks.add", {"url_glob": "*/x", "mode": "rewrite", "hang": True},
                     errors.INVALID_VALUE)
        # A held rule cannot become a rewrite rule without releasing the hang.
        self.refused("mocks.update", {"id": held["id"], "mode": "rewrite"}, errors.INVALID_VALUE)
        self.assertEqual(self.registry_text(), before)
        self.ok("mocks.update", {"id": held["id"], "mode": "rewrite", "hang": False})
        self.assertNotIn("hang", self.stored(held["id"]))
        self.assertEqual(self.stored(held["id"])["mode"], "rewrite")

    # --- refusals ---------------------------------------------------------------

    def test_refusals_leave_the_registry_and_bodies_unchanged(self) -> None:
        self.add(note="keep")
        before = self.registry_text()
        edit = {"path": "a", "value": 1}
        too_long = "x" * 2049
        shape, value = errors.FLOW_COMMAND_INVALID, errors.INVALID_VALUE
        cases = [
            ({"mode": 5}, shape),
            ({"mode": "both"}, value),
            ({"query": "page=2"}, shape),
            ({"query": ["page=2"]}, shape),
            ({"query": [{"name": "a"}, 5]}, shape),
            ({"query": [{"name": "", "value": "1"}]}, value),
            ({"query": [{"name": "a", "value": 2}]}, value),
            ({"query": [{"name": f"p{n}"} for n in range(21)]}, value),
            ({"query": [{"name": "a", "value": too_long}]}, value),
            ({"request_headers": {"name": "X"}}, shape),
            ({"request_headers": [{"name": "bad name", "value": None}]}, value),
            ({"request_headers": [{"name": "", "value": "x"}]}, value),
            ({"body_contains": "needle"}, shape),
            ({"body_contains": [5]}, shape),
            ({"body_contains": [""]}, value),
            ({"body_contains": [too_long]}, value),
            ({"body_json": ["a=1"]}, shape),
            ({"body_json": [{"path": "a"}]}, value),
            ({"body_json": [{"path": "a..b", "value": 1}]}, value),
            ({"body_json": [{"path": "", "value": 1}]}, value),
            ({"mode": "rewrite", "json_set": ["a=1"]}, shape),
            ({"mode": "rewrite", "json_set": [{"path": "a.", "value": 1}]}, value),
            ({"mode": "rewrite", "json_set": [{"path": "a", "value": float("nan")}]}, value),
            ({"mode": "rewrite", "json_set": [{"path": "a", "value": too_long}]}, value),
            ({"mode": "rewrite", "remove_headers": [{"name": "ETag"}]}, shape),
            ({"mode": "rewrite", "remove_headers": ["bad name"]}, value),
            ({"mode": "rewrite", "json_remove": "a.b"}, shape),
            ({"mode": "rewrite", "json_remove": [""]}, value),
            ({"mode": "rewrite", "replace": [["a", "b"]]}, shape),
            ({"mode": "rewrite", "replace": [{"find": "", "with": "x"}]}, value),
            ({"mode": "rewrite", "replace": [{"find": "a", "with": 5}]}, value),
            ({"mode": "rewrite", "replace": [{"find": "a"}]}, value),
            ({"mode": "rewrite", "headers": {"bad name": "1"}}, value),
            ({"remove_headers": ["ETag"]}, value),
            ({"json_set": [edit]}, value),
            ({"json_remove": ["a"]}, value),
            ({"mode": "respond", "replace": [{"find": "a", "with": "b"}]}, value),
            ({"mode": "rewrite", "body": "{}", "json_set": [edit]}, value),
            ({"mode": "rewrite", "hang": True}, value),
            ({"delay_s": "2"}, shape),
            ({"delay_s": True}, shape),
            ({"delay_s": float("nan")}, shape),
            ({"delay_s": float("inf")}, shape),
            ({"delay_s": 0}, value),
            ({"delay_s": -1}, value),
            ({"delay_s": 120.5}, value),
            ({"delay_s": 2, "hang": True}, value),
            ({"hang": "yes"}, shape),
            ({"hang": 1}, shape),
        ]
        for extra, code in cases:
            with self.subTest(extra=str(extra)[:100]):
                self.refused("mocks.add", {"url_glob": "*/x*", **extra}, code)
        self.assertEqual(self.registry_text(), before)
        self.assertEqual(len(list(mocks_mod.bodies_dir().iterdir())), 0)

    def test_update_refusals_leave_the_stored_rule_unchanged(self) -> None:
        rule = self.add_full()
        before = self.registry_text()
        for extra, code in [({"query": [{"name": "a", "value": 1}]}, errors.INVALID_VALUE),
                            ({"query": "a=1"}, errors.FLOW_COMMAND_INVALID),
                            ({"mode": "respond"}, errors.INVALID_VALUE),
                            ({"delay_s": 999}, errors.INVALID_VALUE),
                            ({"hang": True}, errors.INVALID_VALUE),
                            ({"json_set": [{"path": "", "value": 1}]}, errors.INVALID_VALUE),
                            ({"status": 99, "query": []}, errors.INVALID_VALUE)]:
            with self.subTest(extra=extra):
                self.refused("mocks.update", {"id": rule["id"], **extra}, code)
        self.assertEqual(self.registry_text(), before)

    def test_a_refusal_never_echoes_a_condition_or_edit_value(self) -> None:
        record = self.start_session()
        response = self.refused(
            "mocks.add",
            {"url_glob": "*/x", "query": [{"name": "token", "value": SECRET}],
             "json_set": [{"path": "auth.token", "value": SECRET}],
             "replace": [{"find": SECRET, "with": SECRET}]},
            errors.INVALID_VALUE)  # edits need --rewrite
        self.assertNotIn(SECRET, json.dumps(response))
        self.refused("mocks.add", {"url_glob": "*/x", "mode": "rewrite",
                                   "query": [{"name": "token", "value": 5}],
                                   "request_headers": [{"name": "Authorization", "value": SECRET}]},
                     errors.INVALID_VALUE)
        self.assertNotIn(SECRET, self.journal_text(record))
        self.assertEqual([entry["ok"] for entry in self.journal(record)], [False, False])

    def test_the_lists_share_a_32_kib_limit_and_the_body_keeps_its_own(self) -> None:
        big = [{"name": f"p{n}", "value": "v" * 2000} for n in range(20)]  # ~40 KB
        self.add(note="keep")
        before = self.registry_text()
        response = self.refused("mocks.add", {"url_glob": "*/x", "query": big}, errors.INVALID_VALUE)
        self.assertIn("32768", response["error"])
        self.assertEqual(self.registry_text(), before)
        self.add(query=big[:12])  # ~24 KB
        # Text counts in UTF-8 bytes (2 per "é"), not as \\u escapes.
        wide = [{"name": f"p{n}", "value": "é" * 800} for n in range(20)]  # ~32.6 KB
        self.add(query=wide)
        wide = [{"name": f"p{n}", "value": "é" * 1000} for n in range(20)]  # ~40.6 KB
        self.refused("mocks.add", {"url_glob": "*/x", "query": wide}, errors.INVALID_VALUE)
        # The 32 KiB body limit is unchanged, with conditions beside it.
        self.add(body="a" * (32 * 1024), query=big[:12])
        self.refused("mocks.add", {"url_glob": "*/x", "body": "é" * 16385, "query": big[:1]},
                     errors.INVALID_VALUE)

    def test_a_lone_surrogate_is_refused_not_a_backend_failure(self) -> None:
        self.add(note="keep")
        before = self.registry_text()
        bad = "\ud800"
        for extra in ({"query": [{"name": "a", "value": bad}]},
                      {"request_headers": [{"name": "X", "value": bad}]},
                      {"body_contains": [bad]},
                      {"body_json": [{"path": "a", "value": bad}]},
                      {"mode": "rewrite", "json_set": [{"path": "a", "value": bad}]},
                      {"mode": "rewrite", "replace": [{"find": bad, "with": "x"}]}):
            with self.subTest(extra=extra):
                self.refused("mocks.add", {"url_glob": "*/x", **extra}, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(self.registry_text(), before)
        self.assertEqual([path.name for path in mocks_mod.registry_dir().iterdir()
                          if path.name.endswith(".tmp")], [])

    def test_the_new_keys_need_an_existing_rule(self) -> None:
        self.refused("mocks.update", {"id": "m_999", "query": []}, errors.MOCK_NOT_FOUND)
        self.refused("mocks.update", {"query": []}, errors.FLOW_COMMAND_INVALID)

    # --- list and replies: masking and the round trip ---------------------------

    def secret_rule(self) -> dict:
        return self.add(
            url_glob="*/login*", mode="rewrite", status=401,
            headers={"Set-Cookie": "sid=" + SECRET, "X-Plain": "visible"},
            query=[{"name": "token", "value": SECRET}, {"name": "page", "value": "2"}],
            request_headers=[{"name": "Authorization", "value": "Bearer " + SECRET},
                             {"name": "X-Client", "value": "ios*"}],
            body_json=[{"path": "user.password", "value": SECRET},
                       {"path": "user.role", "value": "admin"}],
            json_set=[{"path": "auth.token", "value": SECRET},
                      {"path": "user.plan", "value": "pro"}],
            remove_headers=["ETag"], delay_s=1.5)

    def test_list_returns_every_stored_field_with_secrets_masked(self) -> None:
        reply = self.secret_rule()
        stored = self.stored(reply["id"])
        self.assertIn(SECRET, self.registry_text())
        listed = self.ok("mocks.list")
        (rule,) = listed["mocks"]
        self.assertNotIn(SECRET, json.dumps(listed))
        placeholder = "<redacted>"
        self.assertEqual(rule["match"]["query"],
                         [{"name": "token", "value": placeholder}, {"name": "page", "value": "2"}])
        self.assertEqual(rule["match"]["headers"],
                         [{"name": "Authorization", "value": placeholder},
                          {"name": "X-Client", "value": "ios*"}])
        self.assertEqual(rule["match"]["body"]["json"],
                         [{"path": "user.password", "value": placeholder},
                          {"path": "user.role", "value": "admin"}])
        self.assertEqual(rule["rewrite"]["json_set"],
                         [{"path": "auth.token", "value": placeholder},
                          {"path": "user.plan", "value": "pro"}])
        self.assertEqual(rule["response"]["headers"],
                         {"Set-Cookie": placeholder, "X-Plain": "visible"})
        # Everything else is the stored rule, and the hits count is added.
        unmasked = {key: value for key, value in rule.items()
                    if key not in ("match", "rewrite", "response", "hits")}
        self.assertEqual(unmasked, {key: value for key, value in stored.items()
                                    if key not in ("match", "rewrite", "response")})
        self.assertEqual(rule["hits"], 0)
        self.assertEqual(rule["rewrite"]["remove_headers"], ["ETag"])
        self.assertEqual(rule["delay_s"], 1.5)
        self.assertEqual(rule["mode"], "rewrite")

    def test_replies_mask_secrets_too(self) -> None:
        reply = self.secret_rule()
        self.assertNotIn(SECRET, json.dumps(reply))
        self.assertEqual(reply["mock"], {key: value for key, value in reply.items()
                                         if key not in ("mock", "registry")})
        updated = self.ok("mocks.update", {"id": reply["id"], "note": "n"})
        self.assertNotIn(SECRET, json.dumps(updated))
        for op in ("mocks.disable", "mocks.enable"):
            self.assertNotIn(SECRET, json.dumps(self.ok(op, {"id": reply["id"]})))
        self.assertIn(SECRET, self.registry_text())  # the proxy still gets the real value

    def test_a_list_saved_back_unchanged_stores_the_same_rule(self) -> None:
        """MR-14-S02 at the bridge: what the form was shown, sent back, changes nothing."""
        rule = self.secret_rule()
        self.ok("mocks.update", {"id": rule["id"], "json_remove": ["user.t"],
                                 "replace": [{"find": "Free", "with": "Pro"}],
                                 "body_contains": ["needle"]})
        before = self.stored(rule["id"])
        (shown,) = self.ok("mocks.list")["mocks"]
        payload = {
            "id": shown["id"], "url_glob": shown["match"]["url_glob"],
            "method": shown["match"]["method"] or "", "host": shown["match"]["host"] or "",
            "ignore_query": shown["match"]["ignore_query"], "mode": shown["mode"],
            "status": shown["response"]["status"], "headers": shown["response"]["headers"],
            "query": shown["match"]["query"], "request_headers": shown["match"]["headers"],
            "body_contains": shown["match"]["body"]["contains"],
            "body_json": shown["match"]["body"]["json"],
            **{key: shown["rewrite"][key] for key in
               ("remove_headers", "json_set", "json_remove", "replace")},
            "delay_s": shown["delay_s"], "note": shown["note"] or "",
        }
        self.ok("mocks.update", payload)
        after = self.stored(rule["id"])
        self.assertEqual(self.without_stamp(after), self.without_stamp(before))
        self.assertIn(SECRET, json.dumps(after))
        self.assertEqual(after["match"]["headers"][0]["value"], "Bearer " + SECRET)
        self.assertEqual(after["response"]["headers"]["Set-Cookie"], "sid=" + SECRET)

    def test_a_hand_edited_rule_still_lists(self) -> None:
        rule = self.add(query=[{"name": "page", "value": "2"}])
        rules = self.registry()
        rules[0]["match"]["query"] = "not-a-list"
        rules[0]["match"]["body"] = 5
        rules[0]["rewrite"] = ["bad"]
        mocks_mod.save(rules)
        (listed,) = self.ok("mocks.list")["mocks"]
        self.assertEqual(listed["id"], rule["id"])

    # --- journal ----------------------------------------------------------------

    def test_journal_words_are_names_and_counts_only(self) -> None:
        record = self.start_session()
        reply = self.add(
            url_glob="*/login*", mode="rewrite", status=401, delay_s=1.5,
            headers={"X-Plain": SECRET},
            query=[{"name": "token", "value": SECRET}, {"name": SECRET, "value": None}],
            request_headers=[{"name": "Authorization", "value": SECRET}],
            body_contains=[SECRET], body_json=[{"path": "a", "value": SECRET}],
            remove_headers=["ETag", "Server"],
            json_set=[{"path": "auth.token", "value": SECRET}], json_remove=["x"],
            replace=[{"find": SECRET, "with": SECRET}], note=SECRET)
        self.ok("mocks.update", {"id": reply["id"], "query": [], "json_set": [], "status": 500,
                                 "clear_body": True, "delay_s": 2})
        self.ok("mocks.update", {"id": reply["id"], "mode": "respond", "remove_headers": [],
                                 "json_remove": [], "replace": [], "hang": True})
        self.refused("mocks.add", {"url_glob": "*/x", "query": [{"name": "a", "value": SECRET}],
                                   "replace": [{"find": SECRET, "with": SECRET}]},
                     errors.INVALID_VALUE)
        entries = self.journal(record)
        self.assertEqual([entry["verb"] for entry in entries],
                         ["canvas mocks.add", "canvas mocks.update", "canvas mocks.update",
                          "canvas mocks.add"])
        argv = entries[0]["argv"]
        for word in ("query=2", "request_header=1", "body_contains=1", "body_json=1",
                     "remove_headers=2", "json_set=1", "json_remove=1", "replace=1",
                     "mode=rewrite", "status=401", "delay_s=1.5", "headers=1", reply["id"]):
            self.assertIn(word, argv)
        # Clearing a list records the count 0, never what it held.
        self.assertIn("query=0", entries[1]["argv"])
        self.assertIn("json_set=0", entries[1]["argv"])
        self.assertIn("clear_body", entries[1]["argv"])
        self.assertIn("delay_s=2", entries[1]["argv"])
        self.assertIn("mode=respond", entries[2]["argv"])
        self.assertIn("hang=true", entries[2]["argv"])
        self.assertFalse(entries[3]["ok"])
        self.assertEqual(entries[3]["error_code"], errors.INVALID_VALUE)
        self.assertNotIn(SECRET, self.journal_text(record))

    def test_the_mock_ops_keep_their_origins(self) -> None:
        record = self.start_session()
        reply = self.add(query=[{"name": "a", "value": "1"}])
        self.ok("mocks.update", {"id": reply["id"], "query": []}, origin="agent")
        self.assertEqual([entry["origin"] for entry in self.journal(record)], ["human", "agent"])


# --- journal (wc-007) ----------------------------------------------------------------


class JournalTests(ToolsCase):
    def test_read_ops_are_not_journaled(self) -> None:
        record = self.start_session()
        for op in ("context", "location.get", "biometric.status", "network.status", "network.requests",
                   "mocks.list"):
            self.ok(op)
        self.assertEqual(self.journal(record), [])

    def test_a_failed_mutation_is_journaled_with_its_code(self) -> None:
        record = self.start_session()
        self.refused("location.clear", {}, errors.UNSUPPORTED_ON_PLATFORM, origin="replay")
        (entry,) = self.journal(record)
        self.assertEqual(entry["verb"], "canvas location.clear")
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error_code"], errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(entry["origin"], "replay")

    def test_a_session_on_another_target_gets_nothing(self) -> None:
        other = self.start_session(OTHER_SERIAL)
        self.ok("location.set", {"latitude": 1, "longitude": 2})
        self.ok("simulate", {"control": "appearance", "action": "dark"})
        self.assertEqual(self.journal(other), [])
        self.assertNotIn("location_requested", session.load_current())

    def test_every_origin_is_recorded(self) -> None:
        record = self.start_session()
        for origin in tools_mod.ORIGINS:
            self.ok("simulate", {"control": "appearance", "action": "dark"}, origin=origin)
        self.assertEqual([entry["origin"] for entry in self.journal(record)],
                         list(tools_mod.ORIGINS))


if __name__ == "__main__":
    unittest.main()

class CaptureFolderTests(ToolsCase):
    def test_opens_only_the_fixed_device_folder_in_finder(self):
        with mock.patch.object(tools_mod.sys, "platform", "darwin"), \
                mock.patch.object(tools_mod.subprocess, "run") as opened:
            result = self.ok("captures.open_directory")
            self.assertTrue(result["opened"])
            self.assertTrue(Path(result["dir"]).is_dir())
            opened.assert_called_once_with(
                ["/usr/bin/open", "-a", "Finder", result["dir"]],
                check=True, capture_output=True, text=True, timeout=10)
            self.refused("captures.open_directory", {"path": "/private"}, errors.FLOW_COMMAND_INVALID)
            self.assertEqual(opened.call_count, 1)

    def test_non_macos_and_launch_failure_are_explicit(self):
        with mock.patch.object(tools_mod.sys, "platform", "linux"), \
                mock.patch.object(tools_mod.subprocess, "run") as opened:
            self.refused("captures.open_directory", {}, errors.UNSUPPORTED_CAPABILITY)
            opened.assert_not_called()
        with mock.patch.object(tools_mod.sys, "platform", "darwin"), \
                mock.patch.object(tools_mod.subprocess, "run", side_effect=OSError("unavailable")):
            self.refused("captures.open_directory", {}, errors.BACKEND_FAILED)
