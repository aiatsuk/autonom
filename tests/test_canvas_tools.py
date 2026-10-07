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
            "context", "permissions.list", "location.get", "network.status",
            "network.requests", "network.request", "mocks.list"})
        self.assertEqual(set(tools_mod.MUTATING_OPS), {
            "permissions.set", "location.set", "location.clear", "simulate",
            "network.start", "network.attach", "network.detach", "network.stop",
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
            "biometric": ["match"], "battery": ["set", "reset"],
            "network": ["online", "offline"], "appearance": ["light", "dark"]})
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
            "battery": ["set", "reset"], "appearance": ["light", "dark"]})

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
    def test_controls_outside_the_allowlist_never_reach_the_device(self) -> None:
        for control in ("clipboard", "status-bar", "sms", "call", "keyboard", "animations",
                        "text-size", None, 3):
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

    OPS = ("network.status", "network.requests", "network.request", "network.start",
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
        rule = self.add(body='{"a": 1}', headers={"X-Test": "1"})
        self.assertEqual(rule["response"]["headers"], {"X-Test": "1"})
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


# --- journal (wc-007) ----------------------------------------------------------------


class JournalTests(ToolsCase):
    def test_read_ops_are_not_journaled(self) -> None:
        record = self.start_session()
        for op in ("context", "location.get", "network.status", "network.requests",
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
