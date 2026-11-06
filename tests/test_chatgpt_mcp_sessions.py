"""Autonom MCP adapter session tools: discovery, lifecycle, reads, mutations, control.

Contract section 3 (tests of 3.8). The real server runs as a subprocess over
stdio with ``AUTONOM_MCP_PARTIAL_CATALOG=1``, the test-only seam of 2.10 test 4
and the fake CLI (``tests/fakes/fake_mcp_cli.py`` through ``AUTONOM_CLI``);
the one case that needs the lease keeper itself builds the same server in
process. ``HOME``, ``AUTONOM_HOME`` and ``XDG_STATE_HOME`` are temp dirs for
the server and every child; no test touches a device or a host service.
Every result is checked against its tool's ``outputSchema`` and scanned for
the temp root, so no path ever leaves the server.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_chatgpt_mcp_core as core  # noqa: E402

FAKE = core.FAKE
MISSING = core.MISSING
IOS_UDID = core.IOS_UDID
OTHER_HOST = core.OTHER_HOST
ANDROID = "emulator-5584"
SECRET = "hunter2-secret"
SESSION_TOOLS = ["list_devices", "list_sessions", "get_session", "start_session", "stop_session",
                 "get_screen", "get_ui_tree", "tap_element", "type_text", "press_key",
                 "acquire_control", "release_control", "get_operation"]
MUTATING = {"start_session", "stop_session", "tap_element", "type_text", "press_key", "acquire_control",
            "release_control"}
# `devices list` entries in the merged CLI's shapes (adb.Device.as_dict and
# ios_simctl.Simulator.as_dict, plus the `avd` name of a running emulator).
RECORDED_DEVICES = [
    {"serial": ANDROID, "target_id": ANDROID, "state": "device", "running": True, "platform": "android",
     "name": "sdk_gphone64_arm64", "properties": {"model": "sdk_gphone64_arm64", "product": "sdk"},
     "avd": "Autonom_Split_API36"},
    {"serial": "emulator-5590", "target_id": "emulator-5590", "state": "offline", "running": False,
     "platform": "android", "name": "emulator-5590", "properties": {}},
    {"platform": "ios", "target_id": IOS_UDID, "udid": IOS_UDID, "state": "Booted", "running": True,
     "name": "Autonom-Split-Test", "runtime": "iOS 26.0", "properties": {"is_available": True}},
    {"platform": "ios", "target_id": "11111111-2222-3333-4444-555555555555",
     "udid": "11111111-2222-3333-4444-555555555555", "state": "Shutdown", "running": False,
     "name": "Spare", "runtime": "iOS 26.0", "properties": {"is_available": True}},
    {"platform": "windows", "target_id": "nope"},
]
# The stopping hooks this file's stop test expects, in order: core's test_run,
# the recording hooks it registers through test_hooks, then the agent lease.
TEST_STOP_HOOKS = ["test_run", "cancel_runs", "close_streams", "detach_canvas", "agent_lease"]
DEVICE_KEYS = {"host_id", "platform", "target_id", "name", "state", "avd", "session_id"}


class SessionsCase(core.CoreCase):
    """Server helpers that keep replies of overlapping calls instead of dropping them."""

    def started(self, **overrides: Any) -> core.ServerProcess:
        server = super().started(**overrides)
        server.stash = {}  # type: ignore[attr-defined]
        if not hasattr(self, "_tool_defs"):
            self._tool_defs = self.schemas(server)
        return server

    def send_call(self, server: core.ServerProcess, name: str, arguments: Any = MISSING) -> Any:
        msg_id = "c%d" % server.new_id()
        params: Dict[str, Any] = {"name": name}
        if arguments is not MISSING:
            params["arguments"] = arguments
        server.send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params})
        return msg_id

    def await_reply(self, server: core.ServerProcess, msg_id: Any, timeout: float = 30.0) -> Dict[str, Any]:
        stash = server.stash  # type: ignore[attr-defined]
        deadline = time.monotonic() + timeout
        while msg_id not in stash:
            reply = server.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if reply is None:
                raise AssertionError("server closed stdout while waiting for %r" % (msg_id,))
            if isinstance(reply, dict) and "id" in reply and "method" not in reply:
                stash[reply["id"]] = reply
        return stash.pop(msg_id)

    def reply_checked(self, server: core.ServerProcess, msg_id: Any, name: str) -> Dict[str, Any]:
        reply = self.await_reply(server, msg_id)
        self.assertIn("result", reply, reply)
        result = reply["result"]
        self.last_result = result
        structured = self.check_envelope(result, name)
        common = core.import_server_modules()["common"]
        self.assertEqual(common.validate(self._tool_defs[name]["outputSchema"], structured), [], structured)
        if name in SESSION_TOOLS[2:] and name != "start_session":
            self.assertIn("session", structured)
        if name in SESSION_TOOLS and not structured["ok"]:
            if structured["outcome"] == "outcome_unknown":
                self.assertIn(name, MUTATING)
            self.assertIs(structured["retry_safe"], structured["outcome"] == "rejected" or name not in MUTATING)
        self.assert_no_paths(result)
        return structured

    def call_checked(self, server: core.ServerProcess, name: str, arguments: Any = MISSING) -> Dict[str, Any]:
        return self.reply_checked(server, self.send_call(server, name, arguments), name)

    def assert_no_paths(self, value: Any) -> None:
        text = json.dumps(value)
        for root in {str(self.tmp), os.path.realpath(str(self.tmp)), str(self.home)}:
            self.assertNotIn(root, text)

    def ref(self, host: str, session_id: str, **extra: Any) -> Dict[str, Any]:
        return dict({"host_id": host, "session_id": session_id}, **extra)

    def wait_until(self, check: Any, timeout: float = 15.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)

    def assert_stop_hooks(self, hooks: List[Dict[str, Any]], own: List[str]) -> None:
        """This task's hooks run once each, in their relative order, all ok: the
        first one first and agent_lease last. Hooks other modules register (for
        example runs and media) may run anywhere in between and are not checked."""
        names = [item["name"] for item in hooks]
        self.assertEqual(names[:1], own[:1], names)
        self.assertEqual(names[-1:], own[-1:], names)
        for name in own:
            self.assertEqual(names.count(name), 1, names)
        self.assertEqual([name for name in names if name in own], own, names)
        self.assertTrue(all(item["ok"] for item in hooks if item["name"] in own), hooks)

    def assert_no_takeover(self) -> None:
        for call in self.cli_calls():
            self.assertNotIn("--takeover", call["argv"])


# ====================================================================== catalog

class CatalogTests(SessionsCase):
    def test_tools_4_to_16_in_order_with_strict_schemas_and_annotations(self) -> None:
        server = self.started()
        tools = server.request("tools/list")["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(names[:16], core.CORE_TOOLS + SESSION_TOOLS)
        for tool in tools[3:16]:
            name = tool["name"]
            self.assertIs(tool["inputSchema"]["additionalProperties"], False, name)
            self.assertIs(tool["outputSchema"]["additionalProperties"], False, name)
            self.assertEqual(tool["_meta"], {"openai/widgetAccessible": True}, name)
            annotations = tool["annotations"]
            self.assertIs(annotations["openWorldHint"], False, name)
            self.assertIs(annotations["readOnlyHint"], name not in MUTATING, name)
            self.assertIs(annotations.get("destructiveHint", False), name == "stop_session", name)
            if name not in ("list_devices", "list_sessions", "start_session"):
                self.assertEqual(tool["inputSchema"]["required"][:2], ["host_id", "session_id"], name)
        tree = next(tool for tool in tools if tool["name"] == "get_ui_tree")
        self.assertEqual(tree["outputSchema"]["properties"]["nodes"]["items"], {"type": "object"})
        self.assertIn("compact node", tree["description"])
        tap = next(tool for tool in tools if tool["name"] == "tap_element")
        self.assertNotIn("x", tap["inputSchema"]["properties"])
        self.assertNotIn("y", tap["inputSchema"]["properties"])


# ====================================================================== discovery

class DiscoveryTests(SessionsCase):
    def test_list_devices_joins_live_sessions_and_keeps_only_declared_keys(self) -> None:
        FAKE.set_devices(self.fake_state, RECORDED_DEVICES)
        live = FAKE.add_session(self.fake_state)
        FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False)
        server = self.started()
        host = self.host_id(server)
        devices = self.call_ok(server, "list_devices")["devices"]
        for device in devices:
            self.assertEqual(set(device), DEVICE_KEYS)
        self.assertEqual([(item["platform"], item["target_id"], item["state"], item["session_id"])
                          for item in devices],
                         [("android", ANDROID, "device", live), ("android", "emulator-5590", "offline", None),
                          ("ios", IOS_UDID, "Booted", None),
                          ("ios", "11111111-2222-3333-4444-555555555555", "Shutdown", None)])
        self.assertEqual((devices[0]["name"], devices[0]["avd"], devices[0]["host_id"]),
                         ("sdk_gphone64_arm64", "Autonom_Split_API36", host))
        self.assertIsNone(devices[2]["avd"])
        self.assertEqual(self.call_ok(server, "list_devices", {"host_id": host})["devices"], devices)
        self.assertEqual(self.call_ok(server, "list_devices", {"host_id": OTHER_HOST})["devices"], [])

    def test_partial_collection_when_the_session_list_fails(self) -> None:
        FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="fail:sessions:backend_failed")
        result = self.call_ok(server, "list_devices")
        self.assertEqual([item["session_id"] for item in result["devices"]], [None, None])
        self.assertEqual([item["code"] for item in result["warnings"]], ["sessions_unavailable"])

    def test_device_and_host_failures(self) -> None:
        server = self.started(FAKE_MCP_CLI_MODE="crash:devices")
        failed = self.call_error(server, "list_devices", {}, "cli_output_invalid", outcome="failed")
        self.assertIs(failed["retry_safe"], True)
        refused = self.started(FAKE_MCP_CLI_MODE="fail:host_info:usage_error")
        error = self.call_error(refused, "list_devices", {}, "host_unavailable")
        self.assertIsNone(error["host_id"])
        self.call_error(refused, "list_sessions", {}, "host_unavailable")

    def test_list_sessions_live_and_stopped(self) -> None:
        live = FAKE.add_session(self.fake_state, label="checkout")
        stopped = FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False)
        server = self.started()
        host = self.host_id(server)
        sessions = self.call_ok(server, "list_sessions")["sessions"]
        self.assertEqual([(item["session_id"], item["live"], item["label"]) for item in sessions],
                         [(live, True, "checkout")])
        self.assertNotIn("current", sessions[0])
        both = self.call_ok(server, "list_sessions", {"include_stopped": True})["sessions"]
        self.assertEqual({item["session_id"] for item in both}, {live, stopped})
        self.assertEqual(self.call_ok(server, "list_sessions", {"host_id": OTHER_HOST})["sessions"], [])
        self.assertEqual(self.call_ok(server, "list_sessions", {"host_id": host})["sessions"], sessions)
        self.call_error(server, "list_sessions", {"include_stopped": "yes"}, "invalid_arguments")

    def test_truncated_session_list_keeps_the_live_session_and_the_newest_stopped(self) -> None:
        """The CLI's order is not chronological; a live session started before 300
        stopped ones (listed first here) survives the cut to 256 entries."""
        live = FAKE.add_session(self.fake_state, session_id="s_0a00000001")
        stopped = [FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False) for _ in range(300)]
        table = json.loads((self.fake_state / "sessions.json").read_text(encoding="utf-8"))
        table[live]["started_at"] = "2000-01-01T00:00:00.000Z"
        for index, session_id in enumerate(stopped):
            table[session_id]["started_at"] = "2026-01-01T00:%02d:%02d.000Z" % divmod(index, 60)
        (self.fake_state / "sessions.json").write_text(json.dumps(table), encoding="utf-8")
        server = self.started()
        result = self.call_ok(server, "list_sessions", {"include_stopped": True})
        listed = [item["session_id"] for item in result["sessions"]]
        self.assertEqual(len(listed), 256)
        self.assertEqual(listed[0], live)
        self.assertEqual(listed[1:], stopped[-255:], "the most recently started stopped sessions, in order")
        self.assertEqual([item["code"] for item in result["warnings"]], ["sessions_truncated"])
        self.assertIn("live", result["warnings"][0]["message"])
        self.assertEqual([item["session_id"] for item in self.call_ok(server, "list_sessions")["sessions"]],
                         [live])

    def test_get_session_entry_control_streams_and_active_run(self) -> None:
        live = FAKE.add_session(self.fake_state)
        stopped = FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False)
        server = self.started()
        host = self.host_id(server)
        first = self.call_ok(server, "get_session", self.ref(host, live))
        self.assertEqual(first["session"], {"host_id": host, "session_id": live})
        self.assertEqual((first["entry"]["session_id"], first["entry"]["live"]), (live, True))
        self.assertEqual(first["control"]["state"], "none")
        self.assertEqual((first["streams"], first["active_run_id"], first["busy"]), ([], None, False))
        self.call_ok(server, "acquire_control", self.ref(host, live))
        held = self.call_ok(server, "get_session", self.ref(host, live))["control"]
        self.assertEqual((held["state"], held["mine"], held["role"], held["generation"]),
                         ("held", True, "agent", 1))
        run = self.call_ok(server, "test_hold_run", self.ref(host, live, ms=20000))
        busy = self.call_ok(server, "get_session", self.ref(host, live))
        self.assertEqual((busy["active_run_id"], busy["busy"]), (run["run_id"], True))
        old = self.call_ok(server, "get_session", self.ref(host, stopped))
        self.assertIs(old["entry"]["live"], False)
        self.call_error(server, "get_session", self.ref(OTHER_HOST, live), "host_not_found")
        self.call_error(server, "get_session", self.ref(host, "s_nosuch"), "session_not_found")

    def test_get_session_survives_a_failing_control_status(self) -> None:
        live = FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="fail:control_status:backend_failed")
        host = self.host_id(server)
        result = self.call_ok(server, "get_session", self.ref(host, live))
        self.assertIsNone(result["control"])
        self.assertEqual([item["code"] for item in result["warnings"]], ["control_unavailable"])


# ====================================================================== lifecycle

class LifecycleTests(SessionsCase):
    def test_start_refuse_attach_with_one_cli_start(self) -> None:
        server = self.started()
        host = self.host_id(server)
        args = {"host_id": host, "platform": "android", "target_id": ANDROID}
        created = self.call_ok(server, "start_session", dict(args, label="checkout bug"))
        session_id = created["session"]["session_id"]
        self.assertIs(created["created"], True)
        self.assertEqual((created["entry"]["label"], created["entry"]["live"]), ("checkout bug", True))
        exists = self.call_error(server, "start_session", args, "session_exists")
        self.assertEqual(exists["session"], {"host_id": host, "session_id": session_id})
        self.assertEqual(exists["entry"]["session_id"], session_id)
        self.assertIn("attach_existing", exists["hint"])
        attached = self.call_ok(server, "start_session", dict(args, attach_existing=True))
        self.assertEqual((attached["session"]["session_id"], attached["created"]), (session_id, False))
        starts = self.cli_calls("session_start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["argv"], ["session", "start", "--operation-id", created["operation_id"],
                                             "--platform", "android", "--target", ANDROID, "--alongside",
                                             "--label", "checkout bug"])
        listed = self.call_ok(server, "list_sessions")["sessions"]
        self.assertEqual([item["session_id"] for item in listed], [session_id])

    def test_start_refusals_run_no_cli_start(self) -> None:
        FAKE.set_devices(self.fake_state, RECORDED_DEVICES)
        self.write_config({"allowed_targets": ["android:%s" % ANDROID, "android:emulator-5590",
                                               "ios:%s" % IOS_UDID]})
        server = self.started()
        host = self.host_id(server)
        before = len(self.cli_calls())
        args = {"host_id": host, "platform": "android", "target_id": ANDROID}
        refused = self.call_error(server, "start_session", dict(args, label="x" * 65), "invalid_arguments")
        self.assertEqual(refused["field"], "label")
        self.call_error(server, "start_session", dict(args, label=""), "invalid_arguments")
        bad = self.call_error(server, "start_session", dict(args, label="two\nlines"), "invalid_arguments")
        self.assertEqual(bad["field"], "label")
        self.assertEqual(len(self.cli_calls()), before, "argument refusals run no CLI at all")
        self.call_error(server, "start_session", dict(args, host_id=OTHER_HOST), "host_not_found")
        self.call_error(server, "start_session", dict(args, target_id="emulator-5590"), "target_not_found")
        self.call_error(server, "start_session", dict(args, platform="ios",
                                                      target_id="11111111-2222-3333-4444-555555555555"),
                        "target_not_allowed")
        self.call_error(server, "start_session", dict(args, target_id="emulator-5600"), "target_not_allowed")
        self.assertEqual(self.cli_calls("session_start"), [])
        ok = self.call_ok(server, "start_session", dict(args, label="x" * 64))
        self.assertEqual(ok["entry"]["label"], "x" * 64)

    def test_start_outcome_unknown_and_device_read_failure(self) -> None:
        server = self.started(FAKE_MCP_CLI_MODE="crash:session_start")
        host = self.host_id(server)
        args = {"host_id": host, "platform": "android", "target_id": ANDROID}
        unknown = self.call_error(server, "start_session", args, "cli_output_invalid", outcome="outcome_unknown")
        self.assertIs(unknown["retry_safe"], False)
        self.assertIn("list_sessions", unknown["hint"])
        self.assertEqual(len(self.cli_calls("session_start")), 1, "a start is never repeated")
        failing = self.started(FAKE_MCP_CLI_MODE="crash:devices")
        host = self.host_id(failing)
        self.call_error(failing, "start_session", dict(args, host_id=host), "cli_output_invalid")
        self.assertEqual(len(self.cli_calls("session_start")), 1)

    def test_stop_runs_hooks_in_order_releases_the_lease_then_stops_once(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        self.call_ok(server, "test_hooks", {"action": "register",
                                            "names": ["cancel_runs", "close_streams", "detach_canvas"]})
        self.call_ok(server, "tap_element", self.ref(host, session_id, selector={"text": "Settings"}))
        stopped = self.call_ok(server, "stop_session", self.ref(host, session_id))
        self.assertIs(stopped["stopped"], True)
        self.assert_stop_hooks(stopped["hooks"], TEST_STOP_HOOKS)
        self.assertEqual(stopped["hooks"][-1]["detail"], "released")
        calls = self.call_ok(server, "test_hooks", {"action": "calls"})["calls"]
        self.assertEqual(calls, [{"name": name, "session_id": session_id}
                                 for name in ("cancel_runs", "close_streams", "detach_canvas")])
        self.assertIsNone(FAKE.get_lease(self.fake_state, "android", ANDROID)["holder"])
        stops = self.cli_calls("session_stop")
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["argv"], ["session", "stop", "--operation-id", stopped["operation_id"],
                                            "--session-id", session_id])
        release = self.cli_calls("control_release")
        self.assertEqual(len(release), 1)
        self.assertLess(release[0]["start"], stops[0]["start"])
        again = self.call_error(server, "stop_session", self.ref(host, session_id), "session_stopped")
        self.assertIsNone(again["session"])  # a refused reference is not echoed as resolved
        self.assertEqual(len(self.cli_calls("session_stop")), 1)
        self.assertIs(self.call_ok(server, "get_session", self.ref(host, session_id))["entry"]["live"], False)

    def test_stop_hook_order_allows_other_modules_hooks_in_between(self) -> None:
        """Other modules (runs, media) register their own stopping hooks at setup;
        they may run between this task's hooks, which keep their relative order."""
        session_id = FAKE.add_session(self.fake_state)
        modules = core.import_server_modules()
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(), io.BytesIO(),
                                             io.StringIO())
        self.addCleanup(server.shutdown)
        host = server.services.refs.local_host()["host_id"]
        seen: List[Dict[str, str]] = []

        def other(name: str) -> Any:
            def hook(hook_session: str) -> str:
                seen.append({"name": name, "session_id": hook_session})
                return "done"
            return hook

        def call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
            return server.call_tool(server.tools[name], args, threading.Event())["structuredContent"]

        # The order a wired server has: core's test_run, then runs and media from
        # their setup, then the hooks the test registers, then one registered later.
        server.services.hooks.register("runs", other("runs"))
        server.services.hooks.register("media", other("media"))
        self.assertTrue(call("test_hooks", {"action": "register",
                                            "names": ["cancel_runs", "close_streams"]})["ok"])
        server.services.hooks.register("late_module", other("late_module"))
        self.assertTrue(call("test_hooks", {"action": "register", "names": ["detach_canvas"]})["ok"])
        self.assertTrue(call("tap_element", self.ref(host, session_id, selector={"text": "Settings"}))["ok"])
        stopped = call("stop_session", self.ref(host, session_id))
        self.assertTrue(stopped["ok"], stopped)
        names = [item["name"] for item in stopped["hooks"]]
        self.assertEqual(names, ["test_run", "runs", "media", "cancel_runs", "close_streams", "late_module",
                                 "detach_canvas", "agent_lease"])
        self.assert_stop_hooks(stopped["hooks"], TEST_STOP_HOOKS)
        self.assertEqual(stopped["hooks"][-1], {"name": "agent_lease", "ok": True, "detail": "released"})
        self.assertEqual(seen, [{"name": name, "session_id": session_id}
                                for name in ("runs", "media", "late_module")])
        self.assertEqual(len(self.cli_calls("session_stop")), 1)

    def test_stop_hook_order_check_rejects_a_wrong_order(self) -> None:
        def hooks(*names: str) -> List[Dict[str, Any]]:
            return [{"name": name, "ok": True} for name in names]

        own = TEST_STOP_HOOKS
        self.assert_stop_hooks(hooks(*own), own)
        self.assert_stop_hooks(hooks("test_run", "runs", "cancel_runs", "media", "close_streams",
                                     "detach_canvas", "agent_lease"), own)
        bad = [
            hooks("runs", "test_run", "cancel_runs", "close_streams", "detach_canvas", "agent_lease"),
            hooks("test_run", "cancel_runs", "close_streams", "detach_canvas", "agent_lease", "media"),
            hooks("test_run", "close_streams", "cancel_runs", "detach_canvas", "agent_lease"),
            hooks("test_run", "cancel_runs", "detach_canvas", "agent_lease"),
            hooks("test_run", "cancel_runs", "cancel_runs", "close_streams", "detach_canvas", "agent_lease"),
            [dict(item, ok=item["name"] != "close_streams") for item in hooks(*own)],
        ]
        for case in bad:
            with self.assertRaises(AssertionError, msg=[item["name"] for item in case]):
                self.assert_stop_hooks(case, own)

    def test_stop_cancels_a_held_run_through_the_hook(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        run = self.call_ok(server, "test_hold_run", self.ref(host, session_id, ms=60000))
        started = time.monotonic()
        stopped = self.call_ok(server, "stop_session", self.ref(host, session_id))
        self.assertLess(time.monotonic() - started, 14)
        self.assertEqual(stopped["hooks"][0], {"name": "test_run", "ok": True,
                                               "detail": "released %s" % run["run_id"]})
        self.assertEqual(stopped["hooks"][-1], {"name": "agent_lease", "ok": True, "detail": "not held"})
        self.assertEqual(len(self.cli_calls("session_stop")), 1)

    def test_stop_releases_a_lease_a_racing_tap_took_after_the_hooks(self) -> None:
        """A tap admitted between the busy check and the guard re-acquires the agent
        lease after the stop hook released it; the stop releases it again under the guard."""
        session_id = FAKE.add_session(self.fake_state)
        modules = core.import_server_modules()
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(), io.BytesIO(),
                                             io.StringIO())
        self.addCleanup(server.shutdown)
        host = server.services.refs.local_host()["host_id"]
        tap_args = self.ref(host, session_id, selector={"text": "Settings"})
        first = server.call_tool(server.tools["tap_element"], dict(tap_args), threading.Event())
        self.assertTrue(first["structuredContent"]["ok"], first)
        keeper = server.services.keeper
        real_release = keeper.release
        raced: List[Dict[str, Any]] = []

        def release_then_tap(ref: Any, **kwargs: Any) -> Any:
            view = real_release(ref, **kwargs)
            if not raced:  # the racing tap runs once, right after the hook's release
                raced.append(server.call_tool(server.tools["tap_element"], dict(tap_args),
                                              threading.Event())["structuredContent"])
            return view

        keeper.release = release_then_tap  # type: ignore[method-assign]
        stopped = server.call_tool(server.tools["stop_session"], self.ref(host, session_id),
                                   threading.Event())["structuredContent"]
        self.assertTrue(stopped["ok"], stopped)
        self.assertTrue(raced and raced[0]["ok"], raced)
        self.assertEqual(len(self.cli_calls("control_acquire")), 2, "the racing tap acquired the lease again")
        self.assertEqual(stopped["hooks"][-1], {"name": "agent_lease", "ok": True, "detail": "released"})
        releases = self.cli_calls("control_release")
        self.assertEqual(len(releases), 2)
        self.assertLess(releases[-1]["start"], self.cli_calls("session_stop")[0]["start"])
        self.assertIsNone(FAKE.get_lease(self.fake_state, "android", ANDROID)["holder"])
        self.assertIsNone(keeper.cached("android", ANDROID))

    def test_stop_is_refused_while_a_semantic_mutation_is_in_flight(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        self.call_ok(server, "test_hooks", {"action": "register", "names": ["cancel_runs"]})
        slow = self.send_call(server, "test_slow_mutation", self.ref(host, session_id, ms=3000))
        self.wait_until(lambda: self.call_ok(server, "get_session", self.ref(host, session_id))["busy"],
                        what="the slow mutation to hold the guard")
        busy = self.call_error(server, "stop_session", self.ref(host, session_id), "session_busy")
        self.assertEqual(busy["busy_with"], {"tool": "test_slow_mutation"})
        self.assertEqual(self.call_ok(server, "test_hooks", {"action": "calls"})["calls"], [])
        self.assertEqual(self.cli_calls("session_stop"), [])
        self.assertTrue(self.reply_checked(server, slow, "test_slow_mutation")["ok"])
        self.call_ok(server, "stop_session", self.ref(host, session_id))


# ====================================================================== reads

class ReadTests(SessionsCase):
    def shot_files(self, session_id: str) -> List[Path]:
        return sorted((self.fake_state / "sessions" / session_id / "screenshots").iterdir())

    def test_get_screen_returns_an_artifact_id_and_the_inline_png(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        result = self.call_ok(server, "get_screen", self.ref(host, session_id, label="home"))
        screen = result["screen"]
        files = self.shot_files(session_id)
        self.assertEqual(len(files), 1)
        data = files[0].read_bytes()
        self.assertEqual((screen["width"], screen["height"], screen["bytes"], screen["mime"], screen["inline"]),
                         (4, 8, len(data), "image/png", True))
        refs_mod = core.import_server_modules()["refs"]
        key = (self.mcp_dir / "artifact-key").read_bytes()
        expected = refs_mod.artifact_id(key, host, session_id, "screenshots/%s" % files[0].name)
        self.assertEqual(screen["artifact_id"], expected)
        content = self.last_result["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual((content[1]["type"], content[1]["mimeType"]), ("image", "image/png"))
        self.assertEqual(base64.b64decode(content[1]["data"]), data)
        self.assertNotIn("_meta", self.last_result)
        shots = self.cli_calls("screenshot")
        self.assertEqual(shots[0]["argv"], ["screenshot", "--session-id", session_id, "--no-agent", "--label", "home"])
        again = self.call_ok(server, "get_screen", self.ref(host, session_id))["screen"]
        self.assertNotEqual(again["artifact_id"], screen["artifact_id"])

    def test_inline_image_cap(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.write_config({"inline_image_max_bytes": 10})
        server = self.started(FAKE_MCP_SHOT_SIZE="16x16")
        host = self.host_id(server)
        screen = self.call_ok(server, "get_screen", self.ref(host, session_id))["screen"]
        self.assertIs(screen["inline"], False)
        self.assertGreater(screen["bytes"], 10)
        self.assertEqual((screen["width"], screen["height"]), (16, 16))
        self.assertEqual(len(self.last_result["content"]), 1)

    def test_get_screen_refusals(self) -> None:
        live = FAKE.add_session(self.fake_state)
        stopped = FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False)
        server = self.started()
        host = self.host_id(server)
        self.call_error(server, "get_screen", self.ref(OTHER_HOST, live), "host_not_found")
        self.call_error(server, "get_screen", self.ref(host, stopped), "session_stopped")
        self.call_error(server, "get_screen", self.ref(host, live, label="y" * 65), "invalid_arguments")
        self.assertEqual(self.cli_calls("screenshot"), [])
        failing = self.started(FAKE_MCP_CLI_MODE="crash:screenshot")
        self.call_error(failing, "get_screen", self.ref(self.host_id(failing), live), "cli_output_invalid",
                        outcome="failed")

    def test_get_ui_tree_reads_the_nodes_list_without_recovery(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        tree = self.call_ok(server, "get_ui_tree", self.ref(host, session_id))
        self.assertEqual(tree["nodes"], FAKE.DEFAULT_NODES)
        self.assertEqual((tree["node_count"], tree["truncated"], tree["screen"]),
                         (3, False, {"width": 1080, "height": 2400}))
        self.assertEqual(self.cli_calls("ui_tree")[0]["argv"],
                         ["ui", "tree", "--session-id", session_id, "--format", "json", "--max-nodes", "200",
                          "--no-accessibility-recovery"])
        cut = self.call_ok(server, "get_ui_tree", self.ref(host, session_id, max_nodes=1))
        self.assertEqual((cut["node_count"], cut["truncated"]), (1, True))
        clickable = self.call_ok(server, "get_ui_tree", self.ref(host, session_id, max_nodes=500,
                                                                  interactable=True))
        self.assertEqual([node["ref"] for node in clickable["nodes"]], ["n1", "n2"])
        self.assertEqual(self.cli_calls("ui_tree")[2]["argv"][-2:], ["--no-accessibility-recovery",
                                                                     "--interactable"])
        for bad in (0, 501):
            self.call_error(server, "get_ui_tree", self.ref(host, session_id, max_nodes=bad), "invalid_arguments")
        self.assertEqual(len(self.cli_calls("ui_tree")), 3)
        for call in self.cli_calls("ui_tree"):
            self.assertIn("--no-accessibility-recovery", call["argv"])

    def test_get_ui_tree_failure_is_failed_and_retry_safe(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="crash:ui_tree")
        failed = self.call_error(server, "get_ui_tree", self.ref(self.host_id(server), session_id),
                                 "cli_output_invalid", outcome="failed")
        self.assertIs(failed["retry_safe"], True)


# ====================================================================== mutations

class MutationTests(SessionsCase):
    def test_tap_auto_acquires_once_then_reuses_and_is_reconciled(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        first = self.call_ok(server, "tap_element", self.ref(host, session_id, selector={"text": "Settings"}))
        second = self.call_ok(server, "tap_element", self.ref(host, session_id,
                                                               selector={"desc": "-dash", "role": "button"},
                                                               mode="exact", index=0))
        self.assertEqual(first["action"], {"verb": "ui tap", "operation_id": first["operation_id"]})
        self.assertEqual(len(self.cli_calls("control_acquire")), 1)
        taps = self.cli_calls("ui_tap")
        self.assertEqual(taps[0]["argv"], ["ui", "tap", "--operation-id", first["operation_id"],
                                           "--session-id", session_id, "--no-observe", "--text", "Settings",
                                           "--mode", "contains"])
        self.assertEqual(taps[1]["argv"], ["ui", "tap", "--operation-id", second["operation_id"],
                                           "--session-id", session_id, "--no-observe", "--desc=-dash", "--role", "button",
                                           "--mode", "exact", "--index", "0"])
        for tap in taps:
            self.assertEqual(tap["claim"], {"AUTONOM_CONTROL_HOLDER": "chatgpt-agent:" + host,
                                            "AUTONOM_CONTROL_GENERATION": "1"})
        found = self.call_ok(server, "get_operation", self.ref(host, session_id,
                                                                operation_id=first["operation_id"]))
        self.assertIs(found["found"], True)
        self.assertEqual([(item["verb"], item["ok"], item["error_code"]) for item in found["entries"]],
                         [("ui tap", True, None)])
        self.assertIsInstance(found["entries"][0]["at"], str)
        journal = self.cli_calls("journal_find")[0]["argv"]
        self.assertEqual(journal, ["journal", "--session-id", session_id, "--grep", first["operation_id"],
                                   "--max", "5"])
        self.assert_no_takeover()

    def test_agent_profile_session_still_reads_nodes_and_acts_without_observing(self) -> None:
        """A session started with --profile agent defaults to the outline tree and
        to observing after each action (DEC-028); the adapter pins --format json and
        --no-observe, so get_ui_tree still returns nodes and every action completes
        without the observe wait (the fake would sleep a minute if it observed)."""
        session_id = FAKE.add_session(self.fake_state, profile="agent")
        server = self.started(FAKE_MCP_OBSERVE_MS="60000")
        host = self.host_id(server)
        tree = self.call_ok(server, "get_ui_tree", self.ref(host, session_id))
        self.assertEqual(tree["nodes"], FAKE.DEFAULT_NODES)
        self.assertNotIn("outline", tree)
        self.assertEqual(self.cli_calls("ui_tree")[0]["argv"][4:6], ["--format", "json"])
        tapped = self.call_ok(server, "tap_element", self.ref(host, session_id, selector={"text": "Settings"}))
        self.assertEqual(tapped["action"], {"verb": "ui tap", "operation_id": tapped["operation_id"]})
        typed = self.call_ok(server, "type_text", self.ref(host, session_id, text="hello"))
        self.assertEqual(typed["chars"], 5)
        self.call_ok(server, "press_key", self.ref(host, session_id, key="BACK"))
        for verb in ("ui_tap", "ui_type", "ui_key"):
            calls = self.cli_calls(verb)
            self.assertEqual(len(calls), 1, verb)
            self.assertEqual(calls[0]["argv"][6], "--no-observe", verb)
        for result in (tapped, typed):
            self.assertNotIn("changes", result)
        self.assertEqual(len(self.cli_calls("control_acquire")), 1)
        found = self.call_ok(server, "get_operation", self.ref(host, session_id,
                                                                operation_id=tapped["operation_id"]))
        self.assertEqual([(item["verb"], item["ok"]) for item in found["entries"]], [("ui tap", True)])
        self.assert_no_takeover()

    def test_invalid_arguments_run_no_cli(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        before = len(self.cli_calls())
        extra = self.call_error(server, "tap_element", self.ref(host, session_id, selector={"text": "a"}, x=1),
                                "invalid_arguments")
        self.assertEqual(extra["field"], "x")
        self.call_error(server, "tap_element", self.ref(host, session_id, selector={}), "invalid_arguments")
        self.call_error(server, "tap_element", self.ref(host, session_id, selector={"text": ""}),
                        "invalid_arguments")
        self.call_error(server, "tap_element", self.ref(host, session_id, selector={"text": "a"}, index=501),
                        "invalid_arguments")
        self.call_error(server, "type_text", self.ref(host, session_id, text="bell\x07"), "invalid_arguments")
        self.call_error(server, "type_text", self.ref(host, session_id, text="z" * 1001), "invalid_arguments")
        self.call_error(server, "press_key", self.ref(host, session_id, key="back"), "invalid_arguments")
        self.call_error(server, "get_operation", self.ref(host, session_id, operation_id="op_x"),
                        "invalid_arguments")
        self.assertEqual(len(self.cli_calls()), before)

    def test_malformed_regex_selector_is_refused_before_any_cli_call(self) -> None:
        """The CLI refuses a bad pattern as invalid_value (an uncertain outcome to
        the adapter), so the adapter refuses it first; a valid one still taps."""
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        before = len(self.cli_calls())
        for field, pattern in (("text", "("), ("desc", "[a-"), ("resource_id", "*x"), ("role", "a{2,1}")):
            refused = self.call_error(server, "tap_element",
                                      self.ref(host, session_id, selector={field: pattern}, mode="regex"),
                                      "invalid_arguments")
            self.assertEqual(refused["field"], "selector.%s" % field, pattern)
            self.assertTrue(refused["retry_safe"], refused)
        # The pattern is checked only in regex mode; a later bad field is found too.
        refused = self.call_error(server, "tap_element",
                                  self.ref(host, session_id, selector={"text": "ok", "class_name": "("},
                                           mode="regex"), "invalid_arguments")
        self.assertEqual(refused["field"], "selector.class_name")
        self.assertEqual(len(self.cli_calls()), before)
        tapped = self.call_ok(server, "tap_element",
                              self.ref(host, session_id, selector={"text": "("}, mode="contains"))
        self.assertEqual(tapped["action"]["verb"], "ui tap")
        self.call_ok(server, "tap_element", self.ref(host, session_id, selector={"text": r"^Sign \(in\)$"},
                                                     mode="regex"))
        taps = self.cli_calls("ui_tap")
        self.assertEqual(len(taps), 2)
        self.assertIn("regex", taps[1]["argv"])

    def test_c0_and_c1_control_characters_are_refused_tab_and_newline_pass(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        for text in ("a\u009bb", "a\u0085b", "a\u0080", "\u009f", "a\x7fb", "a\rb", "a\x1bb"):
            refused = self.call_error(server, "type_text", self.ref(host, session_id, text=text),
                                      "invalid_arguments")
            self.assertEqual(refused["field"], "text", repr(text))
        self.assertEqual(self.cli_calls("ui_type"), [])
        self.assertEqual(self.cli_calls("control_acquire"), [])
        typed = self.call_ok(server, "type_text", self.ref(host, session_id, text="tab\tand\nnewline \u00e9"))
        self.assertEqual(typed["chars"], 17)
        self.assertEqual(len(self.cli_calls("ui_type")), 1)

    def test_lone_surrogates_are_refused_before_any_cli_call(self) -> None:
        """A lone surrogate would reach the CLI argv as a raw byte (\\udc9b is 0x9b,
        an 8-bit CSI), bypassing the C1 refusal."""
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        for text in ("a\udc9bb", "\udc80", "\ud800", "x\udfff", "\ud83d"):
            refused = self.call_error(server, "type_text", self.ref(host, session_id, text=text),
                                      "invalid_arguments")
            self.assertEqual(refused["field"], "text", repr(text))
        self.assertEqual(self.cli_calls("ui_type"), [])
        self.assertEqual(self.cli_calls("control_acquire"), [])
        typed = self.call_ok(server, "type_text", self.ref(host, session_id, text="pair \U0001f600"))
        self.assertEqual(typed["chars"], 6)
        self.assertEqual(len(self.cli_calls("ui_type")), 1)

    def test_selector_and_label_nul_and_lone_surrogates_are_refused_before_any_cli_call(self) -> None:
        """Every selector field, in every mode, and the session label pass the same
        argv check as the typed text: NUL or a lone surrogate (JSON "\\ud800",
        "a\\udc9b") is invalid_arguments on that field, rejected and retry-safe,
        before the guard, the agent lease or any CLI call (not even control acquire)."""
        FAKE.set_devices(self.fake_state, RECORDED_DEVICES)
        self.write_config({"allowed_targets": ["android:%s" % ANDROID, "ios:%s" % IOS_UDID]})
        session_id = FAKE.add_session(self.fake_state, platform="ios", target_id=IOS_UDID)
        server = self.started()
        host = self.host_id(server)
        before = len(self.cli_calls())
        bad_values = ("\ud800", "a\udc9b", "\udfffz", "x\ud83dy", "nul\x00")
        for field in ("text", "desc", "resource_id", "class_name", "role"):
            for mode in ("contains", "exact", "regex"):
                for value in bad_values:
                    refused = self.call_error(server, "tap_element",
                                              self.ref(host, session_id, selector={field: value}, mode=mode),
                                              "invalid_arguments")
                    self.assertEqual(refused["field"], "selector.%s" % field, (field, mode, repr(value)))
                    self.assertIs(refused["retry_safe"], True, refused)
        # A later field is checked too, after a clean one.
        refused = self.call_error(server, "tap_element",
                                  self.ref(host, session_id, selector={"text": "ok", "role": "b\udc80"}),
                                  "invalid_arguments")
        self.assertEqual(refused["field"], "selector.role")
        args = {"host_id": host, "platform": "android", "target_id": ANDROID}
        for value in bad_values:
            refused = self.call_error(server, "start_session", dict(args, label=value), "invalid_arguments")
            self.assertEqual(refused["field"], "label", repr(value))
            self.assertIs(refused["retry_safe"], True, refused)
        self.assertEqual(len(self.cli_calls()), before, "argv refusals run no CLI at all")
        self.assertEqual(self.cli_calls("control_acquire"), [])
        # A surrogate pair (an astral character) is one real character and passes.
        tapped = self.call_ok(server, "tap_element",
                              self.ref(host, session_id, selector={"text": "smile \U0001f600"}))
        self.assertEqual(tapped["action"]["verb"], "ui tap")
        taps = self.cli_calls("ui_tap")
        self.assertEqual(len(taps), 1)
        self.assertIn("smile \U0001f600", taps[0]["argv"])
        started = self.call_ok(server, "start_session", dict(args, label="pair \U0001f600"))
        self.assertEqual(started["entry"]["label"], "pair \U0001f600")

    def test_type_log_withholding_fails_closed_when_redaction_rewrites_the_line(self) -> None:
        """An env value that is a piece of ' stderr: ' (or of the line head) is
        redacted out of the core log line; the CLI stderr that echoes the typed
        text must still never reach the server log."""
        session_id = FAKE.add_session(self.fake_state)
        for env in ({"LOG_SINK": "stderr"}, {"AUTONOM_TEST_TAG": "derr: "},
                    {"AUTONOM_TEST_TAG": "backend_failed"}, {"AUTONOM_TEST_TAG": "status cli"}):
            server = self.started(FAKE_MCP_CLI_MODE="fail:ui_type:backend_failed",
                                  FAKE_MCP_FAIL_ERROR='typed "hunterzz" x', **env)
            host = self.host_id(server)
            failed = self.call_error(server, "type_text", self.ref(host, session_id, text="hunterzz",
                                                                    sensitive=True),
                                     "backend_failed", outcome="outcome_unknown")
            self.assertNotIn("hunterzz", json.dumps(self.last_result))
            self.assertEqual(failed["cli_error_code"], "backend_failed")
            self.assertEqual(server.close(), 0)
            log = server.stderr_text()
            self.assertNotIn("hunterzz", log, env)
            self.assertIn("cli ui_type failed; log line withheld: the call carried typed text", log, env)
            self.assertNotIn("error_code", log, env)

    def test_type_log_keeps_only_the_fixed_head_when_the_line_is_intact(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="fail:ui_type:backend_failed",
                              FAKE_MCP_FAIL_ERROR='typed "hunterzz" x')
        host = self.host_id(server)
        self.call_error(server, "type_text", self.ref(host, session_id, text="hunterzz"),
                        "backend_failed", outcome="outcome_unknown")
        self.assertEqual(server.close(), 0)
        log = server.stderr_text()
        self.assertNotIn("hunterzz", log)
        self.assertRegex(log, r"cli ui_type exit 2 status cli_error [0-9.]+s code backend_failed "
                              r"stderr: <withheld: the call carried typed text>")

    def test_reference_refusals_before_any_mutation(self) -> None:
        live = FAKE.add_session(self.fake_state)
        stopped = FAKE.add_session(self.fake_state, "ios", IOS_UDID, live=False)
        self.write_config({"allowed_targets": ["ios:%s" % IOS_UDID]})
        server = self.started()
        host = self.host_id(server)
        tap = {"selector": {"text": "Settings"}}
        self.call_error(server, "tap_element", self.ref(OTHER_HOST, live, **tap), "host_not_found")
        self.call_error(server, "type_text", self.ref(host, "s_nosuch", text="a"), "session_not_found")
        self.call_error(server, "press_key", self.ref(host, stopped, key="BACK"), "session_stopped")
        self.call_error(server, "tap_element", self.ref(host, live, **tap), "target_not_allowed")
        for key in ("ui_tap", "ui_type", "ui_key", "control_acquire"):
            self.assertEqual(self.cli_calls(key), [], key)

    def test_pointer_independence(self) -> None:
        FAKE.add_session(self.fake_state)
        second = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        pointers = [self.state_base / "current.json", self.state_base / "sessions" / "current.json"]
        for path in pointers:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"session_id": "s_first"}\n', encoding="utf-8")
        server = self.started()
        host = self.host_id(server)
        self.call_ok(server, "tap_element", self.ref(host, second, selector={"text": "Settings"}))
        self.assertEqual(self.cli_calls("ui_tap")[0]["argv"][4:6], ["--session-id", second])
        for path in pointers:
            self.assertEqual(path.read_bytes(), b'{"session_id": "s_first"}\n')

    def test_sensitive_text_is_never_echoed_and_passed_as_sensitive(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        typed = self.call_ok(server, "type_text", self.ref(host, session_id, text=SECRET, sensitive=True))
        self.assertEqual((typed["chars"], typed["action"]["verb"]), (14, "ui type"))
        argv = self.cli_calls("ui_type")[0]["argv"]
        self.assertEqual(argv, ["ui", "type", "--operation-id", typed["operation_id"], "--session-id",
                                session_id, "--no-observe", "--sensitive", "--", "<redacted>"])
        self.assertNotIn(SECRET, json.dumps(self.last_result))
        refused = self.started(FAKE_MCP_CLI_MODE="crash:ui_type")
        host = self.host_id(refused)
        self.call_error(refused, "type_text", self.ref(host, session_id, text=SECRET, sensitive=True),
                        "cli_output_invalid", outcome="outcome_unknown")
        self.assertNotIn(SECRET, json.dumps(self.last_result))
        plain = self.call_ok(server, "type_text", self.ref(host, session_id, text="--operation-id op_x -n"))
        self.assertEqual(self.cli_calls("ui_type")[-1]["argv"][-2:], ["--", "--operation-id op_x -n"])
        self.assertEqual(plain["chars"], 22)
        for proc in (server, refused):
            self.assertEqual(proc.close(), 0)
            self.assertNotIn(SECRET, proc.stderr_text())
        self.assertNotIn(SECRET, self.cli_log.read_text(encoding="utf-8"))
        for path in sorted(self.mcp_dir.rglob("*")) if self.mcp_dir.exists() else []:
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path.name)

    def test_type_failure_hides_encoded_and_short_text_from_results_and_log(self) -> None:
        """The CLI's own error and hint echo the text the way Android `input text`
        and the device shell carry it (spaces as %s, single-quoted) and a 3-character
        text; neither reaches the result or the server log."""
        session_id = FAKE.add_session(self.fake_state)
        # The quotes inside are JSON-escaped on the CLI's stderr, so the log line
        # does not hold these env values verbatim (the runner redacts env values).
        echo = ("backend timed out: adb -s emulator-5584 shell input text 'correct%shorse%sbattery' "
                "and 'it'\\''s' then '737' \"x\"")
        server = self.started(FAKE_MCP_CLI_MODE="fail:ui_type:backend_failed", FAKE_MCP_FAIL_ERROR=echo,
                              FAKE_MCP_FAIL_HINT="type 'correct%shorse%sbattery' or '737' again \"y\"")
        host = self.host_id(server)
        leaks = ("correct%shorse%sbattery", "correct horse battery", "'737'", "737 ", "it'\\''s", "it's")
        for text in ("correct horse battery", "737", "it's"):
            for sensitive in (True, False):
                failed = self.call_error(server, "type_text", self.ref(host, session_id, text=text,
                                                                        sensitive=sensitive),
                                         "backend_failed", outcome="outcome_unknown")
                self.assertEqual((failed["cli_error_code"], failed["retry_safe"]), ("backend_failed", False))
                self.assertIn("get_operation", failed["hint"])
                self.assertNotIn("cli_hint", failed)
                shown = json.dumps(self.last_result)
                for leak in leaks:
                    self.assertNotIn(leak, shown, (text, leak))
                for key in ("error", "hint", "cli_error"):
                    self.assertNotIn(text, failed[key], (text, key))
        self.assertEqual(len(self.cli_calls("ui_type")), 6, "one invocation per call, never repeated")
        self.assertEqual(server.close(), 0)
        log = server.stderr_text()
        self.assertIn("cli ui_type exit 2 status cli_error", log)
        self.assertIn("code backend_failed", log)
        for leak in leaks + ("correct",):
            self.assertNotIn(leak, log, leak)

    def test_type_refusals_keep_exact_codes_and_the_control_view(self) -> None:
        """A typed word inside a CLI code or a holder field must not change the
        refusal class, the code, the dropped lease or the control view."""
        session_id = FAKE.add_session(self.fake_state)
        modules = core.import_server_modules()
        log = io.StringIO()
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(), io.BytesIO(), log)
        self.addCleanup(server.shutdown)
        host = server.services.refs.local_host()["host_id"]
        tool = server.tools["type_text"]
        schema = server.schemas["type_text"]
        env = server.services.runner.env

        def typed(text: str) -> Dict[str, Any]:
            result = server.call_tool(tool, self.ref(host, session_id, text=text, sensitive=True),
                                      threading.Event())["structuredContent"]
            self.assertEqual(modules["common"].validate(schema, result), [], result)
            self.assertEqual((result["ok"], result["outcome"], result["retry_safe"]),
                             (False, "rejected", True), (text, result))
            self.assertEqual(result["error_code"], result["cli_error_code"], result)
            self.assertNotIn("<redacted>", json.dumps(result), text)
            return result

        canvas = {"kind": "human", "id": "canvas:1:2", "label": "Canvas 1"}
        env["FAKE_MCP_CLI_MODE"] = "fail:ui_type:control_held"
        for count, text in enumerate(("control", "held", "human", "canvas", "737"), start=1):
            refused = typed(text)
            self.assertEqual(refused["error_code"], "control_held", text)
            control = refused["control"]
            self.assertEqual((control["state"], control["role"], control["mine"], control["generation"],
                              control["holder"]), ("held", "human", False, 7, canvas), text)
            self.assertEqual(refused["capability"], "control.lease")
            self.assertEqual(len(self.cli_calls("control_acquire")), count, "the cached lease was dropped")
        for text, code in (("action", "forbidden_action"), ("allowed", "app_not_allowed"),
                           ("forbidden", "forbidden_action"), ("app", "app_not_allowed")):
            env["FAKE_MCP_CLI_MODE"] = "fail:ui_type:%s" % code
            refused = typed(text)
            self.assertEqual(refused["error_code"], code, text)
            self.assertNotIn("control", refused)
        # The agent's own lease after a newer generation: stale, naming this agent.
        env["FAKE_MCP_CLI_MODE"] = ""
        agent = {"kind": "agent", "id": "chatgpt-agent:" + host, "label": "ChatGPT agent"}
        FAKE.set_lease(self.fake_state, "android", ANDROID, holder=agent, generation=5)
        for text in ("agent", "chatgpt"):
            refused = typed(text)
            self.assertEqual(refused["error_code"], "control_generation_stale", text)
            control = refused["control"]
            self.assertEqual((control["state"], control["role"], control["mine"], control["generation"],
                              control["holder"]), ("held", "agent", True, 5, agent), text)
            self.assertIsNone(server.services.keeper.cached("android", ANDROID))
            server.services.keeper._cache[("android", ANDROID)] = {  # noqa: SLF001 - a stale cached claim
                "generation": 1, "expires_at": time.time() + 600, "session_id": session_id}
        self.assertEqual(len(self.cli_calls("ui_type")), 11, "each refusal ran its one invocation")
        self.assertNotIn("<redacted>", log.getvalue())
        self.assert_no_takeover()

    def test_press_key_and_rejected_cli_codes(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        pressed = self.call_ok(server, "press_key", self.ref(host, session_id, key="BACK"))
        self.assertEqual(self.cli_calls("ui_key")[0]["argv"],
                         ["ui", "key", "--operation-id", pressed["operation_id"], "--session-id", session_id,
                          "--no-observe", "BACK"])
        refusing = self.started(FAKE_MCP_CLI_MODE="fail:ui_key:unsupported_key_for_platform,"
                                                  "fail:ui_tap:ui_action_uncertain")
        host = self.host_id(refusing)
        refused = self.call_error(refusing, "press_key", self.ref(host, session_id, key="VOLUME_UP"),
                                  "unsupported_key_for_platform")
        self.assertEqual(refused["cli_error_code"], "unsupported_key_for_platform")
        uncertain = self.call_error(refusing, "tap_element", self.ref(host, session_id, selector={"text": "a"}),
                                    "ui_action_uncertain", outcome="outcome_unknown")
        self.assertIn("get_operation", uncertain["hint"])
        self.assertEqual(len(self.cli_calls("ui_key")), 2)
        self.assertEqual(len(self.cli_calls("ui_tap")), 1)

    def test_crashes_are_unknown_with_one_invocation_each(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="crash:ui_tap,crash:ui_type,malformed:ui_key")
        host = self.host_id(server)
        for name, extra, key in (("tap_element", {"selector": {"text": "a"}}, "ui_tap"),
                                 ("type_text", {"text": "abc"}, "ui_type"),
                                 ("press_key", {"key": "HOME"}, "ui_key")):
            unknown = self.call_error(server, name, self.ref(host, session_id, **extra), "cli_output_invalid",
                                      outcome="outcome_unknown")
            self.assertIs(unknown["retry_safe"], False)
            self.assertEqual(len(self.cli_calls(key)), 1, key)

    def test_timeout_is_unknown_never_repeated_and_reconciled(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.write_config({"cli_timeout_scale": 0.2})
        server = self.started(FAKE_MCP_CLI_MODE="timeout:ui_tap")
        host = self.host_id(server)
        started = time.monotonic()
        unknown = self.call_error(server, "tap_element", self.ref(host, session_id, selector={"text": "a"}),
                                  "cli_timeout", outcome="outcome_unknown")
        self.assertLess(time.monotonic() - started, 20)
        self.assertIs(unknown["retry_safe"], False)
        self.assertIn("get_operation", unknown["hint"])
        taps = self.cli_calls("ui_tap")
        self.assertEqual(len(taps), 1)
        self.assertTrue(core.wait_dead(taps[0]["pid"], 5))
        self.assertTrue(core.wait_dead(taps[0]["child_pid"], 5))
        operation_id = unknown["operation_id"]
        args = self.ref(host, session_id, operation_id=operation_id)
        missing = self.call_ok(server, "get_operation", args)
        self.assertEqual((missing["found"], missing["entries"]), (False, []))
        self.assertIn("screen", missing["hint"])
        # The CLI call reached the journal (as it does when the answer, not the call, was lost).
        FAKE.journal(self.fake_state, session_id, "ui tap", True, None, operation_id)
        found = self.call_ok(server, "get_operation", args)
        self.assertIs(found["found"], True)
        self.assertEqual(found["entries"][0]["verb"], "ui tap")
        self.assertNotIn("hint", found)
        self.assertEqual(len(self.cli_calls("ui_tap")), 1)

    def test_a_lost_response_is_found_in_the_journal(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="slow:ui_tap:1500")
        host = self.host_id(server)
        lost = self.send_call(server, "tap_element", self.ref(host, session_id, selector={"text": "Settings"}))
        self.wait_until(lambda: self.cli_calls("ui_tap"), what="the tap to start")
        server.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": lost}})
        self.wait_until(lambda: self.cli_calls("ui_tap")[0]["end"] is not None, what="the tap to finish")
        operation_id = self.cli_calls("ui_tap")[0]["operation_id"]
        found = self.call_ok(server, "get_operation", self.ref(host, session_id, operation_id=operation_id))
        self.assertEqual([(item["verb"], item["ok"]) for item in found["entries"]], [("ui tap", True)])
        self.assertNotIn(lost, server.stash)  # type: ignore[attr-defined]
        self.assertEqual(len(self.cli_calls("ui_tap")), 1)

    def test_session_busy_within_one_session_only(self) -> None:
        first = FAKE.add_session(self.fake_state)
        second = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        server = self.started(FAKE_MCP_CLI_MODE="slow:ui_tap:2500")
        host = self.host_id(server)
        slow = self.send_call(server, "tap_element", self.ref(host, first, selector={"text": "Settings"}))
        self.wait_until(lambda: self.cli_calls("ui_tap"), what="the slow tap to start")
        busy = self.call_error(server, "press_key", self.ref(host, first, key="BACK"), "session_busy")
        self.assertEqual(busy["busy_with"], {"tool": "tap_element"})
        self.call_error(server, "type_text", self.ref(host, first, text="a"), "session_busy")
        other = self.call_ok(server, "press_key", self.ref(host, second, key="BACK"))
        self.assertEqual(other["session"], {"host_id": host, "session_id": second})
        self.assertTrue(self.reply_checked(server, slow, "tap_element")["ok"])
        self.assertEqual(len(self.cli_calls("ui_tap")), 1)
        self.assertEqual([call["argv"][-1] for call in self.cli_calls("ui_key")], ["BACK"])
        self.assertEqual(self.cli_calls("ui_type"), [])
        self.call_ok(server, "press_key", self.ref(host, first, key="BACK"))


# ====================================================================== control

class ControlTests(SessionsCase):
    def test_human_takeover_rejects_the_next_tap_once(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        tap = self.ref(host, session_id, selector={"text": "Settings"})
        self.call_ok(server, "tap_element", tap)
        human = {"kind": "human", "id": "canvas:4000:123", "label": "Canvas 4000"}
        FAKE.set_lease(self.fake_state, "android", ANDROID, holder=human, generation=2)
        refused = self.call_error(server, "tap_element", tap, "control_held")
        self.assertEqual((refused["control"]["role"], refused["control"]["state"]), ("human", "held"))
        self.assertEqual(len(self.cli_calls("ui_tap")), 2, "the refused tap ran once and was not repeated")
        # The cached lease is gone: the next tap tries to acquire, is refused, and taps nothing.
        again = self.call_error(server, "tap_element", tap, "control_held")
        self.assertEqual(again["control"]["role"], "human")
        self.assertEqual(len(self.cli_calls("ui_tap")), 2)
        self.assertEqual(len(self.cli_calls("control_acquire")), 2)
        self.assert_no_takeover()

    def test_takeover_answered_stale_rejects_the_next_tap_once(self) -> None:
        """A person's takeover raises the lease generation; the merged CLI answers the
        agent's older claim with control_generation_stale naming that person
        (control.py admission row 7). The fake answers that row through its fail
        mode, switched on in process right after the takeover."""
        session_id = FAKE.add_session(self.fake_state)
        modules = core.import_server_modules()
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(), io.BytesIO(),
                                             io.StringIO())
        self.addCleanup(server.shutdown)
        host = server.services.refs.local_host()["host_id"]
        tool = server.tools["tap_element"]
        schema = server.schemas["tap_element"]
        args = self.ref(host, session_id, selector={"text": "Settings"})
        first = server.call_tool(tool, dict(args), threading.Event())["structuredContent"]
        self.assertTrue(first["ok"], first)
        human = {"kind": "human", "id": "canvas:1:2", "label": "Canvas 1"}
        FAKE.set_lease(self.fake_state, "android", ANDROID, holder=human, generation=7)
        server.services.runner.env["FAKE_MCP_CLI_MODE"] = "fail:ui_tap:control_generation_stale"
        refused = server.call_tool(tool, dict(args), threading.Event())["structuredContent"]
        self.assertEqual(modules["common"].validate(schema, refused), [], refused)
        self.assertEqual((refused["ok"], refused["outcome"], refused["error_code"], refused["retry_safe"]),
                         (False, "rejected", "control_generation_stale", True))
        control = refused["control"]
        self.assertEqual((control["state"], control["role"], control["generation"], control["mine"]),
                         ("held", "human", 7, False))
        self.assertEqual(len(self.cli_calls("ui_tap")), 2, "the refused tap ran once and was not repeated")
        self.assertEqual(len(self.cli_calls("control_acquire")), 1, "a refusal is never followed by an acquire")
        # The cache entry is gone: the next tap tries to acquire, is refused, and taps nothing.
        again = server.call_tool(tool, dict(args), threading.Event())["structuredContent"]
        self.assertEqual((again["outcome"], again["error_code"]), ("rejected", "control_held"))
        self.assertEqual(len(self.cli_calls("ui_tap")), 2)
        self.assertEqual(len(self.cli_calls("control_acquire")), 2)
        self.assertEqual(FAKE.get_lease(self.fake_state, "android", ANDROID)["holder"], human)
        self.assert_no_takeover()

    def test_unknown_control_outcomes_point_at_get_session(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started(FAKE_MCP_CLI_MODE="crash:control_acquire")
        host = self.host_id(server)
        unknown = self.call_error(server, "acquire_control", self.ref(host, session_id), "cli_output_invalid",
                                  outcome="outcome_unknown")
        self.assertIn("get_session", unknown["hint"])
        self.assertEqual(len(self.cli_calls("control_acquire")), 1, "an acquire is never repeated")
        self.call_error(server, "release_control", self.ref(host, session_id), "control_not_held")
        self.assertEqual(self.cli_calls("control_release"), [])

    def test_a_stale_generation_is_rejected_once_then_acquired_again(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        tap = self.ref(host, session_id, selector={"text": "Settings"})
        self.call_ok(server, "tap_element", tap)
        agent = {"kind": "agent", "id": "chatgpt-agent:" + host, "label": "ChatGPT agent"}
        FAKE.set_lease(self.fake_state, "android", ANDROID, holder=agent, generation=5)
        stale = self.call_error(server, "tap_element", tap, "control_generation_stale")
        self.assertEqual((stale["control"]["generation"], stale["control"]["mine"]), (5, True))
        self.assertEqual(len(self.cli_calls("ui_tap")), 2)
        self.call_ok(server, "tap_element", tap)
        self.assertEqual(len(self.cli_calls("control_acquire")), 2)
        self.assertEqual(self.cli_calls("ui_tap")[-1]["claim"]["AUTONOM_CONTROL_GENERATION"], "5")
        self.assertEqual(len(self.cli_calls("ui_tap")), 3)

    def test_acquire_and_release_control(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        server = self.started()
        host = self.host_id(server)
        acquired = self.call_ok(server, "acquire_control", self.ref(host, session_id, ttl_s=30))
        control = acquired["control"]
        self.assertEqual((control["state"], control["mine"], control["role"], control["generation"]),
                         ("held", True, "agent", 1))
        self.assertEqual(control["holder"]["id"], "chatgpt-agent:" + host)
        argv = self.cli_calls("control_acquire")[0]["argv"]
        self.assertEqual(argv, ["control", "acquire", "--operation-id", acquired["operation_id"], "--session-id",
                                session_id, "--holder", "chatgpt-agent:" + host, "--kind", "agent", "--label",
                                "ChatGPT agent", "--ttl", "30"])
        self.call_ok(server, "tap_element", self.ref(host, session_id, selector={"text": "Settings"}))
        self.assertEqual(len(self.cli_calls("control_acquire")), 1, "the acquired lease is reused")
        released = self.call_ok(server, "release_control", self.ref(host, session_id))
        self.assertEqual((released["control"]["state"], released["control"]["holder"]), ("free", None))
        self.assertEqual(self.cli_calls("control_release")[0]["argv"][2:4],
                         ["--operation-id", released["operation_id"]])
        self.assertEqual(self.cli_calls("control_release")[0]["argv"][-2:], ["--generation", "1"])
        none = self.call_error(server, "release_control", self.ref(host, session_id), "control_not_held")
        self.assertEqual((none["control"]["state"], none["control"]["holder"]), ("free", None))
        self.assertEqual(len(self.cli_calls("control_release")), 1)
        self.call_error(server, "acquire_control", self.ref(host, session_id, ttl_s=4), "invalid_arguments")

    def test_release_control_after_a_hard_restart_uses_the_recorded_generation(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        first = self.started()
        host = self.host_id(first)
        self.call_ok(first, "acquire_control", self.ref(host, session_id, ttl_s=3600))
        first.proc.kill()  # no graceful shutdown: the lease stays recorded in the CLI
        first.proc.wait(timeout=5)
        self.assertEqual(FAKE.get_lease(self.fake_state, "android", ANDROID)["holder"]["id"],
                         "chatgpt-agent:" + host)
        second = self.started()
        self.assertEqual(self.host_id(second), host)
        before = self.call_ok(second, "get_session", self.ref(host, session_id))["control"]
        self.assertEqual((before["state"], before["mine"], before["generation"]), ("held", True, 1))
        released = self.call_ok(second, "release_control", self.ref(host, session_id))
        self.assertEqual((released["control"]["state"], released["control"]["holder"]), ("free", None))
        calls = self.cli_calls("control_release")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"][2:4], ["--operation-id", released["operation_id"]])
        self.assertEqual(calls[0]["argv"][-4:], ["--holder", "chatgpt-agent:" + host, "--generation", "1"])
        self.assertIsNone(FAKE.get_lease(self.fake_state, "android", ANDROID)["holder"])
        # A person's lease is never released by the agent.
        human = {"kind": "human", "id": "canvas:4000:123", "label": "Canvas 4000"}
        FAKE.set_lease(self.fake_state, "android", ANDROID, holder=human, generation=4)
        refused = self.call_error(second, "release_control", self.ref(host, session_id), "control_not_held")
        self.assertEqual((refused["control"]["role"], refused["control"]["generation"],
                          refused["control"]["mine"]), ("human", 4, False))
        self.assertEqual(len(self.cli_calls("control_release")), 1)
        self.assertEqual(FAKE.get_lease(self.fake_state, "android", ANDROID)["holder"], human)
        failing = self.started(FAKE_MCP_CLI_MODE="crash:control_status")
        self.call_error(failing, "release_control", self.ref(host, session_id), "cli_output_invalid")
        self.assertEqual(len(self.cli_calls("control_release")), 1)

    def test_acquire_never_takes_over_a_human_lease(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        human = {"kind": "human", "id": "canvas:4000:123", "label": "Canvas 4000"}
        FAKE.set_lease(self.fake_state, "android", ANDROID, holder=human, generation=3)
        server = self.started()
        host = self.host_id(server)
        refused = self.call_error(server, "acquire_control", self.ref(host, session_id), "control_held")
        self.assertEqual((refused["control"]["role"], refused["control"]["generation"]), ("human", 3))
        self.assertEqual(FAKE.get_lease(self.fake_state, "android", ANDROID)["holder"], human)
        self.assert_no_takeover()

    def test_dropped_lease_is_acquired_again_on_the_next_tap(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        modules = core.import_server_modules()
        server = modules["server"].McpServer(self.server_env(), self.home, io.BytesIO(), io.BytesIO(),
                                             io.StringIO())
        self.addCleanup(server.shutdown)
        host = server.services.refs.local_host()["host_id"]
        tool = server.tools["tap_element"]
        args = self.ref(host, session_id, selector={"text": "Settings"})
        for _ in range(2):
            result = server.call_tool(tool, dict(args), threading.Event())
            self.assertTrue(result["structuredContent"]["ok"], result)
        self.assertEqual(len(self.cli_calls("control_acquire")), 1)
        server.services.keeper.drop("android", ANDROID)
        result = server.call_tool(tool, dict(args), threading.Event())
        self.assertTrue(result["structuredContent"]["ok"], result)
        self.assertEqual(len(self.cli_calls("control_acquire")), 2, "one acquire after the drop")
        self.assertEqual(len(self.cli_calls("ui_tap")), 3)


if __name__ == "__main__":
    unittest.main()
