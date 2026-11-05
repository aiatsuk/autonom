"""TEST-003 / TEST-004: Android reads, inputs, settle and the coordinate guard
through the on-device helper (AU-01..AU-05, AH-07, RISK-003/004/010/019/020).

Every case runs against `tests/fakes/fake_adb.py` (the exact argv of every adb
call is logged, and it models the helper's device side) and
`tests/fakes/fake_android_helper.py` (an in-process server speaking protocol 1,
fed with the helper XML fixtures under `tests/fixtures/android_helper/`). The
build is replaced by a fixed dex, or by a failed one where a case is about a
host without the tools. The CLI runs in this process, so the same fakes serve
it. No device, no emulator, no javac or d8.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_TOOLS = ROOT / "tests/fakes/fake_android_tools.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FIXTURES = ROOT / "tests/fixtures/android_helper"
HOME_XML = (FIXTURES / "settings_home.xml").read_text(encoding="utf-8")
HOME_DUMP = FIXTURES / "settings_home_uiautomator.txt"
NETWORK_XML = (FIXTURES / "network_internet.xml").read_text(encoding="utf-8")
ABOUT_XML = (FIXTURES / "about_never_idle.xml").read_text(encoding="utf-8")
SPARSE_XML = FIXTURES / "sparse_flutter.xml"
SERIAL = "emulator-5612"
IOS_UDID = "AAAAAAAA-1111-2222-3333-CCCCCCCCCCCC"
BUILD = "0123456789abcdef"
DUMP = ["exec-out", "uiautomator", "dump", "/dev/tty"]

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/fakes"))
from autonom_lib import android_helper as helper_mod  # noqa: E402
from autonom_lib import android_helper_build as build_mod  # noqa: E402
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui, ui_android  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402
import fake_android_helper  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def is_size_call(call: list[str]) -> bool:
    """`wm size` and the rotation sources: the per-tap lookups AU-04 removes."""
    return (call[:3] == ["shell", "wm", "size"] or call[:3] == ["shell", "dumpsys", "input"]
            or call[:3] == ["shell", "dumpsys", "window"]
            or call[:5] == ["shell", "settings", "get", "system", "user_rotation"])


class BackendCase(EnvSandboxMixin, unittest.TestCase):
    """A private state directory, the fake adb and a fake helper serving the
    Settings home screen; AUTONOM_ANDROID_UI_BACKEND unset (auto)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "state"
        self.state = self.root / "adb-state.json"
        self.log = self.root / "adb-log.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        (self.root / "user").mkdir()
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), PATH=str(self.bin),
                     HOME=str(self.root / "user"), JAVA_HOME=None, ANDROID_HOME=None,
                     ANDROID_SDK_ROOT=None, AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_UI_BACKEND=None,
                     AUTONOM_ANDROID_UI_BACKEND=None, AUTONOM_CONTROL_HOLDER=None,
                     AUTONOM_CONTROL_GENERATION=None)
        self.helper = fake_android_helper.FakeHelper(state_path=self.state, xml=HOME_XML).start()
        self.addCleanup(self.helper.close)
        self.write_state()
        self.target = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        self.dex = self.root / "classes.dex"
        self.dex.write_bytes(b"dex\n035\0fake")
        self.build = mock.patch.object(helper_mod.build_mod, "ensure",
                                       return_value=build_mod.Build(self.dex, BUILD, None, None))
        self.built = self.build.start()
        self.addCleanup(self.build.stop)
        ui_android.set_preference(None)
        self.addCleanup(ui_android.set_preference, None)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        self.cli_module: Any = None
        self.last_stderr = ""

    # -- the fake device

    def write_state(self, **extra: Any) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "helper_port": self.helper.port, "ui_dump": str(HOME_DUMP),
                 "wm_size": "Physical size: 1080x2400",
                 "device_files": ["autonom-helper.log"]}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def device(self) -> dict[str, Any]:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def update_device(self, **changes: Any) -> None:
        state = self.device()
        state.update(changes)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def running_helper(self, pid: int = 100) -> None:
        """A helper an earlier call started, still running and recorded."""
        state = self.device()
        state["helper_procs"] = {str(pid): "autonom-helper\0dev.autonom.helper.Main\0"}
        state["helper_running"] = {"pid": pid, "build": BUILD}
        state["forwards"] = [f"{SERIAL} tcp:{self.helper.port} localabstract:autonom-helper"]
        self.state.write_text(json.dumps(state), encoding="utf-8")
        path = helper_mod.record_path(SERIAL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"serial": SERIAL, "port": self.helper.port, "pid": pid,
                                    "build": BUILD}), encoding="utf-8")

    def no_tools(self) -> None:
        self.built.return_value = build_mod.Build(
            None, None, "javac not found", "Install a JDK (17 or newer) and put javac on PATH.")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"][2:] if row["argv"][:1] == ["-s"] else row["argv"] for row in rows]

    def clear_calls(self) -> None:
        self.log.unlink(missing_ok=True)

    def inputs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:2] == ["shell", "input"]]

    def size_calls(self) -> list[list[str]]:
        return [call for call in self.calls() if is_size_call(call)]

    def helper_setup_calls(self) -> list[list[str]]:
        return [call for call in self.calls()
                if call[:1] in (["push"], ["forward"]) or "app_process" in " ".join(call)]

    def ops(self, op: str | None = None) -> list[dict[str, Any]]:
        return [item for item in self.helper.ops(op) if item["op"] != "hello"]

    # -- the CLI, in this process

    def cli(self, *argv: str, stdin: str | None = None) -> tuple[int, dict[str, Any]]:
        return self.cli_raw("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv, stdin=stdin)

    def cli_raw(self, *argv: str, stdin: str | None = None) -> tuple[int, dict[str, Any]]:
        """The CLI with exactly ``argv`` (no Android target flags)."""
        if self.cli_module is None:
            spec = importlib.util.spec_from_file_location("autonom_cli_helper_backend", CLI)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            self.addCleanup(control_mod.end_invocation)
            cwd = os.getcwd()
            os.chdir(self.root)
            self.addCleanup(os.chdir, cwd)
            self.cli_module = module
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = self.cli_module.main(list(argv))
        self.last_stderr = err.getvalue()
        stream = out.getvalue() if out.getvalue().strip() else err.getvalue()
        self.assertNotIn("Traceback", stream)
        return code, json.loads(stream.strip().splitlines()[-1]) if stream.strip() else {}

    def start_session(self, *extra: str) -> dict[str, Any]:
        code, payload = self.cli("session", "start", "--app-id", "com.example.app", *extra)
        self.assertEqual(code, 0, payload)
        return payload["session"]

    def journal(self, session: dict[str, Any], verb: str) -> list[dict[str, Any]]:
        path = Path(session["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        return [entry for entry in entries if entry.get("verb") == verb]

    def assert_observe_interrupted(self, payload: dict[str, Any], verb: str) -> None:
        """AL-11: the action ran (ok), observe says it was cut off and that
        the input must not be sent again."""
        self.assertIs(payload.get("ok"), True, payload)
        self.assertNotIn("error_code", payload)
        error = payload["observe_error"]
        self.assertEqual(error["code"], errors.INTERRUPTED)
        self.assertIn(f"the input of {verb} was sent", error["message"])
        self.assertIn("before the screen it led to was read", error["message"])
        self.assertIn("do not send it again", error["hint"])
        self.assertIn("autonom ui tree", error["hint"])
        for key in ("screen", "changes", "settled", "settle_ms"):
            self.assertNotIn(key, payload)


# --- backend choice (AU-05, AH-02) -------------------------------------------------------

class BackendChoiceTests(BackendCase):
    def test_uiautomator_builds_pushes_and_contacts_no_helper(self) -> None:  # AU-05-S01
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        nodes = ui.snapshot(self.target)
        self.assertTrue(any(node.get("text") == "Network & internet" for node in nodes))
        self.assertEqual(self.calls(), [DUMP])
        ui.tap(self.target, 100, 200)
        self.assertEqual(self.inputs(), [["shell", "input", "tap", "100", "200"]])
        ui.press_key(self.target, "KEYCODE_BACK")
        self.assertEqual(self.inputs()[-1], ["shell", "input", "keyevent", "KEYCODE_BACK"])
        self.built.assert_not_called()
        self.assertEqual(self.helper.ops(), [])
        self.assertEqual(self.helper_setup_calls(), [])
        # the opt-out keeps every reply as it was: no provenance keys
        self.assertEqual(ui_android.provenance(SERIAL), {})

    def test_auto_reads_through_the_helper(self) -> None:
        nodes = ui.snapshot(self.target)
        self.assertTrue(any(node.get("text") == "Network & internet" for node in nodes))
        [read] = self.ops()
        self.assertEqual((read["op"], read["quiet_ms"], read["max_ms"]), ("read", 500, 2000))
        self.assertNotIn(DUMP, self.calls())
        self.assertEqual(ui_android.provenance(SERIAL), {"ui_backend": "helper"})
        self.assertTrue(self.helper_setup_calls(), "the first use starts the helper")

    def test_the_flag_beats_the_variable(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        ui_android.set_preference("uiautomator")
        ui.snapshot(self.target)
        self.assertEqual(self.calls(), [DUMP])
        self.assertEqual(self.helper.ops(), [])
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        ui_android.set_preference("helper")
        self.clear_calls()
        ui.snapshot(self.target)
        self.assertNotIn(DUMP, self.calls())
        self.assertEqual([item["op"] for item in self.ops()], ["read"])
        # an iOS value is no Android choice: the variable decides
        ui_android.set_preference("idb")
        self.assertEqual(ui_android.preference(self.target), "uiautomator")

    def test_an_unknown_variable_value_is_invalid_value(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="fast")
        with self.assertRaises(errors.AutonomError) as caught:
            ui.snapshot(self.target)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(self.calls(), [])

    def test_the_session_choice_is_kept(self) -> None:
        self.start_session("--ui-backend", "uiautomator")
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("ui_backend", payload)
        self.assertNotIn("settled", payload)
        self.assertIn(DUMP, self.calls())
        self.assertEqual(self.helper.ops(), [])
        self.built.assert_not_called()

    def test_auto_without_tools_falls_back_with_the_reason_and_a_hint(self) -> None:  # AH-02-S01
        self.no_tools()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "uiautomator")
        self.assertEqual(payload["fallback_reason"], "javac not found")
        self.assertIn("JDK", payload["fallback_hint"])
        self.assertNotIn("settled", payload)
        self.assertEqual(self.calls().count(DUMP), 1)
        self.assertEqual(self.helper_setup_calls(), [], "no adb call for the helper")
        self.assertEqual(self.helper.ops(), [])
        code, payload = self.cli("ui", "tap", "--x", "100", "--y", "200")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["backend"], payload["input_backend"]), ("adb", "adb"))
        self.assertEqual(payload["fallback_reason"], "javac not found")
        self.assertEqual(self.inputs(), [["shell", "input", "tap", "100", "200"]])

    def test_helper_mode_without_tools_is_unavailable_before_anything(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        self.no_tools()
        code, envelope = self.cli("ui", "tap", "--x", "100", "--y", "200")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.ANDROID_HELPER_UNAVAILABLE)
        self.assertEqual(envelope["reason"], "javac not found")
        self.assertEqual(self.inputs(), [])
        self.assertNotIn(DUMP, self.calls())

    def test_a_value_of_the_other_platform_is_invalid_value(self) -> None:
        code, envelope = self.cli("ui", "tree", "--ui-backend", "xcuitest")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
        self.assertIn("auto, helper, uiautomator", envelope["error"])
        self.assertNotIn(DUMP, self.calls())
        self.assertEqual(self.helper.ops(), [])
        code, envelope = self.cli("--ui-backend", "idb", "ui", "tap", "--x", "1", "--y", "1")
        self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
        self.assertEqual(self.inputs(), [])
        # and the other way round: an Android value for an iOS target
        import argparse

        self.cli("ui", "helper", "status")  # loads the CLI module
        cli = self.cli_module
        ios = Target("ios", "UDID-1", "xcrun", {"udid": "UDID-1"})

        def ui_verb(value: str) -> argparse.Namespace:
            return argparse.Namespace(ui_backend=value, func=cli.cmd_ui_tree)

        with self.assertRaises(errors.AutonomError) as caught:
            cli._check_ui_backend(ui_verb("helper"), ios)  # noqa: SLF001
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertIn("auto, idb, xcuitest", caught.exception.message)
        for value in ("auto", "idb", "xcuitest"):
            cli._check_ui_backend(ui_verb(value), ios)  # noqa: SLF001
        for value in ("auto", "helper", "uiautomator"):
            cli._check_ui_backend(ui_verb(value), self.target)  # noqa: SLF001
        # on Android only session start lets an iOS value through, as before
        # (it stores it); every other verb that resolves a target refuses it,
        # since this invocation's reads and inputs would run with it
        cli._check_ui_backend(  # noqa: SLF001
            argparse.Namespace(ui_backend="idb", func=cli.cmd_session_start), self.target)
        for func in (cli.cmd_flow_run, cli.cmd_ui_do, cli.cmd_report_serve, cli.cmd_proof,
                     cli.cmd_replay, cli.cmd_devices_shutdown):
            with self.assertRaises(errors.AutonomError) as caught:
                cli._check_ui_backend(  # noqa: SLF001
                    argparse.Namespace(ui_backend="xcuitest", func=func), self.target)
            self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
            cli._check_ui_backend(  # noqa: SLF001
                argparse.Namespace(ui_backend="helper", func=func), self.target)
        # an iOS target refuses an Android value on every verb: a session
        # start would store a choice its iOS ui verbs cannot use
        for func in (cli.cmd_session_start, cli.cmd_devices_boot):
            with self.assertRaises(errors.AutonomError) as caught:
                cli._check_ui_backend(  # noqa: SLF001
                    argparse.Namespace(ui_backend="helper", func=func), ios)
            self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
            cli._check_ui_backend(  # noqa: SLF001
                argparse.Namespace(ui_backend="xcuitest", func=func), ios)

    def test_session_start_still_stores_any_choice(self) -> None:
        """`session start` keeps the value it is given (an iOS one on an
        Android session included); Android ui verbs read only an Android one."""
        session = self.start_session("--ui-backend", "idb")
        self.assertEqual(session["ui_backend_preference"], "idb")
        self.assertEqual(ui_android.preference(self.target), "auto")

    def test_an_android_value_is_accepted_by_the_flag(self) -> None:
        code, payload = self.cli("ui", "tree", "--ui-backend", "uiautomator")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("ui_backend", payload)
        self.assertEqual(self.calls().count(DUMP), 1)
        self.assertEqual(self.helper.ops(), [])
        code, payload = self.cli("--ui-backend", "helper", "ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")

    # -- every verb that runs reads or inputs with the value checks it (AU-05)

    FLOW = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"
            "- tapOn:\n    selector:\n      text: Apps\n")

    def flow_file(self) -> Path:
        path = self.root / "flows" / "t.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(self.FLOW, encoding="utf-8")
        return path

    def ios_session(self) -> dict[str, Any]:
        """The current session on an iOS simulator, as `session start` records
        it (no simulator is booted or asked)."""
        return session_mod.start_session("xcrun", platform="ios", target_id=IOS_UDID,
                                         app_id="com.example.app", tooling={"simctl": "xcrun"},
                                         exclusive=True)

    def assert_refused(self, envelope: dict[str, Any], platform: str, allowed: str) -> None:
        self.assertEqual(envelope["error_code"], errors.INVALID_VALUE, envelope)
        self.assertEqual((envelope["flag"], envelope["platform"]), ("--ui-backend", platform))
        self.assertIn(allowed, envelope["error"])

    def test_flow_run_refuses_an_ios_value_before_any_device_call(self) -> None:
        self.start_session()
        path = self.flow_file()
        for argv in (("flow", "run", str(path), "--ui-backend", "xcuitest"),
                     ("--ui-backend", "idb", "flow", "run", str(path))):
            with self.subTest(argv=argv):
                self.clear_calls()
                contacted = len(self.helper.ops())
                code, envelope = self.cli(*argv)
                self.assertEqual(code, 2, envelope)
                self.assert_refused(envelope, "android", "auto, helper, uiautomator")
                self.assertEqual(self.calls(), [], "no device call")
                self.assertEqual(len(self.helper.ops()), contacted, "the helper is not asked")
        self.assertEqual(list((self.home / "sessions").glob("*/flows/*")), [], "no run")
        # an Android value runs the flow with that backend
        self.clear_calls()
        code, payload = self.cli("flow", "run", str(path), "--ui-backend", "uiautomator")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(self.ops("tap"), [])
        self.assertEqual(len(self.inputs()), 1)

    def test_ui_do_refuses_an_ios_value_before_any_step(self) -> None:
        self.start_session()
        self.clear_calls()
        code, envelope = self.cli("ui", "do", "--ui-backend", "idb",
                                  stdin='[{"tapOn":{"text":"Apps"}}]')
        self.assertEqual(code, 2, envelope)
        self.assert_refused(envelope, "android", "auto, helper, uiautomator")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ops("tap"), [])

    def test_flow_run_on_ios_refuses_an_android_value(self) -> None:
        self.ios_session()
        path = self.flow_file()
        self.clear_calls()
        code, envelope = self.cli_raw("--simctl", str(FAKE_SIMCTL), "flow", "run", str(path),
                                      "--ui-backend", "helper")
        self.assertEqual(code, 2, envelope)
        self.assert_refused(envelope, "ios", "auto, idb, xcuitest")
        self.assertEqual(self.calls(), [], "no simulator call")
        self.assertEqual(self.helper.ops(), [])

    def report_serve(self, record: dict[str, Any], *flags: str) -> tuple[int, dict[str, Any]]:
        """`report serve` of a recorded run of ``record``; a server that
        starts fails the case instead of serving forever."""
        import http.server

        run = Path(record["artifacts_dir"]) / "flows" / "fr_1"
        run.mkdir(parents=True)
        (run / "manifest.json").write_text("{}", encoding="utf-8")
        self.clear_calls()
        with mock.patch.object(http.server, "ThreadingHTTPServer",
                               side_effect=AssertionError("the server started")) as server:
            code, envelope = self.cli_raw(*flags, "report", "serve", "--session",
                                          record["session_id"], "--port", "0")
        server.assert_not_called()
        self.assertEqual(self.calls(), [], "no device call")
        return code, envelope

    def test_report_serve_refuses_an_android_value_for_an_ios_session(self) -> None:
        code, envelope = self.report_serve(self.ios_session(), "--simctl", str(FAKE_SIMCTL),
                                           "--ui-backend", "helper")
        self.assertEqual(code, 2, envelope)
        self.assert_refused(envelope, "ios", "auto, idb, xcuitest")
        self.assertEqual(self.helper.ops(), [])

    def test_report_serve_refuses_an_ios_value_for_an_android_session(self) -> None:
        code, envelope = self.report_serve(self.start_session(), "--adb", str(FAKE_ADB),
                                           "--ui-backend", "xcuitest")
        self.assertEqual(code, 2, envelope)
        self.assert_refused(envelope, "android", "auto, helper, uiautomator")


# --- reads (AU-01, AH-07) ------------------------------------------------------------

class ReadTests(BackendCase):
    def test_helper_xml_parses_exactly_like_the_uiautomator_dump(self) -> None:  # AU-01-S01
        dump = HOME_DUMP.read_text(encoding="utf-8")
        from_helper = ui.nodes_from_payload("android", HOME_XML)
        from_dump = ui.nodes_from_payload("android", dump)
        self.assertEqual(from_helper, from_dump)
        self.assertEqual(ui.outline(ui.compact_from_payload("android", HOME_XML)[0]),
                         ui.outline(ui.compact_from_payload("android", dump)[0]))
        self.assertIn('"Network & internet"', ui.outline(from_helper))

    def test_ui_tree_through_the_helper(self) -> None:
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["ui_backend"], payload["settled"], payload["settle_ms"]),
                         ("helper", True, 20))
        self.assertNotIn("fallback_reason", payload)
        self.assertEqual(payload["screen"], [1080, 2400], "from the read's display")
        self.assertTrue(any(node.get("text") == "Network & internet"
                            for node in payload["nodes"]))
        self.assertNotIn(DUMP, self.calls())
        self.assertEqual(self.size_calls(), [])
        self.assertEqual([item["op"] for item in self.ops()], ["read"])

    def test_a_never_idle_screen_is_read_and_marked_not_settled(self) -> None:  # AU-01-S02
        self.helper.xml = ABOUT_XML
        self.helper.replies["read"] = {
            "settled": False, "settle_ms": 2000,
            "busy": {"events": 37, "packages": {"com.android.settings": 37},
                     "last": [{"package": "com.android.settings", "ms": 1990,
                               "class": "android.widget.TextView",
                               "type": "TYPE_WINDOW_CONTENT_CHANGED"}]}}
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["settled"], False)
        self.assertEqual(payload["settle_ms"], 2000)
        texts = {node.get("text") for node in payload["nodes"]}
        self.assertIn("About emulated device", texts)
        self.assertIn("Android version", texts)
        self.assertNotIn(DUMP, self.calls())

    def never_idle(self) -> None:
        """Settings > About: the helper read it after 2000 ms, not quiet."""
        self.helper.xml = ABOUT_XML
        self.helper.replies["read"] = {"settled": False, "settle_ms": 2000,
                                       "busy": {"events": 37,
                                                "packages": {"com.android.settings": 37}}}

    def test_ui_find_reports_a_read_that_did_not_settle(self) -> None:  # AU-01
        self.never_idle()
        code, payload = self.cli("ui", "find", "--text", "Android version")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["matches"][0]["text"], "Android version")
        self.assertEqual((payload["settled"], payload["settle_ms"], payload["ui_backend"]),
                         (False, 2000, "helper"))
        self.assertNotIn(DUMP, self.calls())
        # a quiet screen says so too
        self.helper.replies.pop("read")
        code, payload = self.cli("ui", "find", "--text", "Android version")
        self.assertEqual((payload["settled"], payload["settle_ms"]), (True, 20))

    def test_ui_tree_reports_the_settle_of_the_read_its_nodes_came_from(self) -> None:
        """A sparse tree in a session makes `ui tree` read once more to
        confirm it; when no recovery follows, the nodes are the first
        read's, and so is the `settled` the reply carries."""
        self.start_session()
        self.helper.xml = SPARSE_XML.read_text(encoding="utf-8")
        self.update_device(foreground_app="com.other.app")  # no recovery here
        original = self.helper._perform  # noqa: SLF001
        reads = [0]

        def perform(op: str, request: dict[str, Any]) -> dict[str, Any] | None:
            reply = original(op, request)
            if op == "read":
                reads[0] += 1
                if reads[0] == 1 and isinstance(reply, dict):
                    reply.update(settled=False, settle_ms=2000)
            return reply

        self.helper._perform = perform  # type: ignore[method-assign]  # noqa: SLF001
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(reads[0], 2, "the confirming read was made")
        self.assertNotIn("accessibility_recovery", payload)
        self.assertEqual((payload["settled"], payload["settle_ms"]), (False, 2000))

    def test_ui_find_through_uiautomator_keeps_todays_reply(self) -> None:  # ODA-001
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        code, payload = self.cli("ui", "find", "--text", "Battery")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["count"], 1)
        for key in ("settled", "settle_ms", "ui_backend", "input_backend", "fallback_reason"):
            self.assertNotIn(key, payload)
        self.assertEqual(self.calls(), [DUMP])

    def test_an_auto_fallback_find_names_its_backend_and_claims_no_settle(self) -> None:
        self.no_tools()
        code, payload = self.cli("ui", "find", "--text", "Battery")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["ui_backend"], payload["fallback_reason"]),
                         ("uiautomator", "javac not found"))
        self.assertNotIn("settled", payload, "uiautomator says nothing of a settle")
        self.assertNotIn("settle_ms", payload)

    def test_ui_do_reports_a_final_screen_that_did_not_settle(self) -> None:  # AU-01
        self.start_session()
        self.never_idle()
        code, payload = self.cli("ui", "do",
                                 stdin='[{"assertVisible":{"text":"Android version"}}]')
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed")
        self.assertIn('"Android version"', payload["screen"]["outline"])
        self.assertEqual((payload["settled"], payload["settle_ms"]), (False, 2000))
        self.assertIn("screen_not_settled",
                      [item.get("code") for item in payload.get("warnings", [])])
        saved = json.loads(Path(payload["screen"]["saved"]).read_text(encoding="utf-8"))
        self.assertEqual((saved["settled"], saved["settle_ms"]), (False, 2000),
                         "the saved tree says it too")
        self.assertNotIn(DUMP, self.calls())

    def test_a_failed_helper_read_stops_the_helper_before_uiautomator(self) -> None:
        self.helper.failing_reads = 1
        nodes = ui.snapshot(self.target)
        self.assertTrue(any(node.get("text") == "Apps" for node in nodes))
        calls = self.calls()
        dump_at = calls.index(DUMP)
        kill_or_forward = [index for index, call in enumerate(calls)
                           if call[:2] == ["forward", "--remove"]
                           or call[:2] == ["shell", "kill"]]
        self.assertTrue(kill_or_forward, calls)
        self.assertLess(max(kill_or_forward), dump_at, "the helper is stopped first (INV-3)")
        self.assertEqual(self.helper.ops("quit")[-1]["op"], "quit")
        self.assertFalse(helper_mod.has_record(SERIAL))
        provenance = ui_android.provenance(SERIAL)
        self.assertEqual(provenance["ui_backend"], "uiautomator")
        self.assertIn("could not read the screen (internal", provenance["fallback_reason"])
        # the rest of the invocation stays on uiautomator: no new helper
        before = len(self.helper.ops())
        self.clear_calls()
        ui.snapshot(self.target)
        self.assertEqual(self.calls(), [DUMP])
        self.assertEqual(len(self.helper.ops()), before)

    def test_a_failed_helper_read_in_helper_mode_fails(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        self.helper.failing_reads = 1
        with self.assertRaises(errors.AutonomError) as caught:
            ui.snapshot(self.target)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(caught.exception.extra["helper_error"], "internal")
        self.assertNotIn(DUMP, self.calls())

    def test_explicit_uiautomator_stops_a_live_helper_first(self) -> None:  # AH-07, INV-3
        self.running_helper(pid=100)
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        ui.snapshot(self.target)
        calls = self.calls()
        self.assertEqual(calls[-1], DUMP)
        self.assertIn(["forward", "--remove", f"tcp:{self.helper.port}"], calls[:-1])
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello", "quit"])
        self.assertFalse(helper_mod.has_record(SERIAL))
        self.assertNotIn("helper_running", self.device())
        # the next dump finds nothing to stop
        self.clear_calls()
        ui.snapshot(self.target)
        self.assertEqual(self.calls(), [DUMP])

    def test_no_helper_record_adds_no_adb_call_before_a_dump(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        ui.snapshot(self.target)
        self.assertEqual(self.calls(), [DUMP])

    def test_the_sparse_tree_recovery_stops_the_helper_before_uiautomator(self) -> None:
        """RISK-020: the recovery of `ui tree` reads again; a read the helper
        fails there falls back to uiautomator only after the helper stopped."""
        self.start_session()
        self.helper.xml = SPARSE_XML.read_text(encoding="utf-8")
        self.update_device(ui_dump=str(SPARSE_XML), ui_dump_after_launch=str(HOME_DUMP),
                           settings_secure={"accessibility_enabled": "0"})
        original = self.helper._perform  # noqa: SLF001
        reads = [0]

        def perform(op: str, request: dict[str, Any]) -> dict[str, Any] | None:
            if op == "read":
                reads[0] += 1
                if reads[0] > 1:  # the recovery's own read fails
                    return {"error": {"code": "internal", "message": "boom"}}
            return original(op, request)

        self.helper._perform = perform  # type: ignore[method-assign]  # noqa: SLF001
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["accessibility_recovery"]["recovered"], payload)
        calls = self.calls()
        first_dump = calls.index(DUMP)
        removed = calls.index(["forward", "--remove", f"tcp:{self.helper.port}"])
        self.assertLess(removed, first_dump, "the helper is stopped before any dump")
        self.assertIn("quit", [item["op"] for item in self.helper.ops()])
        self.assertEqual(payload["ui_backend"], "uiautomator")
        self.assertIn("could not read the screen", payload["fallback_reason"])


# Another Autonom process: it holds this device's use lock until its stdin
# closes (or, with the argument "exit", dies holding it).
HOLDER = """
import os, sys
sys.path.insert(0, {scripts!r})
from autonom_lib import android_helper as helper
with helper.use({serial!r}):
    print("held", flush=True)
    if sys.argv[1:] == ["exit"]:
        os._exit(0)
    sys.stdin.read()
"""


class UseLockTests(BackendCase):
    """The per-device use lock (`android_helper.use`): a process talks to the
    helper only while it holds it. Held by another Autonom process past the
    wait, the helper is busy: nothing is sent, stopped or dumped. Held by
    this call, a helper that does not answer is stuck: it is stopped and
    started once (AH-05), and a second failure falls back (auto) or fails
    (helper mode)."""

    def setUp(self) -> None:
        super().setUp()
        self.running_helper(pid=100)
        for patch in (mock.patch.dict(helper_mod.TIMEOUTS_S, {"hello": 0.3, "read": 0.6,
                                                              "tap": 0.6}),
                      mock.patch.object(helper_mod, "USE_WAIT_S", 0.6),
                      mock.patch.object(helper_mod, "START_BUDGET_S", 1.0)):
            patch.start()
            self.addCleanup(patch.stop)

    def holder(self, *argv: str) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER.format(scripts=str(ROOT / "scripts"), serial=SERIAL),
             *argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.addCleanup(self.release, process)
        self.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def release(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout):
            if not stream.closed:
                stream.close()

    def assert_untouched(self) -> None:
        """Nothing reached the helper (not even a hello), nothing was stopped,
        dumped or sent through adb."""
        calls = self.calls()
        self.assertEqual(self.helper.ops(), [])
        self.assertFalse(any(call[:2] == ["shell", "kill"] for call in calls), calls)
        self.assertFalse(any(call[:2] == ["forward", "--remove"] for call in calls), calls)
        self.assertNotIn(DUMP, calls)
        self.assertEqual(self.inputs(), [])
        self.assertIn("100", self.device()["helper_procs"])
        self.assertEqual(helper_mod.load_record(SERIAL).get("pid"), 100)

    def assert_busy(self, payload: dict[str, Any], holder: subprocess.Popen,
                    backend: str = "helper") -> None:
        self.assertIs(payload["helper_busy"], True, payload)
        self.assertEqual(payload["backend"], backend)
        self.assertEqual(payload["helper_owner_pid"], holder.pid)
        self.assertTrue(payload["reason"].startswith("helper_busy: "), payload)
        self.assertIn(f"Autonom process {holder.pid} is using the helper", payload["error"])
        self.assertIn("Retry when it ends", payload["hint"])

    # -- held by another process: busy

    def test_auto_waits_then_fails_the_read_with_helper_busy_touching_nothing(self) -> None:
        holder = self.holder()
        self.clear_calls()
        began = time.monotonic()
        code, payload = self.cli("ui", "tree")
        self.assertGreaterEqual(time.monotonic() - began, 0.6, "it waited for the lock")
        self.assertEqual(code, 2, payload)
        self.assertEqual((payload["error_code"], payload["backend"]),
                         (errors.BACKEND_FAILED, "helper"))
        self.assert_busy(payload, holder)
        self.assert_untouched()
        # once that process let go, the same helper serves the next call
        self.release(holder)
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertNotIn("fallback_reason", payload)
        self.assertEqual(self.device()["helper_running"]["pid"], 100)
        self.assertEqual(self.device().get("helper_starts") or [], [])

    def test_auto_never_sends_an_input_while_another_process_uses_the_helper(self) -> None:
        holder = self.holder()
        self.clear_calls()
        with self.assertRaises(errors.AutonomError) as caught:
            ui.tap(self.target, 540, 700, screen=(1080, 2400))
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assert_busy(caught.exception.as_dict(), holder)
        # the CLI: the guard's display lookup is the first helper call
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual((code, payload["error_code"]), (2, errors.BACKEND_FAILED), payload)
        self.assert_busy(payload, holder)
        self.assert_untouched()

    def test_helper_mode_is_unavailable_with_the_same_reason(self) -> None:
        holder = self.holder()
        self.clear_calls()
        code, payload = self.cli("--ui-backend", "helper", "ui", "tree")
        self.assertEqual((code, payload["error_code"]),
                         (2, errors.ANDROID_HELPER_UNAVAILABLE), payload)
        self.assert_busy(payload, holder)
        self.assert_untouched()

    def test_the_stop_before_a_dump_waits_and_never_cuts_the_other_call(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        holder = self.holder()
        self.clear_calls()
        with self.assertRaises(errors.AutonomError) as caught:
            ui.snapshot(self.target)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assert_busy(caught.exception.as_dict(), holder, "uiautomator")
        self.assertTrue(caught.exception.message.startswith(
            "uiautomator cannot read the screen now"))
        self.assert_untouched()
        # that call ended: the next read stops the helper, then dumps
        self.release(holder)
        self.clear_calls()
        ui.snapshot(self.target)
        calls = self.calls()
        self.assertEqual(calls[-1], DUMP)
        self.assertIn(["forward", "--remove", f"tcp:{self.helper.port}"], calls)
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello", "quit"])
        self.assertFalse(helper_mod.has_record(SERIAL))

    def test_the_stop_and_the_dump_hold_the_lock_together(self) -> None:
        """No other Autonom process can start or use the helper between the
        stop before a dump and the dump, nor while it runs."""
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        seen: list[Any] = []
        real = ui_android.dump_hierarchy

        def dump(*args: Any, **kwargs: Any) -> str:
            def other() -> None:
                try:
                    with helper_mod.use(SERIAL, wait=0):
                        seen.append("got it")
                except helper_mod.HelperBusy as exc:
                    seen.append(exc.owner_pid)

            thread = threading.Thread(target=other)
            thread.start()
            thread.join(10)
            return real(*args, **kwargs)

        with mock.patch.object(ui_android, "dump_hierarchy", side_effect=dump):
            ui.snapshot(self.target)
        self.assertEqual(seen, [os.getpid()], "held during the dump")
        self.assertEqual([item["op"] for item in self.helper.ops()], ["hello", "quit"])
        self.assertIsNone(helper_mod.use_owner(SERIAL), "let go after it")

    def test_a_stop_with_nothing_running_recorded_waits_for_the_lock_too(self) -> None:
        helper_mod.record_path(SERIAL).write_text(json.dumps(
            {"serial": SERIAL, "unavailable": {"code": "start_timeout", "reason": "x",
                                               "hint": "h", "boot_id": "b"}}), encoding="utf-8")
        holder = self.holder()
        self.clear_calls()
        with self.assertRaises(helper_mod.HelperBusy) as caught:
            helper_mod.stop(self.target)
        self.assertEqual(caught.exception.owner_pid, holder.pid)
        self.assertEqual(self.calls(), [])
        self.release(holder)
        self.assertFalse(helper_mod.stop(self.target)["was_running"])
        self.assertEqual(helper_mod.load_record(SERIAL)["unavailable"]["code"], "start_timeout")

    def test_status_without_a_lock_file_sends_nothing(self) -> None:
        helper_mod.use_lock_path(SERIAL).mkdir(parents=True)  # cannot be opened as a file
        result = helper_mod.status(self.target)
        self.assertIsNone(result["answering"])
        self.assertIn("cannot be written", result["error"])
        self.assertEqual((result["pid"], result["port"]), (100, self.helper.port))
        self.assertEqual(self.helper.ops(), [])

    def test_a_busy_refusal_is_not_remembered_by_the_invocation(self) -> None:
        holder = self.holder()
        with self.assertRaises(errors.AutonomError):
            ui.snapshot(self.target)
        self.release(holder)
        # auto did not fall back for it: the next read goes to the helper
        nodes = ui.snapshot(self.target)
        self.assertTrue(any(node.get("text") == "Apps" for node in nodes))
        self.assertNotIn(DUMP, self.calls())
        self.assertEqual(ui_android.provenance(SERIAL), {"ui_backend": "helper"})

    def test_ui_helper_stop_and_status_while_another_process_uses_it(self) -> None:
        holder = self.holder()
        self.clear_calls()
        code, payload = self.cli("ui", "helper", "stop")
        self.assertEqual((code, payload["error_code"]),
                         (2, errors.ANDROID_HELPER_UNAVAILABLE), payload)
        self.assert_busy(payload, holder)
        with mock.patch.object(helper_mod, "STATUS_USE_WAIT_S", 0.2):
            code, payload = self.cli("ui", "helper", "status")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["in_use"], {"pid": holder.pid})
        self.assertEqual((payload["running"], payload["pid"]), (True, 100))
        self.assert_untouched()

    def test_another_thread_of_this_process_waits_too(self) -> None:
        held, done = threading.Event(), threading.Event()

        def hold() -> None:
            with helper_mod.use(SERIAL):
                held.set()
                done.wait(10)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(done.set)
        self.assertTrue(held.wait(10))
        with self.assertRaises(helper_mod.HelperBusy) as caught:
            with helper_mod.use(SERIAL, wait=0.1):
                pass
        self.assertEqual(caught.exception.owner_pid, os.getpid())
        done.set()
        thread.join(10)
        # re-entrant for the thread that holds it, released at the end
        with helper_mod.use(SERIAL, wait=0), helper_mod.use(SERIAL, wait=0):
            self.assertEqual(helper_mod.use_owner(SERIAL), os.getpid())
        self.assertIsNone(helper_mod.use_owner(SERIAL))

    def test_the_lock_goes_with_a_holder_that_exits(self) -> None:  # (d)
        holder = self.holder("exit")
        holder.wait(timeout=10)
        self.assertEqual(helper_mod.use_owner(SERIAL), holder.pid, "it never let go itself")
        with mock.patch.object(helper_mod, "USE_WAIT_S", 0):
            code, payload = self.cli("ui", "tree")  # no wait at all
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertEqual(self.device()["helper_running"]["pid"], 100)

    # -- held by this call: a helper that does not answer is stuck

    def test_a_stuck_helper_is_stopped_and_started_once(self) -> None:  # (b), AH-05
        self.helper.hung_hellos = 1
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertNotIn("fallback_reason", payload)
        calls = self.calls()
        self.assertLess(calls.index(["shell", "cat", "/proc/100/cmdline"]),
                        calls.index(["shell", "kill", "100"]))
        self.assertIn(["forward", "--remove", f"tcp:{self.helper.port}"], calls)
        self.assertEqual([start["pid"] for start in self.device()["helper_starts"]], [4242])
        self.assertEqual(len(self.ops("read")), 1)
        self.assertEqual(helper_mod.load_record(SERIAL)["pid"], 4242)
        self.assertNotIn(DUMP, calls)

    def test_a_helper_that_never_answers_is_stopped_and_auto_falls_back(self) -> None:  # (b)
        self.helper.hung_hellos = -1
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "uiautomator")
        self.assertIn("did not answer hello", payload["fallback_reason"])
        self.assertNotIn("helper_busy", payload)
        calls = self.calls()
        dump_at = calls.index(DUMP)
        self.assertLess(calls.index(["shell", "kill", "100"]), dump_at)
        self.assertLess(calls.index(["forward", "--remove", f"tcp:{self.helper.port}"]), dump_at)
        # the one restart did not answer either: ended before the dump too
        self.assertEqual([start["pid"] for start in self.device()["helper_starts"]], [4242])
        self.assertLess(calls.index(["shell", "kill", "4242"]), dump_at)
        self.assertEqual(self.device().get("helper_procs"), {})
        self.assertEqual(self.ops(), [])

    def test_a_read_unanswered_twice_falls_back_in_auto(self) -> None:  # (c), AH-05
        """A wedged helper: its read never answered, and then neither its
        hello. The read is retried once on a restarted helper; the second
        failure falls back, and the stop before the dump (holding the use
        lock) ends the wedged helper instead of calling it busy."""
        self.helper.hung = {"read": -1}
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "uiautomator")
        self.assertIn("read was not answered twice", payload["fallback_reason"])
        self.assertNotIn("helper_busy", payload)
        self.assertEqual(len(self.helper.wait_ops("read", 2)), 2)
        calls = self.calls()
        dump_at = calls.index(DUMP)
        self.assertLess(calls.index(["shell", "kill", "100"]), dump_at)
        self.assertEqual([start["pid"] for start in self.device()["helper_starts"]], [4242])
        self.assertLess(calls.index(["shell", "kill", "4242"]), dump_at)
        self.assertLess(calls.index(["forward", "--remove", f"tcp:{self.helper.port}"]), dump_at)
        self.assertEqual(self.helper.ops("quit"), [], "a stuck helper gets no quit")
        self.assertFalse(helper_mod.has_record(SERIAL))

    def test_a_read_unanswered_twice_is_unavailable_in_helper_mode(self) -> None:  # (c)
        self.helper.hung = {"read": -1}
        self.clear_calls()
        code, payload = self.cli("--ui-backend", "helper", "ui", "tree")
        self.assertEqual((code, payload["error_code"]),
                         (2, errors.ANDROID_HELPER_UNAVAILABLE), payload)
        self.assertIn("read was not answered twice", payload["reason"])
        self.assertNotIn("helper_busy", payload)
        self.assertEqual(len(self.helper.wait_ops("read", 2)), 2)
        self.assertNotIn(DUMP, self.calls())

    def test_an_unanswered_input_is_uncertain_and_sent_once(self) -> None:  # (e), AH-06
        self.helper.hung = {"tap": 1}
        self.clear_calls()
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.UI_ACTION_UNCERTAIN)
        self.assertEqual((payload["op"], payload["backend"]), ("tap", "helper"))
        self.assertEqual(len(self.helper.wait_ops("tap", 1)), 1)
        self.assertEqual(self.inputs(), [], "never sent through adb")
        # the helper is wedged now: the next call finds it stuck, ends it
        # and starts one; the tap is not sent again
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertIn(["shell", "kill", "100"], self.calls())
        self.assertEqual(len(self.helper.ops("tap")), 1)
        self.assertEqual(self.inputs(), [])

    # -- another state directory: its own lock, its own forward, the same helper

    def share_with_another_state_directory(self) -> str:
        """Another Autonom state directory (another AUTONOM_HOME) reaches
        this device's helper through its own adb forward; this directory's
        use lock does not keep it out."""
        other = f"tcp:{self.helper.port + 1}"
        self.update_device(forwards=self.device()["forwards"]
                           + [f"{SERIAL} {other} localabstract:autonom-helper"])
        return other

    def foreign_call(self, op: str, **args: Any) -> list[dict[str, Any]]:
        """That directory's call, in flight: its own connection, a hello,
        then ``op``; returns once ``op`` reached the helper. Its replies are
        appended to the returned list."""
        replies: list[dict[str, Any]] = []

        def run() -> None:
            with socket.create_connection(("127.0.0.1", self.helper.port), timeout=10) as sock:
                reader = sock.makefile("rb")
                for number, request in enumerate(({"op": "hello"}, {"op": op, **args})):
                    line = json.dumps({"id": f"{'f' * 31}{number}", **request}) + "\n"
                    sock.sendall(line.encode("utf-8"))
                    replies.append(json.loads(reader.readline()))
                reader.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.assertEqual(len(self.helper.wait_ops(op, 1)), 1)
        return replies

    def assert_kept(self) -> None:
        """The helper (pid 100) was neither killed nor replaced, and this
        directory's forward is still there."""
        calls = self.calls()
        self.assertFalse(any(call[:2] == ["shell", "kill"] for call in calls), calls)
        self.assertFalse(any(call[:2] == ["forward", "--remove"] for call in calls), calls)
        self.assertEqual(self.device().get("helper_starts") or [], [])
        self.assertIn("100", self.device()["helper_procs"])
        self.assertEqual(helper_mod.load_record(SERIAL)["pid"], 100)

    def test_a_silent_hello_is_waited_for_while_another_directory_forwards(self) -> None:
        """Holding this directory's lock, the first hello goes unanswered;
        another directory forwards to the helper, so that is no proof of a
        stuck helper: hello is sent again and answered, nothing is ended."""
        self.share_with_another_state_directory()
        self.helper.hung_hellos = 1
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertNotIn("fallback_reason", payload)
        self.assertIn(["forward", "--list"], self.calls(), "the forwards were checked")
        self.assertEqual(len(self.helper.ops("hello")), 2)
        self.assertEqual(len(self.ops("read")), 1)
        self.assert_kept()

    def test_another_directorys_call_in_flight_is_not_cut(self) -> None:
        """The review's scenario: another directory's call is in the helper
        (a settle) when this one reads. Its call gets its answer, and the
        same helper serves this read; nothing is killed or restarted."""
        self.share_with_another_state_directory()
        self.helper.slow = {"display": 0.8}
        with mock.patch.object(helper_mod, "USE_WAIT_S", 5.0):
            replies = self.foreign_call("display")
            self.clear_calls()
            code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertNotIn("fallback_reason", payload)
        self.assertEqual([reply.get("ok") for reply in replies], [True, True],
                         "the other call got its answer")
        self.assertEqual(len(self.ops("read")), 1)
        self.assert_kept()

    def test_still_silent_after_the_wait_it_is_busy_and_nothing_is_stopped(self) -> None:
        other = self.share_with_another_state_directory()
        self.helper.hung_hellos = -1
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual((code, payload["error_code"]), (2, errors.BACKEND_FAILED), payload)
        self.assertEqual(payload["backend"], "helper")
        self.assertIs(payload["helper_busy"], True)
        self.assertEqual(payload["helper_shared_forward"], other)
        self.assertNotIn("helper_owner_pid", payload)
        self.assertTrue(payload["reason"].startswith(
            "helper_busy: the helper on this device did not answer while another Autonom state "
            f"directory forwards {other} to it"), payload)
        self.assertIn("autonom ui helper stop", payload["hint"])
        calls = self.calls()
        self.assertFalse(any(call[:2] == ["shell", "kill"] for call in calls), calls)
        self.assertFalse(any(call[:2] == ["forward", "--remove"] for call in calls), calls)
        self.assertNotIn(DUMP, calls)
        self.assertEqual(self.inputs(), [])
        self.assertIn("100", self.device()["helper_procs"])
        self.assertEqual(helper_mod.load_record(SERIAL).get("pid"), 100)
        # under --ui-backend helper: unavailable, with the same keys
        code, payload = self.cli("--ui-backend", "helper", "ui", "tree")
        self.assertEqual((code, payload["error_code"]),
                         (2, errors.ANDROID_HELPER_UNAVAILABLE), payload)
        self.assertEqual((payload["helper_busy"], payload["helper_shared_forward"]),
                         (True, other))
        self.assertIn("100", self.device()["helper_procs"])
        # an input is never sent through adb for it either
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual((code, payload["error_code"]), (2, errors.BACKEND_FAILED), payload)
        self.assertEqual(self.inputs(), [])
        # an explicit stop still ends it (after the same wait): the user's way out
        code, payload = self.cli("ui", "helper", "stop")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["stopped"], payload["killed"], payload["stuck"]),
                         (True, True, True))
        self.assertEqual(self.device()["forwards"],
                         [f"{SERIAL} {other} localabstract:autonom-helper"],
                         "only this directory's own forward is removed")

    def test_forwards_of_other_devices_and_sockets_do_not_count(self) -> None:
        self.update_device(forwards=self.device()["forwards"] + [
            "emulator-5580 tcp:7002 localabstract:autonom-helper",
            f"{SERIAL} tcp:7001 localabstract:scrcpy"])
        self.helper.hung_hellos = 1
        self.clear_calls()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertIn(["shell", "kill", "100"], self.calls(), "stuck: stopped and started once")
        self.assertEqual([start["pid"] for start in self.device()["helper_starts"]], [4242])

    def test_the_stop_before_a_dump_lets_the_other_directorys_call_end(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        other = self.share_with_another_state_directory()
        self.helper.slow = {"display": 0.8}
        with mock.patch.object(helper_mod, "USE_WAIT_S", 5.0):
            replies = self.foreign_call("display")
            self.clear_calls()
            ui.snapshot(self.target)
        self.assertEqual([reply.get("ok") for reply in replies], [True, True],
                         "the other call was not cut")
        calls = self.calls()
        self.assertEqual(calls[-1], DUMP)
        self.assertNotIn(["shell", "kill", "100"], calls)
        self.assertEqual(self.helper.ops("quit")[-1]["op"], "quit")
        self.assertEqual(self.device()["forwards"],
                         [f"{SERIAL} {other} localabstract:autonom-helper"])
        self.assertFalse(helper_mod.has_record(SERIAL))


# --- inputs (AU-02, AH-06) -----------------------------------------------------------

class InputTests(BackendCase):
    def test_a_selector_tap_goes_to_the_helper(self) -> None:  # AU-02-S01
        code, payload = self.cli("ui", "tap", "--text", "Network & internet")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["backend"], payload["ui_backend"], payload["input_backend"]),
                         ("helper", "helper", "helper"))
        self.assertNotIn("fallback_reason", payload)
        [read, tap] = self.ops()
        self.assertEqual(read["op"], "read")
        self.assertEqual((tap["op"], tap["x"], tap["y"], tap["settle"]),
                         ("tap", payload["x"], payload["y"], False))
        self.assertNotIn("read_after", tap)
        self.assertEqual(self.inputs(), [], "no adb shell input")
        self.assertEqual(self.size_calls(), [], "the size came from the read")

    def test_a_coordinate_tap_with_nothing_recorded_asks_the_display_once(self) -> None:
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["op"] for item in self.ops()], ["display", "tap"])
        self.assertEqual(self.size_calls(), [])
        self.assertEqual(self.inputs(), [])

    def test_long_press_swipe_and_keys_go_to_the_helper(self) -> None:
        screen = (1080, 2400)
        self.assertEqual(ui.long_press(self.target, 10, 20, 800, screen=screen), "helper")
        self.assertEqual(ui.swipe(self.target, 500, 1800, 500, 600, 0.3, screen=screen), "helper")
        for key in ("KEYCODE_BACK", "66", "KEYCODE_HOME", "ENTER", "KEYCODE_A", "KEYCODE_7"):
            self.assertEqual(ui.press_key(self.target, key), "helper")
        long_press, swipe, *keys = self.ops()
        self.assertEqual({k: long_press[k] for k in ("op", "x", "y", "duration_ms", "settle")},
                         {"op": "long_press", "x": 10, "y": 20, "duration_ms": 800,
                          "settle": False})
        self.assertEqual({k: swipe[k] for k in ("op", "x1", "y1", "x2", "y2", "duration_ms")},
                         {"op": "swipe", "x1": 500, "y1": 1800, "x2": 500, "y2": 600,
                          "duration_ms": 300})
        self.assertEqual([item["keycode"] for item in keys], [4, 66, 3, 66, 29, 14])
        self.assertEqual(self.inputs(), [])
        self.assertEqual(ui_android.provenance(SERIAL)["input_backend"], "helper")

    def test_a_key_name_autonom_cannot_number_goes_through_adb_in_auto(self) -> None:
        self.assertEqual(ui.press_key(self.target, "KEYCODE_SOMETHING_NEW"), "adb")
        self.assertEqual(self.inputs(), [["shell", "input", "keyevent", "KEYCODE_SOMETHING_NEW"]])
        self.assertEqual(self.ops("key"), [])
        provenance = ui_android.provenance(SERIAL)
        self.assertNotIn("ui_backend", provenance, "nothing was read")
        self.assertEqual(provenance["input_backend"], "adb")
        self.assertIn("not a key name Autonom knows", provenance["fallback_reason"])

    def test_the_reply_of_an_adb_key_says_why_and_claims_no_read(self) -> None:
        code, payload = self.cli("ui", "key", "KEYCODE_BUTTON_A")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["backend"], payload["input_backend"]), ("adb", "adb"))
        self.assertNotIn("ui_backend", payload)
        self.assertIn("the key is not a key name", payload["fallback_reason"])
        self.assertNotIn("BUTTON_A", payload["fallback_reason"], "no key word in the reason")
        self.assertIn("number", payload["fallback_hint"])
        self.assertEqual(self.inputs(), [["shell", "input", "keyevent", "KEYCODE_BUTTON_A"]])
        self.assertEqual(self.ops(), [])
        # the rest of the invocation still uses the helper: only that key went by adb
        self.assertEqual(ui.press_key(self.target, "KEYCODE_BACK"), "helper")
        self.assertEqual(ui_android.provenance(SERIAL, ui_android.uses(SERIAL) - 1),
                         {"ui_backend": "helper", "input_backend": "helper"})

    def test_keys_the_helper_cannot_take_go_through_adb_in_auto(self) -> None:
        for key in ("0", "KEYCODE_UNKNOWN", "300", "9999"):  # a key number has 4 digits at most
            self.assertEqual(ui.press_key(self.target, key), "adb", key)
            self.assertEqual(self.inputs()[-1], ["shell", "input", "keyevent", key])
            self.assertIn("adb shell input keyevent",
                          ui_android.provenance(SERIAL)["fallback_reason"])
        self.assertEqual(self.ops("key"), [], "nothing the helper would refuse reached it")
        self.assertEqual(ui.press_key(self.target, "283"), "helper")
        self.assertEqual(self.ops("key")[-1]["keycode"], 283)

    def test_helper_mode_refuses_key_zero_and_lets_the_device_judge_a_high_number(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        for key in ("0", "KEYCODE_UNKNOWN"):
            with self.assertRaises(errors.AutonomError) as caught:
                ui.press_key(self.target, key)
            self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(ui.press_key(self.target, "300"), "helper")
        self.assertEqual([item["keycode"] for item in self.ops("key")], [300])
        self.helper.max_keycode = 288  # an older device does not know 300
        with self.assertRaises(errors.AutonomError) as caught:
            ui.press_key(self.target, "300")
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(caught.exception.extra["helper_error"], "bad_request")
        self.assertIn("Nothing was performed", caught.exception.hint)
        self.assertEqual(self.inputs(), [])

    def test_gesture_lengths_reach_the_helper_as_it_takes_them(self) -> None:
        screen = (1080, 2400)
        self.assertEqual(ui.long_press(self.target, 10, 20, 0, screen=screen), "helper")
        self.assertEqual(ui.long_press(self.target, 10, 20, -5, screen=screen), "helper")
        self.assertEqual(ui.swipe(self.target, 500, 1800, 500, 600, 0.0005, screen=screen),
                         "helper")
        self.assertEqual(ui.swipe(self.target, 500, 1800, 500, 600, 0.0, screen=screen),
                         "helper")
        self.assertEqual(ui.long_press(self.target, 10, 20, 60000, screen=screen), "helper")
        lengths = [item["duration_ms"] for item in self.ops()
                   if item["op"] in ("long_press", "swipe")]
        # 0 is down and up at once (1 ms), a negative length adb's 300 ms
        self.assertEqual(lengths, [1, 300, 1, 1, 60000])
        self.assertEqual(self.inputs(), [])

    def test_a_gesture_longer_than_the_helper_takes_goes_through_adb_in_auto(self) -> None:
        self.assertEqual(ui.long_press(self.target, 10, 20, 70000, screen=(1080, 2400)), "adb")
        self.assertEqual(self.inputs(), [["shell", "input", "swipe", "10", "20", "10", "20",
                                          "70000"]])
        self.assertEqual(self.ops("long_press"), [])
        provenance = ui_android.provenance(SERIAL)
        self.assertEqual(provenance["input_backend"], "adb")
        self.assertIn("at most 60000 ms", provenance["fallback_reason"])
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        with self.assertRaises(errors.AutonomError) as caught:
            ui.long_press(self.target, 10, 20, 70000, screen=(1080, 2400))
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(len(self.inputs()), 1, "nothing more was sent")
        self.assertEqual(self.ops("long_press"), [])

    def test_a_key_name_autonom_cannot_number_is_refused_in_helper_mode(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        with self.assertRaises(errors.AutonomError) as caught:
            ui.press_key(self.target, "KEYCODE_SOMETHING_NEW")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.ops("key"), [])

    def test_a_tap_without_a_reply_is_uncertain_and_never_sent_again(self) -> None:  # AH-06-S01
        self.helper.drop_after = {"tap": 1}
        code, envelope = self.cli("ui", "tap", "--x", "100", "--y", "200")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.UI_ACTION_UNCERTAIN)
        self.assertEqual(len(self.helper.ops("tap")), 1)
        self.assertEqual(self.inputs(), [], "no fallback to adb input")

    def test_an_input_the_helper_refuses_is_not_sent_again(self) -> None:
        self.helper.replies["tap"] = {"error": {"code": "inject_failed", "message": "denied"}}
        with self.assertRaises(errors.AutonomError) as caught:
            ui.tap(self.target, 100, 200, screen=(1080, 2400))
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(caught.exception.extra["helper_error"], "inject_failed")
        self.assertIn("in part", caught.exception.hint)
        self.assertEqual(len(self.helper.ops("tap")), 1)
        self.assertEqual(self.inputs(), [])

    def test_auto_sends_through_adb_when_the_helper_is_unavailable(self) -> None:
        self.no_tools()
        self.assertEqual(ui.tap(self.target, 100, 200, screen=(1080, 2400)), "adb")
        self.assertEqual(self.inputs(), [["shell", "input", "tap", "100", "200"]])
        self.assertEqual(ui_android.provenance(SERIAL)["fallback_reason"], "javac not found")

    def test_a_double_tap_is_two_helper_taps(self) -> None:
        self.assertEqual(ui.double_tap(self.target, 10, 10, screen=(1080, 2400)), "helper")
        taps = self.ops("tap")
        self.assertEqual(len(taps), 2)
        self.assertEqual([tap["settle"] for tap in taps], [False, False])
        self.assertEqual(self.inputs(), [])

    def test_a_point_outside_the_screen_reaches_no_helper_input(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            ui.tap(self.target, 5000, 5000)
        self.assertEqual(caught.exception.code, errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(caught.exception.extra["screen"], [1080, 2400])
        self.assertEqual([item["op"] for item in self.ops()], ["display"])

    def test_typing_goes_through_the_helper(self) -> None:  # AT-01
        self.assertEqual(ui.type_text(self.target, "abc"), "helper")
        [typed] = self.ops("type")
        self.assertEqual(typed["text"], "abc")
        self.assertIs(typed["settle"], False, "an unobserved type does not settle")
        self.assertEqual(self.inputs(), [], "no adb shell input text")
        self.assertEqual(ui.input_reply(self.target)["method"], "keys")

    def test_typing_uses_adb_input_text_only_on_the_fallback_path(self) -> None:
        self.no_tools()
        self.assertEqual(ui.type_text(self.target, "abc"), "adb")
        self.assertEqual(self.inputs(), [["shell", "input", "text", "'abc'"]])
        self.assertEqual(self.ops(), [])
        self.assertEqual(ui_android.provenance(SERIAL)["fallback_reason"], "javac not found")
        self.assertIsNone(ui.input_reply(self.target))

    def test_typing_under_the_opt_out_stays_on_adb_input_text(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        self.assertEqual(ui.type_text(self.target, "abc"), "adb")
        self.assertEqual(self.inputs(), [["shell", "input", "text", "'abc'"]])
        self.assertEqual(self.helper.ops(), [], "the opt-out contacts no helper")


# --- settle after input (AU-03) ---------------------------------------------------------

class SettleTests(BackendCase):
    def test_an_observed_tap_takes_one_round_trip(self) -> None:  # AU-03-S01
        self.helper.xml_after = NETWORK_XML
        code, payload = self.cli("ui", "tap", "--text", "Network & internet", "--observe")
        self.assertEqual(code, 0, payload)
        ops = self.ops()
        self.assertEqual([item["op"] for item in ops], ["read", "tap"],
                         "tap, settle and the read after it in one request")
        tap = ops[-1]
        self.assertIs(tap["read_after"], True)
        self.assertEqual(tap["settle"], {"first_event_ms": 300, "quiet_ms": 500, "max_ms": 2000})
        self.assertIs(payload["settled"], True)
        self.assertEqual(payload["settle_ms"], 600)
        self.assertIn('"Airplane mode"', payload["screen"]["outline"])
        self.assertEqual(payload["changes_baseline"], "pre_read")
        self.assertGreater(payload["changes"]["added"], 0)
        self.assertNotIn("screen_not_settled",
                         [item.get("code") for item in payload.get("warnings", [])])
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.size_calls(), [])

    def test_an_input_that_did_not_settle_warns(self) -> None:
        self.helper.xml_after = NETWORK_XML
        self.helper.replies["tap"] = {
            "settled": False, "settle_ms": 2000,
            "busy": {"events": 37, "packages": {"com.android.systemui": 30,
                                                "com.android.settings": 7}}}
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "500", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["settled"], False)
        [warning] = [item for item in payload["warnings"] if item["code"] == "screen_not_settled"]
        self.assertIn("37 accessibility event(s), most from com.android.systemui",
                      warning["error"])
        self.assertIn("--observe-timeout-ms", warning["hint"])
        self.assertIn('"Airplane mode"', payload["screen"]["outline"], "still reported")

    def test_an_interrupt_while_the_helper_settles_keeps_the_action(self) -> None:  # AL-11
        """Ctrl-C while the helper settles after an observed tap: its request
        was written, so the tap happens, and the reply says the action ran
        (as an interrupt during observe's own settle does under
        uiautomator); the journal records it ok, the command exits 130, and
        the tap is never sent again."""
        session = self.start_session()
        real = helper_mod.Connection._readline

        def readline(connection: Any, op: str, rid: str, deadline: float, wait: float) -> bytes:
            if op == "tap":
                raise KeyboardInterrupt  # written, waiting for the settle and the read
            return real(connection, op, rid, deadline, wait)

        with mock.patch.object(helper_mod.Connection, "_readline", readline):
            code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700", "--observe")
        self.assertEqual(code, 130, payload)
        self.assert_observe_interrupted(payload, "ui tap")
        self.assertEqual(self.last_stderr, "", "the reply, not the exit-130 envelope")
        self.assertEqual((payload["backend"], payload["input_backend"]), ("helper", "helper"))
        # the fake helper records the tap when its thread reads it, which may
        # come after the interrupted client returned
        [tap] = self.helper.wait_ops("tap", 1)
        self.assertIs(tap["read_after"], True)
        self.assertEqual(self.inputs(), [], "never sent again through adb")
        [entry] = self.journal(session, "ui tap")
        self.assertTrue(entry["ok"], entry)

    def test_an_interrupt_before_the_input_was_written_still_fails(self) -> None:
        session = self.start_session()
        real = helper_mod.request

        def request(target: Any, op: str, **kwargs: Any) -> dict[str, Any]:
            if op == "tap":
                raise KeyboardInterrupt  # before the tap reached the helper
            return real(target, op, **kwargs)

        with mock.patch.object(helper_mod, "request", side_effect=request):
            code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700", "--observe")
        self.assertEqual(code, 130, payload)
        self.assertEqual(payload["error_code"], errors.INTERRUPTED)
        self.assertEqual(self.ops("tap"), [])
        self.assertEqual(self.inputs(), [])
        [entry] = self.journal(session, "ui tap")
        self.assertFalse(entry["ok"], entry)

    def test_a_zero_observe_budget_claims_no_change(self) -> None:
        """No budget: the tap asks for no settle (`settle: false`), so the
        helper never waited and nothing says the screen was changing."""
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700", "--observe",
                                 "--observe-timeout-ms", "0")
        self.assertEqual(code, 0, payload)
        [tap] = self.ops("tap")
        self.assertIs(tap["settle"], False)
        self.assertEqual((payload["settled"], payload["settle_ms"]), (False, None))
        [warning] = [item for item in payload["warnings"] if item["code"] == "screen_not_settled"]
        self.assertTrue(warning["error"].startswith(
            "could not confirm the screen settled: the helper read it without waiting"),
            warning)
        self.assertNotIn("still changing", warning["error"])
        self.assertNotIn("animation", warning["hint"])
        self.assertIn("--observe-timeout-ms to at least ~800 ms", warning["hint"])

    def test_a_wait_shorter_than_the_quiet_window_claims_no_change(self) -> None:
        self.helper.replies["read"] = {"settled": False, "settle_ms": 300}
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "300")
        self.assertEqual(code, 1, payload)
        [warning] = payload["warnings"]
        self.assertEqual(warning["error"],
                         "could not confirm the screen settled: the helper saw no accessibility "
                         "event in its 300 ms wait, shorter than the 500 ms quiet window")
        self.assertIn("raise --timeout-ms by at least ~500 ms", warning["hint"])

    def test_still_changing_needs_an_event_in_the_last_quiet_window(self) -> None:
        def message(**result: Any) -> tuple[str, str]:
            return ui.unsettled_message({"settled": False, "method": "helper", **result},
                                        3000, 500, timeout_name="--observe-timeout-ms")

        def busy(*times: int) -> dict[str, Any]:
            return {"events": len(times), "packages": {"com.example.app": len(times)},
                    "last": [{"package": "com.example.app", "ms": ms} for ms in times]}

        error, hint = message(settle_ms=2000, busy=busy(100, 1800))
        self.assertTrue(error.startswith("the screen was still changing after 2000 ms"), error)
        self.assertIn("2 accessibility event(s), most from com.example.app", error)
        self.assertIn("animation", hint)
        # events, but none in the last 500 ms of the wait (the app answered late)
        error, hint = message(settle_ms=2000, busy=busy(40, 90))
        self.assertNotIn("still changing", error)
        self.assertIn("did not fit", error)
        self.assertIn("It may have just settled", hint)
        # events, in a wait shorter than the quiet window
        error, hint = message(settle_ms=300, busy=busy(250))
        self.assertNotIn("still changing", error)
        self.assertIn("shorter than the 500 ms quiet window", error)
        self.assertIn("by at least ~500 ms", hint)
        # events counted, none timed: as before, still changing
        error, _ = message(settle_ms=2000, busy={"events": 3})
        self.assertTrue(error.startswith("the screen was still changing"), error)
        # no event at all: never "still changing"
        error, hint = message(settle_ms=2000)
        self.assertTrue(error.startswith("could not confirm the screen settled"), error)
        self.assertIn("Nothing was seen changing", hint)

    def test_observe_budget_shapes_the_settle(self) -> None:
        self.assertEqual(ui_android.settle_args(3000)["settle"]["max_ms"], 2000)
        self.assertEqual(ui_android.settle_args(1500)["settle"],
                         {"first_event_ms": 300, "quiet_ms": 500, "max_ms": 1500})
        self.assertEqual(ui_android.settle_args(200)["settle"]["first_event_ms"], 200)
        self.assertEqual(ui_android.settle_args(6000)["settle"]["max_ms"], 5000)
        self.assertEqual(ui_android.settle_args(0), {"settle": False, "read_after": True})
        # never more than one helper request waits
        self.assertEqual(ui_android.settle_args(70000)["settle"]["max_ms"], 60000)
        self.assertEqual(ui_android.settle_args(70000, 90000)["settle"]["quiet_ms"], 60000)
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "500", "--observe",
                                 "--observe-timeout-ms", "9000")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.ops("tap")[-1]["settle"]["max_ms"], 8000)

    def test_a_failed_read_after_the_input_waits_only_for_what_is_left(self) -> None:
        """The helper settled within the observe budget, then its read after
        the input failed (`read_error`): observe's own read gets only what
        the input left of `--observe-timeout-ms`, counted from the input."""
        self.helper.failing_read_after = 1
        self.helper.slow = {"tap": 0.4}  # the settle took 400 ms
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "500", "--observe",
                                 "--observe-timeout-ms", "1000")
        self.assertEqual(code, 0, payload)
        [tap] = self.ops("tap")
        self.assertEqual(tap["settle"]["max_ms"], 1000)
        [read] = self.ops("read")
        self.assertGreater(read["max_ms"], 0)
        self.assertLessEqual(read["max_ms"], 600, "the 400 ms the input took are spent")
        self.assertIs(payload["settled"], True)
        self.assertIn('"Network & internet"', payload["screen"]["outline"])
        self.assertEqual(self.inputs(), [])

    def test_with_the_budget_spent_the_read_after_a_failed_one_does_not_wait(self) -> None:
        self.helper.failing_read_after = 1
        self.helper.slow = {"tap": 1.2}  # longer than the whole budget
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "500", "--observe",
                                 "--observe-timeout-ms", "1000")
        self.assertEqual(code, 0, payload)
        [read] = self.ops("read")
        self.assertEqual(read["max_ms"], 0, "one read that does not wait")
        self.assertIn('"Network & internet"', payload["screen"]["outline"])
        # the helper's answer for that read: the screen was already quiet
        self.assertIs(payload["settled"], True)
        self.assertNotIn("screen_not_settled",
                         [item.get("code") for item in payload.get("warnings", [])])

    def test_the_follow_up_budget_counts_from_the_end_of_the_input(self) -> None:
        """Neither the connection set-up before the input (a hello the helper
        is slow to answer) nor a long press's own length is spent from the
        observe budget: only what followed the end of the input."""
        self.helper.failing_read_after = 2
        self.helper.slow = {"hello": 0.8, "tap": 0.2}
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "500", "--observe",
                                 "--observe-timeout-ms", "1000")
        self.assertEqual(code, 0, payload)
        [read] = self.ops("read")
        self.assertGreaterEqual(read["max_ms"], 400, "the 800 ms hello is not spent")
        self.assertLessEqual(read["max_ms"], 800, "the 200 ms settle is")
        self.helper.slow = {"long_press": 0.9}  # 700 ms of gesture, then 200 ms
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "500", "--duration", "700",
                                 "--observe", "--observe-timeout-ms", "1000")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.ops("long_press")[-1]["duration_ms"], 700)
        read = self.ops("read")[-1]
        self.assertGreaterEqual(read["max_ms"], 400, "the 700 ms gesture is not spent")
        self.assertLessEqual(read["max_ms"], 800)

    def test_an_adb_input_is_observed_through_one_helper_read(self) -> None:
        # typing goes through the helper now (AT-01); a key name Autonom has
        # no number for is an input auto still sends with adb while the
        # helper serves the reads
        code, payload = self.cli("ui", "key", "KEYCODE_SOMETHING_NEW", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["input_backend"], "adb")
        self.assertEqual(self.inputs(), [["shell", "input", "keyevent", "KEYCODE_SOMETHING_NEW"]])
        self.assertEqual(self.ops("key"), [])
        # observe's one read, which waits for quiet itself within the observe
        # budget (the helper took no input, so there is no settle block)
        [read] = self.ops("read")
        self.assertEqual((read["quiet_ms"], read["max_ms"]), (500, 3000))
        self.assertIs(payload["settled"], True)
        self.assertNotIn(DUMP, self.calls())

    def test_an_observed_type_through_the_helper_takes_one_round_trip(self) -> None:  # AT-01
        code, payload = self.cli("ui", "type", "abc", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["input_backend"], "helper")
        # the focus read before typing, then type with its settle and the read
        # after it in the same request: observe reads nothing more
        self.assertEqual([item["op"] for item in self.ops()], ["read", "type"])
        [typed] = self.ops("type")
        self.assertIs(typed["read_after"], True)
        self.assertIsInstance(typed["settle"], dict)
        self.assertIs(payload["settled"], True)
        self.assertEqual(self.inputs(), [])
        self.assertNotIn(DUMP, self.calls())

    def test_ui_wait_settled_lets_the_helper_wait_for_quiet(self) -> None:
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "3000",
                                 "--quiet-ms", "700")
        self.assertEqual(code, 0, payload)
        [read] = self.ops()
        self.assertEqual((read["op"], read["quiet_ms"], read["max_ms"]), ("read", 700, 3000))
        self.assertEqual((payload["method"], payload["settled"], payload["snapshots"]),
                         ("helper", True, 1))
        self.assertEqual(payload["settle_ms"], 20)
        self.assertNotIn("warnings", payload)
        self.assertNotIn(DUMP, self.calls())

    def test_ui_wait_settled_on_a_screen_that_never_quiets(self) -> None:
        self.helper.replies["read"] = {"settled": False, "settle_ms": 3000,
                                       "busy": {"events": 12, "app_answered": False,
                                                "packages": {"com.example.app": 12}}}
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "3000")
        self.assertEqual(code, 1, payload)
        self.assertIs(payload["settled"], False)
        [warning] = payload["warnings"]
        self.assertEqual(warning["code"], "screen_not_settled")
        self.assertIn("did not answer", warning["error"])
        self.assertEqual(payload["busy"]["events"], 12)

    def test_ui_wait_settled_falls_back_to_polling_without_the_helper(self) -> None:
        self.no_tools()
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "3000",
                                 "--quiet-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["method"], "poll")
        self.assertEqual(payload["quiet_fallback"]["code"], errors.ANDROID_HELPER_UNAVAILABLE)
        # the fallback's own read is the poll's first snapshot
        self.assertEqual((payload["snapshots"], self.calls().count(DUMP)), (2, 2))

    def test_a_failed_helper_read_in_the_settle_is_the_polls_first_snapshot(self) -> None:
        # A budget no load uses up: with --quiet-ms 0 the poll ends at the
        # second dump that matches the first, whenever that comes.
        self.helper.failing_reads = 5
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "30000",
                                 "--quiet-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["method"], "poll")
        self.assertIn("could not read the screen", payload["quiet_fallback"]["error"])
        self.assertEqual((payload["snapshots"], self.calls().count(DUMP)), (2, 2),
                         "no dump of the budget is thrown away")
        self.assertEqual(len(self.ops("read")), 1)

    def test_the_poll_times_the_fallback_read_by_its_dump_alone(self) -> None:
        """The seed's time is the dump's, not the helper read that failed
        first nor the stop before the dump: a slow failed read does not make
        the poll forecast a slow dump."""
        self.helper.failing_reads = 1
        self.helper.slow = {"read": 4.0}
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "30000",
                                 "--quiet-ms", "0")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["method"], payload["snapshots"]), ("poll", 2))
        self.assertGreaterEqual(payload["elapsed_ms"], 4000)
        self.assertLess(payload["dump_ms"], 4000, payload)

    def test_a_long_budget_is_spent_over_reads_one_request_can_wait_for(self) -> None:
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "90000")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["max_ms"] for item in self.ops("read")], [60000])
        self.assertEqual((payload["method"], payload["snapshots"]), ("helper", 1))
        # a screen that never quiets: the rest of the budget in a second read
        self.helper.replies["read"] = {"settled": False, "settle_ms": 60000}
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "90000",
                                 "--quiet-ms", "60000")
        self.assertEqual(code, 1, payload)
        self.assertEqual([(item["quiet_ms"], item["max_ms"]) for item in self.ops("read")][1:],
                         [(60000, 60000), (60000, 30000)])
        self.assertEqual((payload["method"], payload["snapshots"]), ("helper", 2))
        self.assertNotIn("fallback_reason", payload)
        self.assertEqual(self.ops("quit"), [], "the helper stays")
        self.assertNotIn(DUMP, self.calls())

    def test_a_quiet_window_above_one_request_is_polled_through_the_helper(self) -> None:
        code, payload = self.cli("ui", "wait", "--settled", "--timeout-ms", "300",
                                 "--quiet-ms", "70000")
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["method"], "poll")
        self.assertNotIn("quiet_fallback", payload)
        reads = self.ops("read")
        self.assertTrue(reads)
        self.assertTrue(all(item["quiet_ms"] <= 60000 and item["max_ms"] <= 60000
                            for item in reads), reads)
        self.assertEqual(self.ops("quit"), [])
        self.assertNotIn(DUMP, self.calls())

    def test_an_observe_budget_above_one_request_still_sends_the_tap(self) -> None:
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700", "--observe",
                                 "--observe-timeout-ms", "70000")
        self.assertEqual(code, 0, payload)
        [tap] = self.ops("tap")
        self.assertEqual(tap["settle"]["max_ms"], 60000)
        self.assertEqual(payload["input_backend"], "helper")
        self.assertIs(payload["settled"], True)
        self.assertEqual(self.inputs(), [])


# --- the coordinate guard's size (AU-04, RISK-010) ---------------------------------------

class GuardSizeTests(BackendCase):
    def test_a_coordinate_tap_after_a_tree_makes_no_size_call(self) -> None:  # AU-04-S01
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        self.start_session()
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["screen"], [1080, 2400])
        self.clear_calls()
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.size_calls(), [])
        self.assertEqual(self.inputs(), [["shell", "input", "tap", "540", "700"]])

    def test_a_selector_tap_after_a_tree_makes_no_size_call(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        self.start_session()
        self.cli("ui", "tree")
        self.clear_calls()
        code, payload = self.cli("ui", "tap", "--text", "Battery")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.size_calls(), [])
        self.assertEqual(self.calls()[0], DUMP)
        self.assertEqual(len(self.inputs()), 1)

    def test_through_the_helper_a_tap_after_a_tree_asks_nothing(self) -> None:
        self.start_session()
        self.cli("ui", "tree")
        before = len(self.ops())
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["op"] for item in self.ops()[before:]], ["tap"])
        self.assertEqual(self.size_calls(), [])

    def test_a_refusal_on_a_recorded_size_is_decided_on_one_fresh_lookup(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        self.start_session()
        self.cli("ui", "tree")  # records 1080x2400, portrait
        self.update_device(settings_system={"user_rotation": "1"})  # turned to landscape
        self.clear_calls()
        code, payload = self.cli("ui", "tap", "--x", "2000", "--y", "500")
        self.assertEqual(code, 0, payload)
        self.assertEqual([call for call in self.calls() if call[:3] == ["shell", "wm", "size"]],
                         [["shell", "wm", "size"]], "one lookup")
        self.assertEqual(self.inputs(), [["shell", "input", "tap", "2000", "500"]])
        # a point the fresh lookup refuses too stays refused, nothing is sent
        self.clear_calls()
        code, envelope = self.cli("ui", "tap", "--x", "5000", "--y", "500")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(envelope["screen"], [2400, 1080])
        self.assertEqual(self.inputs(), [])

    def test_a_portrait_session_size_never_sends_a_point_off_the_turned_screen(self) -> None:
        """The session recorded 1080x2400 (portrait); the device turned to
        2400x1080. (500, 2000) fits the recorded size but not its
        transpose, so it is looked up once and refused: nothing is sent. A
        point inside both orientations is still sent with no lookup."""
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        self.start_session()
        self.cli("ui", "tree")  # records 1080x2400, portrait, in the session
        self.update_device(settings_system={"user_rotation": "1"})  # turned to landscape
        self.clear_calls()
        code, envelope = self.cli("ui", "tap", "--x", "500", "--y", "2000")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(envelope["screen"], [2400, 1080])
        self.assertEqual(self.calls().count(["shell", "wm", "size"]), 1, "one lookup")
        self.assertEqual(self.inputs(), [])
        self.clear_calls()
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.size_calls(), [])
        self.assertEqual(self.inputs(), [["shell", "input", "tap", "540", "700"]])

    def test_through_the_helper_a_session_size_from_before_a_turn_asks_the_display(self) -> None:
        self.start_session()
        self.cli("ui", "tree")  # records 1080x2400 from the helper read
        self.helper.display = {"width": 2400, "height": 1080, "rotation": 1}
        before = len(self.ops())
        code, envelope = self.cli("ui", "tap", "--x", "500", "--y", "2000")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual([item["op"] for item in self.ops()[before:]], ["display"])

    def test_through_the_helper_a_stale_refusal_asks_the_display_once(self) -> None:
        self.start_session()
        self.cli("ui", "tree")  # records 1080x2400 from the read
        self.helper.display = {"width": 2400, "height": 1080, "rotation": 1}
        before = len(self.ops())
        code, payload = self.cli("ui", "tap", "--x", "2000", "--y", "500")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["op"] for item in self.ops()[before:]], ["display", "tap"])
        self.assertEqual(self.size_calls(), [])

    def test_the_recorded_geometry_is_kept_in_the_session(self) -> None:
        session = self.start_session()
        self.cli("ui", "tree")
        stored = json.loads((Path(session["artifacts_dir"]) / "session.json").read_text())
        self.assertEqual(stored["android_geometry"],
                         {"width": 1080, "height": 2400, "rotation": 0})

    def test_a_size_the_caller_passes_needs_no_lookup_and_no_record(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        ui.tap(self.target, 10, 10, screen=(1080, 2400))
        self.assertEqual(self.calls(), [["shell", "input", "tap", "10", "10"]])

    # Battery's title is at [189,1078][1036,1137] in the Settings home dump
    BATTERY_TAP = ["shell", "input", "tap", "612", "1107"]

    def test_a_uiautomator_selector_tap_takes_the_size_from_its_own_dump(self) -> None:  # AU-04
        """No `ui tree` before it, nothing recorded: one dump, then the tap,
        and nothing in between (no `wm size`, `dumpsys input`, `dumpsys
        window`, `settings get`)."""
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        code, payload = self.cli("ui", "tap", "--text", "Battery")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.calls(), [DUMP, self.BATTERY_TAP])
        self.assertEqual(self.helper.ops(), [], "no helper contacted")
        for key in ("ui_backend", "input_backend", "fallback_reason"):
            self.assertNotIn(key, payload, "the opt-out reply is today's (ODA-001)")

    def test_after_an_auto_fallback_a_selector_tap_asks_no_size_either(self) -> None:
        self.no_tools()
        code, payload = self.cli("ui", "tap", "--text", "Battery")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.calls(), [DUMP, self.BATTERY_TAP])
        self.assertEqual((payload["ui_backend"], payload["input_backend"]),
                         ("uiautomator", "adb"))

    def test_a_coordinate_tap_with_no_read_and_nothing_recorded_looks_up_once(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 0, payload)
        calls = self.calls()
        self.assertEqual(calls[-1], ["shell", "input", "tap", "540", "700"])
        self.assertEqual(calls[0], ["shell", "wm", "size"], "one lookup, before the tap")
        self.assertTrue(all(is_size_call(call) for call in calls[:-1]), calls)
        self.assertEqual(calls.count(["shell", "wm", "size"]), 1)

    def test_a_dump_root_is_this_commands_size_not_the_sessions(self) -> None:
        """The dump's root covers less than the display under a dialog, so
        its size is not written to the session: a later command's
        coordinate tap still looks the screen up once."""
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        session = self.start_session()
        code, payload = self.cli("ui", "find", "--text", "Battery")
        self.assertEqual(code, 0, payload)
        stored = json.loads((Path(session["artifacts_dir"]) / "session.json").read_text())
        self.assertNotIn("android_geometry", stored)
        self.clear_calls()
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.calls().count(["shell", "wm", "size"]), 1)

    def test_a_point_outside_a_dialog_root_is_decided_on_one_lookup(self) -> None:
        """A dialog as the active window: the dump's root ends at 1017x1534.
        A point inside it is sent with no lookup; a point below it (still on
        the 1080x2400 screen) is refused on that size, looked up once and
        sent; a point off the screen stays refused."""
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        dialog = self.root / "dialog.xml"
        dialog.write_text(
            '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
            '<node index="0" text="" class="android.widget.FrameLayout" '
            'package="com.example.app" bounds="[63,866][1017,1534]">'
            '<node index="0" text="Allow" class="android.widget.Button" '
            'package="com.example.app" clickable="true" enabled="true" '
            'bounds="[700,1400][980,1500]" /></node></hierarchy>', encoding="utf-8")
        self.update_device(ui_dump=str(dialog))
        self.assertEqual(ui_android.dump_screen(dialog.read_text(encoding="utf-8")),
                         (1017, 1534, 0))
        with mock.patch.object(ui, "_pretest_guard"):
            ui.snapshot(self.target)
            ui.tap(self.target, 840, 1450)
            self.assertEqual(self.calls(), [DUMP, ["shell", "input", "tap", "840", "1450"]])
            self.clear_calls()
            ui.tap(self.target, 540, 2000)
            calls = self.calls()
            self.assertEqual(calls[0], ["shell", "wm", "size"])
            self.assertEqual(calls[-1], ["shell", "input", "tap", "540", "2000"])
            self.clear_calls()
            with self.assertRaises(errors.AutonomError) as caught:
                ui.tap(self.target, 540, 3000)
        self.assertEqual(caught.exception.code, errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(caught.exception.extra["screen"], [1080, 2400])
        self.assertEqual(self.inputs(), [])


class SharedSizeLookupTests(BackendCase):
    """`ui.screen_size` outside the coordinate guard (Mobile Canvas's
    /status poll through the bridge's `screen-size` op, a Canvas recording
    on an emulator, the Flow engine's cache) stays today's `wm size` and
    rotation lookup in the default auto mode: it never builds, starts,
    asks or waits for the helper (impact analysis: Canvas is unaffected;
    NG-4). Only the guard asks the helper's display."""

    def assert_helper_untouched(self) -> None:
        self.assertEqual(self.helper.ops(), [], "not even a hello")
        self.assertEqual(self.helper_setup_calls(), [], "no push, forward or start")
        self.built.assert_not_called()

    def test_the_canvas_status_poll_never_starts_the_helper(self) -> None:
        import autonom_canvas_bridge as bridge  # noqa: PLC0415 - Canvas only

        for number in range(3):  # three /status polls, 1.2 s apart on the page
            answer = bridge.dispatch(self.target, {"id": number, "op": "screen-size",
                                                   "origin": "system", "payload": {}})
            self.assertEqual(answer, {"ok": True, "display": {"width": 1080, "height": 2400}})
        self.assertEqual(self.calls().count(["shell", "wm", "size"]), 3)
        self.assert_helper_untouched()
        self.assertFalse(helper_mod.has_record(SERIAL))
        self.assertEqual(ui_android.provenance(SERIAL), {})

    def test_a_running_helper_is_neither_asked_nor_waited_for(self) -> None:
        """An agent's call holds the use lock (a settle, a first-use build):
        the size lookup does not queue behind it."""
        self.running_helper(pid=100)
        held, done = threading.Event(), threading.Event()

        def agent() -> None:
            with helper_mod.use(SERIAL):
                held.set()
                done.wait(30)

        thread = threading.Thread(target=agent, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(done.set)
        self.assertTrue(held.wait(10))
        began = time.monotonic()
        self.assertEqual(ui.screen_size(self.target), (1080, 2400))
        self.assertLess(time.monotonic() - began, helper_mod.USE_WAIT_S / 2, "no lock wait")
        done.set()
        self.assertEqual(self.helper.ops(), [])
        self.assertIn(["shell", "wm", "size"], self.calls())
        self.assertFalse(any(call[:2] == ["shell", "kill"] for call in self.calls()))
        self.assertEqual(helper_mod.load_record(SERIAL)["pid"], 100)

    def test_the_guard_alone_asks_the_helper_display(self) -> None:
        self.assertEqual(ui.screen_size(self.target), (1080, 2400))
        self.assertEqual(self.helper.ops(), [])
        self.clear_calls()
        ui.tap(self.target, 540, 700)
        self.assertEqual([item["op"] for item in self.ops()], ["display", "tap"])
        self.assertEqual(self.size_calls(), [])


class DumpScreenTests(unittest.TestCase):
    """`ui_android.dump_screen`: the screen a uiautomator dump lies on, from
    its root node's bounds (clipped by uiautomator to the display as it is
    now) and `<hierarchy rotation>`."""

    def test_a_full_screen_root_is_the_display(self) -> None:
        self.assertEqual(ui_android.dump_screen(HOME_DUMP.read_text(encoding="utf-8")),
                         (1080, 2400, 0))
        landscape = ('<hierarchy rotation="1"><node index="0" text="" '
                     'bounds="[0,0][2400,1080]"><node bounds="[0,0][10,10]"/></node>'
                     '</hierarchy>')
        self.assertEqual(ui_android.dump_screen(landscape), (2400, 1080, 1))

    def test_adb_noise_before_the_document_is_skipped(self) -> None:
        text = ('UI hierchary dumped to: /dev/tty\n<?xml version="1.0"?>\n'
                '<hierarchy rotation="0">\n  <node index="0" class="a" '
                'bounds="[0,0][720,1280]" />\n</hierarchy>')
        self.assertEqual(ui_android.dump_screen(text), (720, 1280, 0))

    def test_no_usable_root_names_no_size(self) -> None:
        for text in ("", "ERROR: could not get idle state.",
                     '<hierarchy rotation="0"></hierarchy>',
                     '<hierarchy rotation="0"><node index="0" class="a" /></hierarchy>',
                     '<hierarchy rotation="0"><node bounds="[0,0][0,0]" /></hierarchy>',
                     '<hierarchy><node bounds="[0,0][1080" /></hierarchy>',
                     # no display is this large: no size, and no exception
                     '<hierarchy><node bounds="[0,0][1080,' + "9" * 5000 + ']" /></hierarchy>',
                     '<hierarchy><node bounds="[0,0][99999999999,99999999999]" /></hierarchy>',
                     '<hierarchy><node bounds="[0,0][200000,2400]" /></hierarchy>'):
            with self.subTest(text=text[:80]):
                self.assertIsNone(ui_android.dump_screen(text))
        self.assertIsNone(ui_android.dump_screen(None))  # type: ignore[arg-type]

    def test_only_the_root_counts_and_a_missing_rotation_is_unknown(self) -> None:
        # a root without bounds is not replaced by its child's
        text = ('<hierarchy><node index="0" class="a"><node bounds="[0,0][100,100]"/>'
                '</node></hierarchy>')
        self.assertIsNone(ui_android.dump_screen(text))
        self.assertEqual(ui_android.dump_screen('<hierarchy><node bounds="[0,0][5,6]"/>'
                                                '</hierarchy>'), (5, 6, None))


# --- Flow (RISK-019, AU-02 provenance) ----------------------------------------------------

class FlowTests(BackendCase):
    HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"

    def run_flow(self, body: str):
        flow = flow_schema.build_flow(flow_parser.parse_document(self.HEAD + body, "t.yaml"))
        session = {"session_id": "s_test", "artifacts_dir": str(self.root / "run")}
        runner = flow_executor.Executor(self.target, session, flow_executor.RunConfig(),
                                        screen=(1080, 2400))
        return runner, runner.run(flow)

    def events(self, runner: Any) -> list[dict[str, Any]]:
        path = next((self.root / "run").rglob("events.ndjson"))
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_step_event_names_the_backends_that_served_it(self) -> None:
        runner, result = self.run_flow("- tapOn:\n    selector:\n      text: Apps\n")
        self.assertEqual(result.status, "passed")
        [finished] = [event for event in self.events(runner)
                      if event["kind"] == "flow.step.finished"]
        self.assertEqual((finished["payload"]["ui_backend"], finished["payload"]["input_backend"]),
                         ("helper", "helper"))
        self.assertEqual(len(self.helper.ops("tap")), 1)

    def finished(self, runner: Any) -> list[dict[str, Any]]:
        return [event["payload"] for event in self.events(runner)
                if event["kind"] == "flow.step.finished"]

    def test_each_step_names_only_what_served_it(self) -> None:
        runner, result = self.run_flow("- tapOn:\n    selector:\n      text: Apps\n"
                                       "- assertVisible:\n    selector:\n      text: Apps\n")
        self.assertEqual(result.status, "passed")
        tap, check = self.finished(runner)
        self.assertEqual((tap["ui_backend"], tap["input_backend"]), ("helper", "helper"))
        self.assertEqual(check["ui_backend"], "helper")
        self.assertNotIn("input_backend", check, "the assertion sent nothing")

    def test_after_a_fallback_every_step_says_why(self) -> None:
        self.no_tools()
        runner, result = self.run_flow("- tapOn:\n    selector:\n      text: Apps\n"
                                       "- assertVisible:\n    selector:\n      text: Apps\n")
        self.assertEqual(result.status, "passed")
        tap, check = self.finished(runner)
        self.assertEqual((tap["ui_backend"], tap["input_backend"], tap["fallback_reason"]),
                         ("uiautomator", "adb", "javac not found"))
        self.assertEqual((check["ui_backend"], check["fallback_reason"]),
                         ("uiautomator", "javac not found"))
        self.assertNotIn("input_backend", check)

    def test_a_step_event_says_whether_its_helper_read_settled(self) -> None:  # AU-01
        self.helper.xml = ABOUT_XML
        self.helper.replies["read"] = {"settled": False, "settle_ms": 2000}
        runner, result = self.run_flow(
            "- assertVisible:\n    selector:\n      text: Android version\n")
        self.assertEqual(result.status, "passed")
        [check] = self.finished(runner)
        self.assertIn("screen", check)
        self.assertEqual((check["settled"], check["settle_ms"]), (False, 2000))

    def test_a_step_event_says_when_its_helper_read_settled(self) -> None:
        runner, result = self.run_flow("- assertVisible:\n    selector:\n      text: Apps\n")
        self.assertEqual(result.status, "passed")
        [check] = self.finished(runner)
        self.assertEqual((check["settled"], check["settle_ms"]), (True, 20))

    def test_a_wait_for_settled_step_keeps_its_own_settle(self) -> None:
        """Its `settle` block speaks for its reads: no `settle_ms` of the
        last helper read beside it."""
        self.helper.replies["read"] = {"settled": False, "settle_ms": 2000}
        runner, result = self.run_flow("- waitForSettled:\n    timeoutMs: 1000\n"
                                       "    quietMs: 0\n")
        self.assertEqual(result.status, "passed")
        [step] = self.finished(runner)
        self.assertIn("settle", step)
        self.assertIs(step["settled"], step["settle"]["settled"])
        self.assertNotIn("settle_ms", step)

    def test_a_step_read_through_uiautomator_carries_no_settle(self) -> None:  # ODA-001
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        runner, result = self.run_flow("- assertVisible:\n    selector:\n      text: Apps\n")
        self.assertEqual(result.status, "passed")
        [check] = self.finished(runner)
        self.assertIn("screen", check)
        for key in ("settled", "settle_ms", "ui_backend", "fallback_reason"):
            self.assertNotIn(key, check)

    def test_a_zero_length_swipe_step_is_sent_through_the_helper(self) -> None:
        runner, result = self.run_flow("- swipe:\n    direction: up\n    durationMs: 0\n")
        self.assertEqual(result.status, "passed")
        [swipe] = self.helper.ops("swipe")
        self.assertEqual(swipe["duration_ms"], 1)
        self.assertEqual(self.inputs(), [])

    def test_an_unacknowledged_input_fails_the_step_as_infrastructure_once(self) -> None:
        self.helper.drop_after = {"tap": 1}
        with self.assertRaises(errors.AutonomError) as caught:
            self.run_flow("- tapOn:\n    selector:\n      text: Apps\n")
        self.assertEqual(caught.exception.code, errors.UI_ACTION_UNCERTAIN)
        self.assertEqual(caught.exception.extra["failure_class"], "infrastructure")
        self.assertEqual(len(self.helper.ops("tap")), 1, "never re-sent")
        self.assertEqual(self.inputs(), [])


# --- the whole path through a CLI process: build on first use, then reads -----------------

def tool_wrapper(path: Path, tool: str) -> None:
    """An executable `tool` that runs the fake toolchain in that role."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n"
                    'AUTONOM_FAKE_TOOL_PATH="$0"; export AUTONOM_FAKE_TOOL_PATH\n'
                    f'exec "{sys.executable}" -B "{FAKE_TOOLS}" {tool} "$@"\n', encoding="utf-8")
    path.chmod(0o755)


class CliProcessTests(BackendCase):
    """`ui tree` and an observed `ui tap` in their own CLI processes, auto mode,
    with a fake JDK and SDK: the first call builds the helper (fake javac and
    d8), pushes and starts it on the fake device, and every read and input
    after that goes to the fake helper."""

    def setUp(self) -> None:
        super().setUp()
        self.build.stop()  # the real build module, with the fake toolchain
        self.addCleanup(self.build.start)  # undone before setUp's own stop
        jdk, sdk = self.root / "jdk", self.root / "sdk"
        for tool in ("javac", "java"):
            tool_wrapper(jdk / "bin" / tool, tool)
        (jdk / "release").write_text('JAVA_VERSION="21.0.0"\n', encoding="utf-8")
        tool_wrapper(sdk / "build-tools/36.0.0/d8", "d8")
        jar = sdk / "platforms/android-36/android.jar"
        jar.parent.mkdir(parents=True)
        jar.write_bytes(b"PK fake android-36")
        self.tools_log = self.root / "tools.jsonl"
        self.env = {**os.environ, "JAVA_HOME": str(jdk), "ANDROID_HOME": str(sdk),
                    "AUTONOM_FAKE_TOOLS_LOG": str(self.tools_log)}

    def run_cli(self, *argv: str) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL, *argv],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        return completed.returncode, json.loads(stream)

    def compiles(self) -> list[str]:
        if not self.tools_log.exists():
            return []
        rows = [json.loads(line) for line in self.tools_log.read_text().splitlines()]
        return [row["tool"] for row in rows
                if row["argv"] not in (["-version"], ["--version"])]

    def test_first_use_builds_then_every_call_goes_to_the_helper(self) -> None:
        code, payload = self.run_cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["ui_backend"], payload["settled"]), ("helper", True))
        self.assertNotIn("fallback_reason", payload)
        self.assertEqual(self.compiles(), ["javac", "d8"], "built once, on first use")
        [start] = self.device()["helper_starts"]
        built = list((self.home / "android-helper/builds").glob("*/classes.dex"))
        self.assertEqual([path.parent.name for path in built], [start["build"]])
        self.assertNotIn(DUMP, self.calls())
        self.assertEqual(self.size_calls(), [])

        self.helper.xml_after = NETWORK_XML
        self.clear_calls()
        code, payload = self.run_cli("ui", "tap", "--text", "Network & internet", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["backend"], payload["input_backend"], payload["settled"]),
                         ("helper", "helper", True))
        self.assertIn('"Airplane mode"', payload["screen"]["outline"])
        self.assertEqual(self.compiles(), ["javac", "d8"], "the cached build is reused")
        self.assertEqual(len(self.device()["helper_starts"]), 1, "the running helper is reused")
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.size_calls(), [])
        self.assertNotIn(DUMP, self.calls())
        self.assertEqual([item["op"] for item in self.ops()][-2:], ["read", "tap"])

    def test_a_real_interrupt_while_the_helper_settles_keeps_the_action(self) -> None:  # AL-11
        """A host's SIGINT 0.x s after the observed tap reached the helper,
        while the helper settles: exit 130, the reply on stdout says the tap
        ran (observe_error interrupted), the journal records it ok, and the
        helper got exactly one tap."""
        code, payload = self.run_cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(code, 0, payload)
        session = payload["session"]
        code, payload = self.run_cli("ui", "tree")  # builds and starts the helper
        self.assertEqual((code, payload.get("ui_backend")), (0, "helper"), payload)
        self.helper.slow = {"tap": 3.0}  # the settle after the tap
        process = subprocess.Popen(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL,
             "ui", "tap", "--x", "540", "--y", "700", "--observe"],
            cwd=self.root, env=self.env, text=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 60
        while not self.helper.ops("tap") and process.poll() is None \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.helper.ops("tap")), 1, "the tap reached the helper")
        time.sleep(0.3)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=60)
        self.assertNotIn("Traceback", stdout + stderr)
        self.assertEqual(process.returncode, 130, (stdout, stderr))
        [line] = stdout.splitlines()
        reply = json.loads(line)
        self.assert_observe_interrupted(reply, "ui tap")
        self.assertEqual(reply["input_backend"], "helper")
        self.assertEqual(stderr.strip(), "", "the reply, not the exit-130 envelope")
        self.assertEqual(len(self.helper.ops("tap")), 1, "never sent again")
        self.assertEqual(self.inputs(), [])
        [entry] = self.journal(session, "ui tap")
        self.assertTrue(entry["ok"], entry)


if __name__ == "__main__":
    unittest.main()
