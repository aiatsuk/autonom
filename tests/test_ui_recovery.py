"""Regressions from Reader: unusable AX geometry, fallback, and session isolation."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from autonom_lib import errors, ios_geometry, ios_xctest, providers, session, ui, ui_ios
from autonom_lib.platform import Target
from test_journal import CLI, JournalBase

TARGET = Target("ios", "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB", "/usr/bin/xcrun")
ZERO = [{"type": "Application", "frame": {"x": 0, "y": 0, "width": 0, "height": 0}}]


class GeometryRecoveryTests(unittest.TestCase):
    def test_zero_root_is_not_a_screen(self):
        self.assertIsNone(ui_ios.screen_size_from(ZERO))

    def test_zero_root_does_not_hide_a_valid_window(self):
        payload = ZERO + [{"type": "Window", "frame": {"width": 402, "height": 874}}]
        self.assertEqual(ui_ios.screen_size_from(payload), (402, 874))

    def test_partial_child_extent_is_not_treated_as_display_size(self):
        self.assertIsNone(ui_ios.screen_size_from([
            {"type": "Button", "frame": {"x": 20, "y": 30, "width": 20, "height": 40}}]))

    def test_unknown_geometry_refuses_xcuitest_input(self):
        with patch.object(ui, "screen_size", return_value=None), \
                patch.object(ui_ios, "selected_backend", return_value="xcuitest"), \
                patch.object(ui_ios, "tap") as tap:
            with self.assertRaises(errors.AutonomError) as caught:
                ui.tap(TARGET, 200, 278)
            self.assertEqual(caught.exception.code, "display_geometry_unavailable")
            tap.assert_not_called()

    def _measure_with(self, plists: dict, png=(2622, 1206)):
        """measure() against a fake device type bundle holding `plists`."""
        with tempfile.TemporaryDirectory() as tmp:
            resources = Path(tmp) / "Contents/Resources"
            resources.mkdir(parents=True)
            for name, content in plists.items():
                (resources / name).write_bytes(plistlib.dumps(content))
            devices = {"devices": {"ios": [{"udid": TARGET.target_id, "state": "Booted",
                                            "deviceTypeIdentifier": "phone"}]}}
            types = {"devicetypes": [{"identifier": "phone", "bundlePath": tmp}]}
            outputs = [subprocess.CompletedProcess([], 0, json.dumps(x)) for x in (devices, types)]
            with patch.object(ios_geometry.ios_simctl, "run_simctl", side_effect=outputs), \
                    patch.object(ios_geometry.ios_simctl, "screenshot"), \
                    patch.object(ios_geometry.screenshot, "png_size", return_value=png):
                return ios_geometry.measure(TARGET)

    def test_profile_scale_uses_current_png_orientation(self):
        measured = self._measure_with({"profile.plist": {
            "mainScreenScale": 3, "mainScreenWidth": 1206, "mainScreenHeight": 2622}})
        self.assertEqual((measured["width"], measured["height"]), (874, 402))
        self.assertEqual(measured["orientation"], "landscape")

    # Xcode 27 layout: profile.plist has no mainScreen* keys; the screen lives
    # in capabilities.plist under ScreenDimensionsCapability.
    XCODE27_CAPABILITIES = {"capabilities": {"ScreenDimensionsCapability": {
        "main-screen-class": 36, "main-screen-height": 2622, "main-screen-orientation": 0,
        "main-screen-pitch": 460, "main-screen-scale": 3, "main-screen-width": 1206}}}
    XCODE27_PROFILE = {"modelIdentifier": "iPhone18,3", "productClass": "V57",
                       "minRuntimeVersion": "26.0"}

    def test_xcode27_capabilities_screen_dimensions(self):
        measured = self._measure_with({"capabilities.plist": self.XCODE27_CAPABILITIES,
                                       "profile.plist": self.XCODE27_PROFILE}, png=(1206, 2622))
        self.assertEqual((measured["width"], measured["height"], measured["scale"]),
                         (402, 874, 3.0))
        self.assertEqual(measured["orientation"], "portrait")

    def test_capabilities_win_over_stale_profile_keys(self):
        stale = dict(self.XCODE27_PROFILE, mainScreenScale=2, mainScreenWidth=750,
                     mainScreenHeight=1334)
        measured = self._measure_with({"capabilities.plist": self.XCODE27_CAPABILITIES,
                                       "profile.plist": stale}, png=(1206, 2622))
        self.assertEqual((measured["width"], measured["height"]), (402, 874))

    def test_integrated_display_entry_for_older_device_types(self):
        capabilities = {"capabilities": {"displays": [
            {"displayType": "integrated", "scale": 2, "width": 750, "height": 1334},
            {"displayType": "tvOut", "scale": 1, "width": 720, "height": 480}]}}
        measured = self._measure_with({"capabilities.plist": capabilities,
                                       "profile.plist": self.XCODE27_PROFILE}, png=(750, 1334))
        self.assertEqual((measured["width"], measured["height"]), (375, 667))

    def test_no_screen_record_anywhere_is_unknown(self):
        self.assertIsNone(self._measure_with({"profile.plist": self.XCODE27_PROFILE}))


class UnknownGeometryInputTests(unittest.TestCase):
    """No measurable screen: idb and AXe still send a coordinate tap, as on main
    (Canvas taps call ui.tap with no tree read); only the XCUITest route refuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": self.tmp.name})
        self.env.start()
        for name in ("AUTONOM_IOS_HID", "AUTONOM_AXE", ui_ios.UI_BACKEND_ENV):
            os.environ.pop(name, None)
        self.token = session.select(None)
        ui_ios.set_preference(None)
        ui_ios.reset_hid_probe()
        self.zero_tree = patch.object(ui_ios.ios_idb, "describe_all", return_value=json.dumps(ZERO))
        self.no_measure = patch.object(ios_geometry, "measure", return_value=None)
        self.zero_tree.start()
        self.no_measure.start()

    def tearDown(self):
        self.no_measure.stop()
        self.zero_tree.stop()
        ui_ios.set_preference(None)
        ui_ios.reset_hid_probe()
        session._SELECTED.reset(self.token)
        self.env.stop()
        self.tmp.cleanup()

    def test_canvas_tap_without_a_session_goes_through_idb(self):
        os.environ["AUTONOM_IOS_HID"] = "idb"
        self.assertIsNone(ui.screen_size(TARGET))
        with patch.object(ui_ios.ios_idb, "tap") as idb_tap:
            self.assertEqual(ui.tap(TARGET, 200, 278), "idb")
        idb_tap.assert_called_once_with(TARGET, 200, 278, duration=None)

    def test_idb_backend_session_without_app_id_still_taps(self):
        session.start_session(TARGET.tool, platform="ios", target_id=TARGET.target_id)
        ui_ios.set_preference("idb")
        os.environ["AUTONOM_IOS_HID"] = "idb"
        with patch.object(ui_ios.ios_idb, "tap") as idb_tap, \
                patch.object(ui_ios.ios_idb, "swipe") as idb_swipe, \
                patch.object(ios_xctest, "request") as runner:
            self.assertEqual(ui.tap(TARGET, 200, 278), "idb")
            self.assertEqual(ui.swipe(TARGET, 10, 20, 30, 40, 0.3), "idb")
        idb_tap.assert_called_once_with(TARGET, 200, 278, duration=None)
        idb_swipe.assert_called_once_with(TARGET, 10, 20, 30, 40, 0.3)
        runner.assert_not_called()

    def test_axe_route_still_taps(self):
        os.environ["AUTONOM_IOS_HID"] = "axe"
        os.environ["AUTONOM_AXE"] = "/fake/axe"
        with patch.object(ui_ios, "run_axe") as run_axe:
            self.assertEqual(ui.tap(TARGET, 200, 278), "axe")
        run_axe.assert_called_once()
        self.assertEqual(run_axe.call_args.args[:2], ("/fake/axe", ["tap", "-x", "200", "-y", "278"]))

    def test_xcuitest_route_refuses_and_sends_nothing(self):
        ui_ios.set_preference("xcuitest")
        verbs = []

        def runner(target, verb, **values):
            verbs.append(verb)
            return {"screen": {}}

        with patch.object(ios_xctest, "request", side_effect=runner), \
                patch.object(ui_ios.ios_idb, "tap") as idb_tap:
            with self.assertRaises(errors.AutonomError) as caught:
                ui.tap(TARGET, 200, 278)
        self.assertEqual(caught.exception.code, errors.DISPLAY_GEOMETRY_UNAVAILABLE)
        self.assertEqual(verbs, ["geometry"])
        idb_tap.assert_not_called()


class BackendRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": self.tmp.name})
        self.env.start()
        self.token = session.select(None)
        session.start_session(TARGET.tool, platform="ios", target_id=TARGET.target_id, app_id="test.reader")
        ui_ios.set_preference("auto")

    def tearDown(self):
        ui_ios.set_preference(None)
        session._SELECTED.reset(self.token)
        self.env.stop()
        self.tmp.cleanup()

    def test_empty_idb_tree_uses_xctest_and_records_reason(self):
        result = {"nodes": [{"type": "Button", "AXLabel": "Save", "identifier": "save",
                             "frame": {"width": 44, "height": 44}, "xcuitest_ref": "r1"}],
                  "screen": {"width": 402, "height": 874}}
        with patch.object(ui_ios.ios_idb, "describe_all", return_value=json.dumps(ZERO)), \
                patch.object(ios_xctest, "available", return_value=True), \
                patch.object(ios_xctest, "request", return_value=result) as request:
            nodes = ui.snapshot(TARGET)
        self.assertEqual(nodes[0]["resource_id"], "save")
        self.assertEqual(nodes[0]["xcuitest_ref"], "r1")
        self.assertEqual(session.load_current()["ui_observation"]["fallback_reason"], "empty_accessibility_tree")
        request.assert_called_once_with(TARGET, "snapshot")

    def test_an_uncertain_xctest_tap_never_falls_back_to_idb(self):
        ui_ios.set_preference("xcuitest")
        with patch.object(ios_xctest, "request", side_effect=errors.AutonomError("ui_action_uncertain", "lost response")) as request, \
                patch.object(ui_ios.ios_idb, "tap") as idb:
            with self.assertRaises(errors.AutonomError):
                ui_ios.tap(TARGET, 20, 20)
        self.assertEqual(request.call_count, 1)
        idb.assert_not_called()

    def test_auto_with_a_persisted_runner_observation_never_retaps_through_idb(self):
        # REV-003: `auto` in a fresh invocation follows the session's persisted
        # ui_observation (the runner served it); an unanswered tap is uncertain,
        # sent to the runner once, and never dispatched through idb or AXe.
        record = session.load_current()
        record["ui_observation"] = {"ui_backend": "xcuitest", "observed_at": time.time(),
                                    "fallback_reason": "empty_accessibility_tree"}
        session.save(record)
        ui_ios.set_preference("auto")  # a new invocation: no in-memory observation
        mailbox = Path(self.tmp.name) / "mailbox"
        mailbox.mkdir()
        state = {"token": "owner", "mailbox": str(mailbox)}
        with patch.object(ios_xctest, "_ensure", return_value=state), \
                patch.object(ios_xctest, "REQUEST_TIMEOUT", 0), \
                patch.object(ios_xctest, "_exchange", wraps=ios_xctest._exchange) as exchange, \
                patch.object(ios_xctest, "_write", wraps=ios_xctest._write) as write, \
                patch.object(ui_ios.ios_idb, "tap") as idb_tap, \
                patch.object(ui_ios, "_dispatch") as dispatch, \
                patch.object(ui_ios, "run_axe") as axe:
            self.assertEqual(ui_ios.preference(TARGET), "auto")
            with self.assertRaises(errors.AutonomError) as caught:
                ui_ios.tap(TARGET, 20, 20)
        self.assertEqual(caught.exception.code, errors.UI_ACTION_UNCERTAIN)
        self.assertEqual(exchange.call_count, 1)
        self.assertEqual(exchange.call_args.args[1], "tap")
        requests = [c for c in write.call_args_list if c.args[0].name == "request.json"]
        self.assertEqual(len(requests), 1)
        idb_tap.assert_not_called()
        dispatch.assert_not_called()
        axe.assert_not_called()

    def test_timeout_submits_a_mutation_once_and_removes_unconsumed_text(self):
        mailbox = Path(self.tmp.name) / "mailbox"
        mailbox.mkdir()
        state = {"token": "owner", "mailbox": str(mailbox)}
        with patch.object(ios_xctest, "_ensure", return_value=state), \
                patch.object(ios_xctest.time, "monotonic", side_effect=[0, 31]), \
                patch.object(ios_xctest, "_write", wraps=ios_xctest._write) as write:
            with self.assertRaises(errors.AutonomError) as caught:
                ios_xctest.request(TARGET, "type", text="private input")
        self.assertEqual(caught.exception.code, "ui_action_uncertain")
        self.assertEqual(write.call_count, 1)
        self.assertFalse((mailbox / "request.json").exists())

    def test_installed_tooling_stays_the_baseline_until_measured(self):
        record = session.load_current()
        record["tooling"]["idb"] = {"state": "ready", "hid": {"ready": True}}
        snapshot = providers.open_session(TARGET, record).capabilities()
        states = {item.name: item.state for item in snapshot.capabilities}
        self.assertEqual(states["ui.accessibility"], "available")
        self.assertEqual(states["ui.input"], "available")

    def test_a_recent_degraded_read_overrides_installed_tooling(self):
        ui_ios._record(TARGET, "idb", meaningful_nodes=0, accessibility="degraded",
                       fallback_reason="empty_accessibility_tree")
        record = session.load_current()
        record["tooling"]["idb"] = {"state": "ready", "hid": {"ready": True}}
        caps = {item.name: item for item in providers.open_session(TARGET, record).capabilities().capabilities}
        self.assertEqual(caps["ui.accessibility"].state, "degraded")
        self.assertEqual(caps["ui.accessibility"].reason, "empty_accessibility_tree")
        # idb input is still judged by its tooling, as before
        self.assertEqual(caps["ui.input"].state, "available")

    def test_new_snapshot_preserves_reason_but_does_not_refresh_old_input(self):
        with patch.object(ui_ios.time, "time", return_value=100):
            ui_ios._record(TARGET, "xcuitest", accessibility="available",
                           fallback_reason="empty_accessibility_tree", input_verified=True)
        ui_ios.set_preference("auto")  # Next CLI invocation has no in-memory observations.
        with patch.object(ui_ios.time, "time", return_value=200):
            ui_ios._record(TARGET, "xcuitest", accessibility="available")
            record = session.load_current()
            states = {item.name: item.state for item in providers.open_session(TARGET, record).capabilities().capabilities}
        self.assertEqual(record["ui_observation"]["fallback_reason"], "empty_accessibility_tree")
        self.assertEqual(record["ui_observation"]["input_at"], 100)
        self.assertEqual(states["ui.accessibility"], "available")
        self.assertEqual(states["ui.input"], "degraded")

    def test_live_bridge_refuses_another_session_before_build_or_dispatch(self):
        with patch.object(ios_xctest, "_read", return_value={"session_id": "s_other"}), \
                patch.object(ios_xctest, "_alive", return_value=True), \
                patch.object(ios_xctest, "_build") as build:
            with self.assertRaises(errors.AutonomError) as caught:
                ios_xctest._ensure(TARGET, Path(self.tmp.name))
        self.assertEqual(caught.exception.code, "ui_bridge_in_use")
        build.assert_not_called()


class BackendCompositionTests(unittest.TestCase):
    """XCUITest sits above main's idb/AXe input routing (DEC-002)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": self.tmp.name})
        self.env.start()
        self.token = session.select(None)
        session.start_session(TARGET.tool, platform="ios", target_id=TARGET.target_id, app_id="test.reader")
        ui_ios.set_preference(None)
        ui_ios.reset_hid_probe()

    def tearDown(self):
        ui_ios.set_preference(None)
        ui_ios.reset_hid_probe()
        session._SELECTED.reset(self.token)
        self.env.stop()
        self.tmp.cleanup()

    def test_idb_session_keeps_the_hid_route_and_reports_it(self):
        with patch.object(ui_ios, "_dispatch", return_value="axe") as dispatch, \
                patch.object(ios_xctest, "request") as request:
            self.assertEqual(ui.tap(TARGET, 10, 20, screen=(402, 874)), "axe")
        dispatch.assert_called_once()
        request.assert_not_called()
        self.assertEqual(session.load_current()["ui_observation"]["input_backend"], "axe")

    def test_xcuitest_session_never_consults_the_hid_route(self):
        ui_ios.set_preference("xcuitest")
        node = {"xcuitest_ref": "r1", "resource_id": "save", "desc": "Save"}
        with patch.object(ui_ios, "_dispatch") as dispatch, \
                patch.object(ios_xctest, "request", return_value={"dispatched": True}) as request:
            self.assertEqual(ui.tap(TARGET, 10, 20, screen=(402, 874), node=node), "xcuitest")
        dispatch.assert_not_called()
        request.assert_called_once_with(TARGET, "tap", x=10, y=20, ref="r1",
                                        identifier="save", label="Save")
        self.assertEqual(ui_ios.last_backend(), "xcuitest")

    def test_xcuitest_long_press_sends_duration_not_a_reference(self):
        ui_ios.set_preference("xcuitest")
        with patch.object(ios_xctest, "request", return_value={}) as request:
            self.assertEqual(ui.long_press(TARGET, 10, 20, 800, screen=(402, 874)), "xcuitest")
        request.assert_called_once_with(TARGET, "tap", x=10, y=20, duration=0.8)

    def test_text_through_the_runner_is_not_journaled_in_the_observation(self):
        ui_ios.set_preference("xcuitest")
        with patch.object(ios_xctest, "request", return_value={}):
            self.assertEqual(ui_ios.type_text(TARGET, "private input"), "xcuitest")
        stored = json.dumps(session.load_current())
        self.assertNotIn("private input", stored)

    def test_forced_idb_raises_the_idb_failure_without_fallback(self):
        ui_ios.set_preference("idb")
        failure = errors.AutonomError(errors.BACKEND_FAILED, "describe-all failed")
        with patch.object(ui_ios.ios_idb, "describe_all", side_effect=failure), \
                patch.object(ios_xctest, "request") as request:
            with self.assertRaises(errors.AutonomError) as caught:
                ui_ios.describe_all(TARGET)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        request.assert_not_called()

    def test_no_fallback_without_an_app_session(self):
        record = session.load_current()
        record["app_id"] = None
        session.save(record)
        with patch.object(ui_ios.ios_idb, "describe_all", return_value=json.dumps(ZERO)), \
                patch.object(ios_xctest, "available", return_value=True), \
                patch.object(ios_xctest, "request") as request:
            payload = ui_ios.describe_all(TARGET)
        self.assertEqual(json.loads(payload), ZERO)
        request.assert_not_called()
        observed = session.load_current()["ui_observation"]
        self.assertEqual(observed["accessibility"], "degraded")
        self.assertEqual(observed["fallback_reason"], "empty_accessibility_tree")

    def test_an_invalid_backend_is_a_typed_error(self):
        ui_ios.set_preference("webdriver")
        with self.assertRaises(errors.AutonomError) as caught:
            ui_ios.preference(TARGET)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)

    def test_new_error_codes_are_declared(self):
        for name in ("SESSION_STOPPED", "SESSION_TARGET_MISMATCH", "DISPLAY_GEOMETRY_UNAVAILABLE",
                     "UI_ACTION_UNCERTAIN", "APP_ID_REQUIRED", "UI_BRIDGE_IN_USE",
                     "XCUITEST_UNAVAILABLE", "XCUITEST_BUILD_FAILED", "XCUITEST_START_FAILED",
                     "XCUITEST_TIMEOUT", "XCUITEST_FAILED", "STALE_UI_ELEMENT"):
            self.assertEqual(getattr(errors, name), name.lower())


class RunnerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": self.tmp.name})
        self.env.start()
        self.token = session.select(None)
        self.record = session.start_session(TARGET.tool, platform="ios",
                                            target_id=TARGET.target_id, app_id="test.reader")

    def tearDown(self):
        session._SELECTED.reset(self.token)
        self.env.stop()
        self.tmp.cleanup()

    def _state(self, **extra):
        directory = ios_xctest.state_dir(TARGET)
        directory.mkdir(parents=True, exist_ok=True)
        state = {"pid": 4242, "token": "t", "mailbox": str(directory / "mailbox"),
                 "session_id": self.record["session_id"], **extra}
        ios_xctest._write(directory / "state.json", state)
        return directory

    def test_stop_asks_once_and_drops_the_row_when_the_runner_exits(self):
        directory = self._state()
        with patch.object(ios_xctest, "_alive", return_value=True), \
                patch.object(ios_xctest, "_pid_alive", return_value=False), \
                patch.object(ios_xctest, "_exchange", return_value={}) as exchange, \
                patch.object(ios_xctest.processes, "deregister") as deregister:
            self.assertTrue(ios_xctest.stop(TARGET))
        exchange.assert_called_once()
        self.assertEqual(exchange.call_args.args[0]["token"], "t")
        self.assertEqual(exchange.call_args.args[1], "stop")
        deregister.assert_called_once_with(4242)
        self.assertFalse((directory / "state.json").exists())

    def test_an_unanswering_runner_is_ended_through_its_registry_row(self):
        self._state()
        alive = iter([True, True] + [False] * 5)  # waiting, before the kill, then gone
        entry = {"pid": 4242, "kind": "xcuitest", "signature": ["xcodebuild"]}
        with patch.object(ios_xctest, "_alive", return_value=True), \
                patch.object(ios_xctest, "_pid_alive", side_effect=lambda pid: next(alive)), \
                patch.object(ios_xctest, "STOP_TIMEOUT", 0), \
                patch.object(ios_xctest, "_exchange",
                             side_effect=errors.AutonomError(errors.XCUITEST_TIMEOUT, "late")), \
                patch.object(ios_xctest.processes, "entries", return_value=[entry]), \
                patch.object(ios_xctest.processes, "terminate_entry",
                             return_value="terminated") as terminate, \
                patch.object(ios_xctest.processes, "deregister") as deregister:
            self.assertTrue(ios_xctest.stop(TARGET))
        terminate.assert_called_once_with(entry)
        deregister.assert_called_once_with(4242)

    def test_stop_leaves_another_sessions_runner_alone(self):
        directory = self._state(session_id="s_other")
        with patch.object(ios_xctest, "request") as request:
            self.assertFalse(ios_xctest.stop(TARGET))
        request.assert_not_called()
        self.assertTrue((directory / "state.json").exists())

    def test_a_stop_while_waiting_for_the_lock_starts_no_runner(self):
        session.select(self.record["session_id"])  # bound with --session-id
        real_lock = ios_xctest.lock

        @contextlib.contextmanager
        def stop_then_lock(path):
            # `session stop` lands in another terminal while this one waits.
            session.stop_session(reap=False)
            with real_lock(path):
                yield
        with patch.object(ios_xctest, "lock", side_effect=stop_then_lock), \
                patch.object(ios_xctest, "_build") as build, \
                patch.object(ios_xctest.subprocess, "Popen") as popen, \
                patch.object(ios_xctest.processes, "register") as register:
            with self.assertRaises(errors.AutonomError) as caught:
                ios_xctest.request(TARGET, "tap", x=10, y=20)
        self.assertEqual(caught.exception.code, errors.SESSION_STOPPED)
        build.assert_not_called()
        popen.assert_not_called()
        register.assert_not_called()

    def test_a_late_read_is_a_timeout_not_an_uncertain_action(self):
        mailbox = Path(self.tmp.name) / "mailbox"
        mailbox.mkdir()
        with patch.object(ios_xctest, "_ensure", return_value={"token": "owner", "mailbox": str(mailbox)}), \
                patch.object(ios_xctest.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_xctest.request(TARGET, "snapshot")
        self.assertEqual(caught.exception.code, errors.XCUITEST_TIMEOUT)

    def _bundles(self, directory, *tokens):
        for token in tokens:
            (directory / f"run-{token}.xcresult" / "Data").mkdir(parents=True)
        return directory

    def test_stop_removes_this_and_earlier_result_bundles(self):
        directory = self._bundles(self._state(), "t", "old")
        with patch.object(ios_xctest, "_alive", return_value=False), \
                patch.object(ios_xctest, "_pid_alive", return_value=False), \
                patch.object(ios_xctest.processes, "deregister"):
            ios_xctest.stop(TARGET)
        self.assertEqual(list(directory.glob("run-*.xcresult")), [])

    def test_stop_removes_the_runner_log_once_the_runner_exited(self):
        # The XCTest console logs typeText activity with the typed text.
        directory = self._state()
        (directory / "runner.log").write_text("Type 'secret' into TextField")
        with patch.object(ios_xctest, "_alive", return_value=False), \
                patch.object(ios_xctest, "_pid_alive", return_value=False), \
                patch.object(ios_xctest.processes, "deregister"):
            ios_xctest.stop(TARGET)
        self.assertFalse((directory / "runner.log").exists())

    def test_stop_keeps_the_runner_log_while_its_runner_runs(self):
        directory = self._state()
        (directory / "runner.log").write_text("console")
        entry = {"pid": 4242, "kind": "xcuitest", "signature": ["xcodebuild"]}
        with patch.object(ios_xctest, "_alive", return_value=False), \
                patch.object(ios_xctest, "_pid_alive", return_value=True), \
                patch.object(ios_xctest, "STOP_TIMEOUT", 0), \
                patch.object(ios_xctest.processes, "entries", return_value=[entry]), \
                patch.object(ios_xctest.processes, "terminate_entry", return_value="unverified"):
            self.assertFalse(ios_xctest.stop(TARGET))
        self.assertTrue((directory / "runner.log").exists())

    def test_stop_never_builds_or_starts_a_runner(self):
        # The runner ends (a concurrent stop, or the idle timeout) between the
        # first liveness check and the request lock: nothing is sent or built.
        self._state()
        with patch.object(ios_xctest, "_alive", side_effect=[True, False]), \
                patch.object(ios_xctest, "_pid_alive", return_value=False), \
                patch.object(ios_xctest, "_build") as build, \
                patch.object(ios_xctest.subprocess, "Popen") as popen, \
                patch.object(ios_xctest, "_exchange") as exchange, \
                patch.object(ios_xctest.processes, "register") as register, \
                patch.object(ios_xctest.processes, "deregister"):
            ios_xctest.stop(TARGET)
        build.assert_not_called()
        popen.assert_not_called()
        register.assert_not_called()
        exchange.assert_not_called()

    def test_stop_of_a_dead_runner_never_builds_or_starts_one(self):
        self._state()
        with patch.object(ios_xctest, "_alive", return_value=False), \
                patch.object(ios_xctest, "_pid_alive", return_value=False), \
                patch.object(ios_xctest, "_build") as build, \
                patch.object(ios_xctest.subprocess, "Popen") as popen, \
                patch.object(ios_xctest.processes, "deregister"):
            ios_xctest.stop(TARGET)
        build.assert_not_called()
        popen.assert_not_called()

    def test_stop_skips_a_runner_replaced_before_the_lock(self):
        # Another runner took over state.json: this stop never talks to it.
        directory = self._state()
        original = ios_xctest._read

        def read(path):
            value = original(path)
            if path == directory / "state.json" and read.calls:
                value = {**value, "token": "replacement"}
            read.calls += 1
            return value
        read.calls = 0
        with patch.object(ios_xctest, "_alive", return_value=True), \
                patch.object(ios_xctest, "_read", side_effect=read), \
                patch.object(ios_xctest, "_pid_alive", return_value=False), \
                patch.object(ios_xctest, "_build") as build, \
                patch.object(ios_xctest, "_exchange") as exchange, \
                patch.object(ios_xctest.processes, "deregister"):
            ios_xctest.stop(TARGET)
        build.assert_not_called()
        exchange.assert_not_called()

    def test_stop_cleanup_holds_the_lock_and_spares_a_newer_runner(self):
        import fcntl
        directory = self._bundles(self._state(), "t", "old")
        (directory / "runner.log").write_text("console of the newer runner")

        def newer_runner_starts(*_args, **_kwargs):
            # While stop waits, a concurrent request() starts a new runner.
            self._bundles(directory, "new")
            ios_xctest._write(directory / "state.json",
                              {"pid": 5151, "token": "new", "mailbox": str(directory / "mailbox"),
                               "session_id": self.record["session_id"]})
            return {}

        held = []
        remove = ios_xctest._remove_bundles

        def remove_bundles(*args, **kwargs):
            with (directory / "request.lock").open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    held.append(False)
            return remove(*args, **kwargs)

        with patch.object(ios_xctest, "_alive", return_value=True), \
                patch.object(ios_xctest, "_pid_alive", side_effect=lambda pid: int(pid) == 5151), \
                patch.object(ios_xctest, "_exchange", side_effect=newer_runner_starts), \
                patch.object(ios_xctest, "_remove_bundles", side_effect=remove_bundles), \
                patch.object(ios_xctest.processes, "deregister") as deregister:
            self.assertTrue(ios_xctest.stop(TARGET))
        deregister.assert_called_once_with(4242)
        self.assertEqual(held, [True])
        self.assertEqual([path.name for path in directory.glob("run-*.xcresult")],
                         ["run-new.xcresult"])
        self.assertEqual(ios_xctest._read(directory / "state.json")["token"], "new")
        self.assertTrue((directory / "runner.log").exists())

    def test_stop_without_a_runner_removes_a_failed_starts_bundle(self):
        directory = self._bundles(ios_xctest.state_dir(TARGET), "failed")
        (directory / "runner.log").write_text("console")
        self.assertFalse(ios_xctest.stop(TARGET))
        self.assertEqual(list(directory.glob("run-*.xcresult")), [])
        self.assertFalse((directory / "runner.log").exists())

    def test_stop_never_creates_runner_state(self):
        self.assertFalse(ios_xctest.stop(TARGET))
        self.assertFalse(ios_xctest.state_dir(TARGET, create=False).exists())

    def test_a_new_runner_first_removes_bundles_of_one_that_ended(self):
        directory = self._bundles(self._state(token="idle"), "idle", "older")
        with patch.object(ios_xctest, "_alive", return_value=False), \
                patch.object(ios_xctest, "_pid_alive", return_value=False), \
                patch.object(ios_xctest, "_build",
                             side_effect=errors.AutonomError(errors.XCUITEST_UNAVAILABLE, "stop here")):
            with self.assertRaises(errors.AutonomError):
                ios_xctest._ensure(TARGET, directory)
        self.assertEqual(list(directory.glob("run-*.xcresult")), [])

    def test_a_runner_that_never_gets_ready_leaves_no_bundle(self):
        directory = ios_xctest.state_dir(TARGET)
        template = Path(self.tmp.name) / "Build" / "Products" / "runner.xctestrun"
        template.parent.mkdir(parents=True)
        template.write_bytes(plistlib.dumps({"AutonomUITests": {}}))

        def popen(argv, stdout=None, **_):
            Path(argv[argv.index("-resultBundlePath") + 1], "Data").mkdir(parents=True)
            stdout.write("Type 'secret' into TextField\n")
            child = Mock(pid=999999)
            child.poll.return_value = 65
            return child

        with patch.object(ios_xctest, "_build", return_value=template), \
                patch.object(ios_xctest.subprocess, "Popen", side_effect=popen), \
                patch.object(ios_xctest.processes, "register"), \
                patch.object(ios_xctest.processes, "deregister"):
            with self.assertRaises(errors.AutonomError) as caught:
                ios_xctest._ensure(TARGET, directory)
        self.assertEqual(caught.exception.code, errors.XCUITEST_START_FAILED)
        self.assertEqual(list(directory.glob("run-*.xcresult")), [])
        self.assertFalse((directory / "runner.log").exists())
        self.assertNotIn("log", caught.exception.extra)

    def test_another_simulator_never_claims_or_stops_the_sessions_runner(self):
        other = Target("ios", "CCCCCCCC-1111-2222-3333-DDDDDDDDDDDD", "/usr/bin/xcrun")
        self.assertEqual(ios_xctest._owner(TARGET)["session_id"], self.record["session_id"])
        self.assertEqual(ios_xctest._owner(other), {})
        directory = ios_xctest.state_dir(other)
        ios_xctest._write(directory / "state.json",
                          {"pid": 4242, "token": "t", "session_id": "s_other"})
        with patch.object(ios_xctest, "request") as request:
            self.assertFalse(ios_xctest.stop(other))
        request.assert_not_called()

    def test_run_file_entry_is_found_in_both_formats(self):
        entry = {"BlueprintName": "AutonomUITests"}
        self.assertIs(ios_xctest._test_entry({"AutonomUITests": entry}), entry)
        nested = {"TestConfigurations": [{"TestTargets": [entry]}]}
        self.assertIs(ios_xctest._test_entry(nested), entry)
        with self.assertRaises(errors.AutonomError) as caught:
            ios_xctest._test_entry({})
        self.assertEqual(caught.exception.code, errors.XCUITEST_BUILD_FAILED)


class StoppedRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": self.tmp.name})
        self.env.start()
        self.token = session.select(None)
        ui_ios._OBSERVATIONS.clear()
        record = session.start_session(TARGET.tool, platform="ios",
                                       target_id=TARGET.target_id, app_id="test.reader")
        self.path = Path(record["artifacts_dir"]) / "session.json"
        self.stale = dict(record)  # a copy held from before the stop
        self.stopped = session.stop_session(reap=False)
        session.select(record["session_id"])  # named with --session-id
        self.stored = self.path.read_bytes()

    def tearDown(self):
        session._SELECTED.reset(self.token)
        ui_ios._OBSERVATIONS.clear()
        self.env.stop()
        self.tmp.cleanup()

    def test_a_ui_read_never_rewrites_a_stopped_session(self):
        ui_ios._record(TARGET, "xcuitest", fallback_reason="empty_accessibility_tree")
        self.assertEqual(self.path.read_bytes(), self.stored)
        self.assertNotIn("ui_observation", json.loads(self.stored))

    def test_save_skips_a_stopped_record(self):
        self.stopped["ui_observation"] = {"ui_backend": "xcuitest"}
        session.save(self.stopped)
        self.assertEqual(self.path.read_bytes(), self.stored)

    def test_save_skips_a_stale_copy_of_a_stopped_session(self):
        self.stale["ui_observation"] = {"ui_backend": "xcuitest"}
        session.save(self.stale)
        self.assertEqual(self.path.read_bytes(), self.stored)
        self.assertTrue(json.loads(self.path.read_text())["stopped_at"])

    def test_a_request_bound_to_a_stopped_session_starts_no_runner(self):
        # flow run --session-id ..., then `session stop` elsewhere mid-run.
        with patch.object(ios_xctest, "_build") as build, \
                patch.object(ios_xctest.subprocess, "Popen") as popen, \
                patch.object(ios_xctest.processes, "register") as register:
            with self.assertRaises(errors.AutonomError) as caught:
                ios_xctest.request(TARGET, "tap", x=10, y=20)
        self.assertEqual(caught.exception.code, errors.SESSION_STOPPED)
        build.assert_not_called()
        popen.assert_not_called()
        register.assert_not_called()


def _load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_ui_recovery", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StoppingMarkTests(unittest.TestCase):
    """Issue #31: `session stop` marks `stopping_at` before its teardown, and
    a runner request refuses a stopping session on entry, under request.lock
    and right before it would start a runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": self.tmp.name})
        self.env.start()
        self.token = session.select(None)
        self.record = session.start_session(TARGET.tool, platform="ios",
                                            target_id=TARGET.target_id, app_id="test.reader")
        self.session_json = Path(self.record["artifacts_dir"]) / "session.json"
        self.current_json = session.artifacts_root() / "current.json"

    def tearDown(self):
        session._SELECTED.reset(self.token)
        self.env.stop()
        self.tmp.cleanup()

    def _template(self):
        template = Path(self.tmp.name) / "Build" / "Products" / "runner.xctestrun"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_bytes(plistlib.dumps({"AutonomUITests": {}}))
        return template

    def _assert_refused_without_runner(self, call, build=None):
        build = build or Mock(return_value=self._template())
        with patch.object(ios_xctest, "_build", build), \
                patch.object(ios_xctest, "_alive", return_value=False), \
                patch.object(ios_xctest.subprocess, "Popen") as popen, \
                patch.object(ios_xctest.processes, "register") as register:
            with self.assertRaises(errors.AutonomError) as caught:
                call()
        self.assertEqual(caught.exception.code, errors.SESSION_STOPPED)
        popen.assert_not_called()
        register.assert_not_called()
        return build

    def _stored(self):
        return json.loads(self.session_json.read_text())

    def _assert_served(self):
        with patch.object(ios_xctest, "_ensure", return_value={"token": "t"}) as ensure, \
                patch.object(ios_xctest, "_exchange", return_value={"dispatched": True}):
            self.assertEqual(ios_xctest.request(TARGET, "tap", x=10, y=20), {"dispatched": True})
        ensure.assert_called_once()

    def test_mark_is_persisted_atomically_to_both_files(self):
        token = session.mark_stopping(self.record)
        for path in (self.session_json, self.current_json):
            stored = json.loads(path.read_text())
            self.assertTrue(stored["stopping_at"])
            self.assertEqual(stored["stopping_tokens"], [token])
        self.assertEqual(self.record["stopping_tokens"], [token])
        self.assertNotIn("stopped_at", self._stored())
        session.clear_stopping(self.record, token)
        for path in (self.session_json, self.current_json):
            stored = json.loads(path.read_text())
            self.assertNotIn("stopping_at", stored)
            self.assertNotIn("stopping_tokens", stored)
        self.assertNotIn("stopping_tokens", self.record)

    def test_each_stop_gets_its_own_token(self):
        first = session.mark_stopping(self.record)
        second = session.mark_stopping(self.record)
        self.assertNotEqual(first, second)
        self.assertEqual(self._stored()["stopping_tokens"], [first, second])

    def test_one_of_two_stoppers_clearing_keeps_the_mark_and_refuses(self):
        first = session.mark_stopping(self.record)
        began = self._stored()["stopping_at"]
        second = session.mark_stopping(self.record)
        self.assertEqual(self._stored()["stopping_at"], began)  # the first stop's time
        session.clear_stopping(self.record, first)
        stored = self._stored()
        self.assertEqual(stored["stopping_tokens"], [second])
        self.assertEqual(stored["stopping_at"], began)
        self.assertEqual(json.loads(self.current_json.read_text())["stopping_tokens"], [second])
        build = self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "tap", x=10, y=20))
        build.assert_not_called()
        # Clearing the same token again, or an unknown one, changes nothing.
        session.clear_stopping(self.record, first)
        session.clear_stopping(self.record, "not-a-stopper")
        self.assertEqual(self._stored()["stopping_tokens"], [second])
        session.clear_stopping(self.record, second)
        stored = self._stored()
        self.assertNotIn("stopping_at", stored)
        self.assertNotIn("stopping_tokens", stored)
        self._assert_served()

    def test_a_bare_stopping_at_counts_as_one_anonymous_stopper(self):
        # A record written by the version before owner tokens.
        stored = self._stored()
        stored["stopping_at"] = "2026-10-04T00:00:00Z"
        self.session_json.write_text(json.dumps(stored))
        self.assertEqual(session.active_stoppers(stored), [session.ANONYMOUS_STOPPER])
        build = self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "tap", x=10, y=20))
        build.assert_not_called()
        token = session.mark_stopping(self.record)
        stored = self._stored()
        self.assertEqual(stored["stopping_at"], "2026-10-04T00:00:00Z")
        self.assertEqual(stored["stopping_tokens"], [session.ANONYMOUS_STOPPER, token])
        session.clear_stopping(self.record, token)
        stored = self._stored()
        self.assertEqual(stored["stopping_at"], "2026-10-04T00:00:00Z")
        self.assertEqual(session.active_stoppers(stored), [session.ANONYMOUS_STOPPER])
        self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "tap", x=10, y=20))

    def test_save_keeps_the_token_list_of_the_file(self):
        stale = session.load_current()  # a copy from before both marks
        first = session.mark_stopping(self.record)
        second = session.mark_stopping(self.record)
        session.save(stale)
        self.assertEqual(self._stored()["stopping_tokens"], [first, second])
        marked = session.load_current()  # a copy holding both tokens
        session.clear_stopping(self.record, first)
        session.save(marked)
        self.assertEqual(self._stored()["stopping_tokens"], [second])
        self.assertEqual(json.loads(self.current_json.read_text())["stopping_tokens"], [second])
        session.clear_stopping(self.record, second)
        session.save(marked)
        self.assertNotIn("stopping_tokens", self._stored())
        self.assertNotIn("stopping_at", self._stored())

    def test_save_neither_drops_nor_revives_the_mark(self):
        stale = session.load_current()  # a copy from before the mark
        session.mark_stopping(self.record)
        stale["ui_observation"] = {"ui_backend": "xcuitest"}
        session.save(stale)
        on_disk = json.loads(self.session_json.read_text())
        self.assertTrue(on_disk["stopping_at"])
        self.assertEqual(on_disk["ui_observation"], {"ui_backend": "xcuitest"})
        marked = session.load_current()  # a copy taken during the stop
        session.clear_stopping(self.record, session.active_stoppers(marked)[0])
        session.save(marked)
        self.assertNotIn("stopping_at", json.loads(self.session_json.read_text()))
        self.assertNotIn("stopping_at", json.loads(self.current_json.read_text()))

    def test_a_record_without_the_mark_is_served(self):
        # Records written by older versions carry no stopping_at at all.
        self.assertNotIn("stopping_at", json.loads(self.session_json.read_text()))
        with patch.object(ios_xctest, "_ensure", return_value={"token": "t"}) as ensure, \
                patch.object(ios_xctest, "_exchange", return_value={"dispatched": True}):
            self.assertEqual(ios_xctest.request(TARGET, "tap", x=1, y=2), {"dispatched": True})
        ensure.assert_called_once()

    def test_a_request_during_teardown_is_refused_before_any_runner_work(self):
        # `session stop` has marked the session and is tearing down; stopped_at
        # is not written yet and the current pointer still names the session.
        session.mark_stopping(self.record)
        self.assertNotIn("stopped_at", json.loads(self.current_json.read_text()))
        build = self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "tap", x=10, y=20))
        build.assert_not_called()

    def test_a_request_bound_by_session_id_during_teardown_is_refused(self):
        session.select(self.record["session_id"])
        session.mark_stopping(self.record)
        build = self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "snapshot"))
        build.assert_not_called()

    def test_a_stop_that_begins_while_waiting_for_the_lock_is_refused(self):
        real_lock = ios_xctest.lock

        @contextlib.contextmanager
        def mark_then_lock(path):
            session.mark_stopping(self.record)
            with real_lock(path):
                yield
        with patch.object(ios_xctest, "lock", side_effect=mark_then_lock):
            build = self._assert_refused_without_runner(
                lambda: ios_xctest.request(TARGET, "tap", x=10, y=20))
        build.assert_not_called()

    def test_a_stop_that_begins_while_the_runner_builds_gets_no_popen(self):
        template = self._template()

        def build_while_stop_begins():
            session.mark_stopping(self.record)  # `session stop` elsewhere
            return template
        build = self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "tap", x=10, y=20),
            build=Mock(side_effect=build_while_stop_begins))
        build.assert_called_once()

    def test_a_stop_that_finishes_while_the_runner_builds_gets_no_popen(self):
        # The finished stop removed current.json: the session's own file decides.
        template = self._template()

        def build_while_stop_finishes():
            session.stop_session(reap=False)
            return template
        build = self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "tap", x=10, y=20),
            build=Mock(side_effect=build_while_stop_finishes))
        build.assert_called_once()
        self.assertFalse(self.current_json.exists())

    def test_session_stop_marks_before_its_first_teardown_step(self):
        cli = _load_cli()
        seen = {}

        def first_step(_target):
            seen["mark"] = session.on_disk(self.record).get("stopping_at")
            seen["stopped"] = session.on_disk(self.record).get("stopped_at")
            return False
        with patch.object(cli, "_target", return_value=TARGET), \
                patch.object(cli, "_session_target_gone", return_value=False), \
                patch.object(ios_xctest, "stop", side_effect=first_step), \
                patch.object(session, "reap_owned_processes", return_value={}), \
                patch.object(cli.logs_mod, "stop_log_writer", return_value={"result": "not_running"}), \
                patch.object(cli, "_stop_recorder", return_value={"result": "not_running"}), \
                patch.object(cli.proxy_mod, "stop", return_value={}), \
                contextlib.redirect_stdout(open(os.devnull, "w")) as sink:
            self.assertEqual(cli.cmd_session_stop(cli.argparse.Namespace()), 0)
        sink.close()
        self.assertTrue(seen["mark"])
        self.assertIsNone(seen["stopped"])
        stored = json.loads(self.session_json.read_text())
        self.assertTrue(stored["stopped_at"])
        self.assertFalse(self.current_json.exists())

    def test_an_aborted_stop_clears_the_mark_and_requests_work_again(self):
        cli = _load_cli()
        record = session.load_current()
        record["accessibility"] = {"previous": {}, "applied": {}, "enabled_by_autonom": True}
        session.save(record)
        during = {}

        def restore_fails(_target, _record):
            # While the stop runs, a runner request is refused.
            try:
                ios_xctest.request(TARGET, "tap", x=10, y=20)
            except errors.AutonomError as exc:
                during["code"] = exc.code
            raise errors.AutonomError(errors.BACKEND_FAILED, "accessibility settings did not restore")
        with patch.object(cli, "_target", return_value=TARGET), \
                patch.object(cli.accessibility_mod, "restore", side_effect=restore_fails), \
                patch.object(ios_xctest, "_build") as build, \
                patch.object(ios_xctest, "stop") as runner_stop:
            with self.assertRaises(errors.AutonomError) as caught:
                cli.cmd_session_stop(cli.argparse.Namespace())
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertEqual(during["code"], errors.SESSION_STOPPED)
        build.assert_not_called()
        runner_stop.assert_not_called()
        stored = json.loads(self.session_json.read_text())
        self.assertNotIn("stopping_at", stored)
        self.assertNotIn("stopped_at", stored)
        self.assertTrue(self.current_json.exists())
        self.assertNotIn("stopping_at", json.loads(self.current_json.read_text()))
        self.assertNotIn("stopping_tokens", stored)
        # The session stays usable: the next request reaches the runner.
        self._assert_served()

    def test_an_interrupted_stop_clears_only_its_own_token(self):
        # S2 (another terminal) is tearing down; S1 is interrupted (Ctrl-C)
        # during its accessibility restore. S1 must withdraw only its own
        # token: S2's mark stays and a request is still refused.
        cli = _load_cli()
        record = session.load_current()
        record["accessibility"] = {"previous": {}, "applied": {}, "enabled_by_autonom": True}
        session.save(record)
        other = session.mark_stopping(self.record)  # S2
        seen = {}

        def restore_interrupted(_target, _record):
            seen["tokens"] = list(session.on_disk(self.record)["stopping_tokens"])
            raise KeyboardInterrupt
        with patch.object(cli, "_target", return_value=TARGET), \
                patch.object(cli.accessibility_mod, "restore", side_effect=restore_interrupted), \
                patch.object(ios_xctest, "stop") as runner_stop:
            with self.assertRaises(KeyboardInterrupt):
                cli.cmd_session_stop(cli.argparse.Namespace())
        runner_stop.assert_not_called()
        self.assertEqual(len(seen["tokens"]), 2)
        self.assertEqual(seen["tokens"][0], other)
        stored = self._stored()
        self.assertEqual(stored["stopping_tokens"], [other])
        self.assertTrue(stored["stopping_at"])
        self.assertNotIn("stopped_at", stored)
        self.assertEqual(json.loads(self.current_json.read_text())["stopping_tokens"], [other])
        build = self._assert_refused_without_runner(
            lambda: ios_xctest.request(TARGET, "tap", x=10, y=20))
        build.assert_not_called()
        # Once S2 withdraws too, requests reach the runner again.
        session.clear_stopping(self.record, other)
        self.assertNotIn("stopping_at", self._stored())
        self._assert_served()


class LiveScriptHelperTests(unittest.TestCase):
    """The live script's own checks: Xcode major parsing, typed-text redaction."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent / "live" / "ios_ui_recovery_live.py"
        spec = importlib.util.spec_from_file_location("ios_ui_recovery_live", path)
        cls.live = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.live)

    def test_xcode_major_parsing(self):
        self.assertEqual(self.live.xcode_major("Xcode 27.0 Build version 27A266a"), 27)
        self.assertEqual(self.live.xcode_major("Xcode 9.4.1 Build version 9F2000"), 9)
        self.assertIsNone(self.live.xcode_major(""))
        self.assertIsNone(self.live.xcode_major("xcode-select: error: tool 'xcodebuild' requires Xcode"))

    def test_typed_text_never_reaches_the_recorded_calls(self):
        autonom = self.live.Autonom("UDID", Path(self.id()), None)
        typed = self.live.TYPED
        answer = subprocess.CompletedProcess([], 0, stdout=json.dumps(
            {"ok": True, "typed": typed, "backend": "xcuitest"}), stderr="")
        with patch.object(self.live, "run", return_value=answer):
            payload = autonom("ui", "type", typed, "--sensitive", secret=typed)
        self.assertNotIn(typed, json.dumps(autonom.calls))
        self.assertNotIn(typed, json.dumps(payload))
        self.assertEqual(autonom.calls[0]["argv"], ["ui", "type", f"<{len(typed)} chars>", "--sensitive"])
        self.assertEqual(payload["backend"], "xcuitest")

    def test_a_failed_type_keeps_the_text_out_of_the_error(self):
        autonom = self.live.Autonom("UDID", Path(self.id()), None)
        typed = self.live.TYPED
        answer = subprocess.CompletedProcess([], 2, stdout="", stderr=json.dumps(
            {"ok": False, "error_code": "no_focused_field", "error": "Tap a text field"}))
        with patch.object(self.live, "run", return_value=answer):
            with self.assertRaises(self.live.StepFailed) as caught:
                autonom("ui", "type", typed, secret=typed)
        self.assertNotIn(typed, str(caught.exception))
        self.assertIn("no_focused_field", str(caught.exception))

    def test_row_helpers_on_partial_nodes(self):
        nodes = [{"desc": "General", "resource_id": "com.apple.settings.general",
                  "bounds": [16, 293, 386, 345]},
                 {"desc": "General", "role": "text"},  # the row's label, not the row
                 {"desc": "Wallet", "resource_id": "com.apple.settings.wallet"},
                 {"resource_id": "com.apple.settings.empty"}]
        self.assertEqual(self.live.row_labels(nodes), {"General", "Wallet"})
        self.assertEqual(self.live.row_top(nodes, "General"), 293)
        self.assertIsNone(self.live.row_top(nodes, "Wallet"))
        self.assertIsNone(self.live.search_field(nodes))


class SessionWriteSafetyTests(unittest.TestCase):
    """_record runs on every iOS read and input, so session.json/current.json
    are replaced atomically, skipped when nothing changed, and never written
    over a stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTONOM_HOME": self.tmp.name})
        self.env.start()
        self.token = session.select(None)
        ui_ios.set_preference(None)
        self.record = session.start_session(TARGET.tool, platform="ios",
                                            target_id=TARGET.target_id, app_id="test.reader")
        self.session_json = Path(self.record["artifacts_dir"]) / "session.json"
        self.current_json = session.artifacts_root() / "current.json"

    def tearDown(self):
        ui_ios.set_preference(None)
        session._SELECTED.reset(self.token)
        self.env.stop()
        self.tmp.cleanup()

    def _leftovers(self):
        return [p.name for d in (self.session_json.parent, self.current_json.parent)
                for p in d.iterdir() if p.name.endswith(".tmp")]

    def test_a_failed_write_leaves_both_files_whole(self):
        before = (self.session_json.read_text(), self.current_json.read_text())
        changed = dict(self.record, ui_backend_preference="xcuitest")
        with patch.object(session.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                session.save(changed)
        self.assertEqual((self.session_json.read_text(), self.current_json.read_text()), before)
        self.assertEqual(self._leftovers(), [])

    def test_save_replaces_files_instead_of_rewriting_them(self):
        inode = self.session_json.stat().st_ino
        mode = self.session_json.stat().st_mode & 0o777
        with patch.object(Path, "write_text", side_effect=AssertionError("in-place write")):
            session.save(dict(self.record, ui_backend_preference="idb"))
        self.assertNotEqual(self.session_json.stat().st_ino, inode)
        self.assertEqual(self.session_json.stat().st_mode & 0o777, mode)
        self.assertEqual(json.loads(self.current_json.read_text())["ui_backend_preference"], "idb")
        self.assertEqual(self._leftovers(), [])

    def test_concurrent_reader_never_sees_a_partial_file(self):
        import threading
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                for path in (self.session_json, self.current_json):
                    try:
                        json.loads(path.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        pass
                    except ValueError as exc:
                        bad.append(str(exc))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            big = {"padding": "x" * 200_000}
            for index in range(40):
                session.save(dict(self.record, ui_observation={**big, "n": index}))
        finally:
            stop.set()
            thread.join()
        self.assertEqual(bad, [])

    def test_unchanged_observation_is_not_written_again(self):
        with patch.object(session, "save", wraps=session.save) as save:
            ui_ios._record(TARGET, "idb", meaningful_nodes=3, accessibility="available")
            ui_ios._OBSERVATIONS.clear()  # a fresh process reads the stored one
            ui_ios._record(TARGET, "idb", meaningful_nodes=3, accessibility="available")
            self.assertEqual(save.call_count, 1)
            ui_ios._record(TARGET, "idb", meaningful_nodes=4, accessibility="available")
            self.assertEqual(save.call_count, 2)
            later = time.time() + ui_ios.OBSERVATION_REWRITE_S + 1
            with patch.object(ui_ios.time, "time", return_value=later):
                ui_ios._record(TARGET, "idb", meaningful_nodes=4, accessibility="available")
            self.assertEqual(save.call_count, 3)
        stored = json.loads(self.session_json.read_text())["ui_observation"]
        self.assertEqual(stored["meaningful_nodes"], 4)
        self.assertEqual(stored["accessibility_at"], later)

    def test_a_save_holding_a_pre_stop_copy_never_undoes_the_stop(self):
        stale = session.load_current()
        stopped = session.stop_session(reap=False)
        with patch.object(session, "load_current", return_value=stale):
            ui_ios._record(TARGET, "idb", meaningful_nodes=2, accessibility="available")
        session.save(dict(stale, ui_backend_preference="idb"))
        on_disk = json.loads(self.session_json.read_text())
        self.assertEqual(on_disk["stopped_at"], stopped["stopped_at"])
        self.assertNotIn("ui_observation", on_disk)
        self.assertFalse(self.current_json.exists())

    def test_a_save_waiting_on_a_stop_rechecks_after_the_lock(self):
        import threading
        stale = session.load_current()
        with session._record_lock(self.session_json.parent):
            worker = threading.Thread(
                target=session.save, args=(dict(stale, ui_backend_preference="idb"),))
            worker.start()
            worker.join(0.3)
            self.assertTrue(worker.is_alive())  # blocked on the stop's lock
            stopped = dict(stale, stopped_at="2026-10-04T00:00:00Z")
            session._write_atomic(self.session_json, json.dumps(stopped))
            self.current_json.unlink()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        on_disk = json.loads(self.session_json.read_text())
        self.assertEqual(on_disk["stopped_at"], "2026-10-04T00:00:00Z")
        self.assertNotIn("ui_backend_preference", on_disk)
        self.assertFalse(self.current_json.exists())


class ExplicitSessionTests(JournalBase):
    def test_action_and_stop_belong_to_the_selected_session(self):
        first = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "one")["session"]
        result = self.run_cli("ui", "tap", "--x", "10", "--y", "20", "--session-id", first["session_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["target_id"], "emulator-5554")
        stopped = self.run_cli("session", "stop", "--session-id", first["session_id"])
        self.assertTrue(stopped["ok"], stopped)
        self.assertEqual(self.run_cli("session", "show")["error_code"], "no_active_session")
        journal = self.run_cli("journal", "--session-id", first["session_id"])
        self.assertEqual([e["verb"] for e in journal["entries"]], ["session start", "ui tap", "session stop"])

    def test_a_stopped_session_stays_readable_without_moving_current(self):
        first = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "one")["session"]
        self.run_cli("session", "stop")
        second = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "two")["session"]
        shown = self.run_cli("session", "show", "--session-id", first["session_id"])
        self.assertEqual(shown["session"]["session_id"], first["session_id"])
        self.assertEqual(self.run_cli("journal", "--session-id", first["session_id"])["ok"], True)
        self.assertEqual(self.run_cli("session", "show")["session"]["session_id"], second["session_id"])

    def test_logs_tail_never_overwrites_a_stopped_sessions_logs(self):
        first = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "one")["session"]
        live = self.run_cli("logs", "tail")
        self.assertTrue(live["ok"], live)
        saved = Path(first["artifacts_dir"]) / "logs" / "latest.json"
        self.assertEqual(live["saved"], str(saved))
        evidence = saved.read_bytes()
        self.run_cli("session", "stop")
        result = self.run_cli("logs", "tail", "--session-id", first["session_id"])
        self.assertTrue(result["ok"], result)
        self.assertIn("lines", result)
        self.assertIsNone(result.get("saved"))
        self.assertEqual(saved.read_bytes(), evidence)

    def test_session_start_refuses_a_session_id(self):
        self.start()
        selected = self.run_cli("session", "show")["session"]["session_id"]
        self.run_cli("session", "stop")
        result = self.run_cli("session", "start", "--serial", "emulator-5554", "--session-id", selected)
        self.assertEqual(result["error_code"], "usage_error")

    def test_explicit_session_cannot_control_another_target(self):
        self.start()
        selected = self.run_cli("session", "show")["session"]["session_id"]
        result = self.run_cli("ui", "tap", "--x", "10", "--y", "20", "--session-id", selected,
                              "--serial", "emulator-5556")
        self.assertEqual(result["error_code"], "session_target_mismatch")

    def test_invalid_session_is_a_json_error(self):
        result = self.run_cli("ui", "tree", "--session-id", "../../elsewhere")
        self.assertEqual(result["error_code"], "session_not_found")

    def test_stopped_session_refuses_input(self):
        self.start()
        selected = self.run_cli("session", "show")["session"]["session_id"]
        self.run_cli("session", "stop", "--session-id", selected)
        result = self.run_cli("ui", "tap", "--x", "10", "--y", "20", "--session-id", selected)
        self.assertEqual(result["error_code"], "session_stopped")

    def test_report_serve_refuses_a_stopped_session_named_with_session_id(self):
        # /replay drives the device; it is not a read-only use of a stopped session.
        first = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "one")["session"]
        self.run_cli("session", "stop")
        stored = (Path(first["artifacts_dir"]) / "session.json").read_bytes()
        result = self.run_cli("report", "serve", "--port", "0", "--session-id", first["session_id"])
        self.assertEqual(result["error_code"], "session_stopped")
        self.assertIn("report serve", result["hint"])
        self.assertEqual((Path(first["artifacts_dir"]) / "session.json").read_bytes(), stored)
        # other report verbs still read it (no target flags: report build takes none)
        built = subprocess.run(
            [sys.executable, str(CLI), "--session-id", first["session_id"], "report", "build"],
            capture_output=True, text=True, env=self.env, cwd=self.cwd, timeout=60)
        payload = json.loads(built.stdout if built.returncode == 0 else built.stderr)
        self.assertNotIn(payload.get("error_code"), ("session_stopped", "usage_error"), payload)

    def _journal(self, record):
        path = Path(record["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_stopped_session_gets_no_new_journal_entries(self):
        first = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "one")["session"]
        self.run_cli("session", "stop")
        before = self._journal(first)
        for argv in (("session", "outputs"), ("session", "show"), ("shots", "list"),
                     ("journal",), ("ui", "tree"), ("ui", "tap", "--x", "1", "--y", "1")):
            self.run_cli(*argv, "--session-id", first["session_id"])
        self.assertEqual(self._journal(first), before)
        # With a current session the entry goes there, as it did before the flag.
        second = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "two")["session"]
        self.run_cli("session", "outputs", "--session-id", first["session_id"])
        self.assertEqual(self._journal(first), before)
        self.assertEqual([e["verb"] for e in self._journal(second)], ["session start", "session outputs"])

    def test_a_command_for_another_target_is_journaled_in_the_current_session(self):
        self.state.write_text(json.dumps({"devices": [["emulator-5554", "device", ""],
                                                      ["emulator-5556", "device", ""]]}),
                              encoding="utf-8")
        record = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "one")["session"]
        result = self.run_cli("ui", "tap", "--x", "1", "--y", "1", "--serial", "emulator-5556")
        self.assertEqual(result.get("target_id", "emulator-5556"), "emulator-5556")
        verbs = [e["verb"] for e in self._journal(record)]
        self.assertEqual(verbs, ["session start", "ui tap"])

    def test_session_start_persists_the_ui_backend_choice(self):
        result = self.run_cli("session", "start", "--serial", "emulator-5554", "--app-id", "one",
                              "--ui-backend", "idb")
        self.assertEqual(result["session"]["ui_backend_preference"], "idb")

    def test_android_launch_resolves_activity_and_retains_resume_mode(self):
        self.start()
        result = self.run_cli("session", "launch", "com.example.app")
        self.assertEqual(result["component"], "com.example.app/.MainActivity")
        self.assertEqual(result["mode"], "resume")


class FlagAbbreviationTests(unittest.TestCase):
    """--session-id, --ui-backend and --probe match only in full, so every
    abbreviation of an older flag keeps the meaning it had."""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.parser = importlib.import_module("autonom").build_parser()

    def parse(self, *argv):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            return self.parser.parse_args(list(argv))

    def test_old_global_abbreviations_keep_their_flag(self):
        self.assertEqual(self.parse("--u", "UDID-1", "ui", "tree").udid, "UDID-1")
        self.assertEqual(self.parse("ui", "tree", "--se", "emulator-5554").serial, "emulator-5554")
        self.assertEqual(self.parse("capabilities", "--p", "ios").platform, "ios")

    def test_new_flags_work_in_full_before_and_after_the_verb(self):
        args = self.parse("--ui-backend", "xcuitest", "ui", "tree", "--session-id", "s_1")
        self.assertEqual((args.ui_backend, args.session_id), ("xcuitest", "s_1"))
        self.assertEqual(self.parse("ui", "tree", "--ui-backend=idb").ui_backend, "idb")
        self.assertTrue(self.parse("capabilities", "--probe").probe)

    def test_a_session_id_abbreviation_is_not_a_new_spelling(self):
        with self.assertRaises(SystemExit):
            self.parse("ui", "tree", "--sess", "s_1")

    def test_verbs_that_had_session_id_keep_its_abbreviations(self):
        self.assertEqual(self.parse("journal", "--sess", "s_1").session_id, "s_1")
        self.assertEqual(self.parse("logs", "follow", "--session", "s_1").session_id, "s_1")

    def test_journal_scrubber_resolves_like_argparse(self):
        from autonom_lib import journal
        argv = ["--u", "UDID-1", "ui", "type", "SECRETTEXT"]
        scrubbed = journal.scrub_for_journal(argv, self.parse(*argv), self.parser)
        self.assertNotIn("SECRETTEXT", json.dumps(scrubbed))
        argv = ["--session-id", "s_1", "ui", "type", "SECRETTEXT2"]
        for namespace in (None, self.parse(*argv)):
            self.assertNotIn("SECRETTEXT2", json.dumps(journal.scrub_for_journal(argv, namespace, self.parser)))
            self.assertNotIn("SECRETTEXT2", json.dumps(journal.scrub_for_journal(argv, namespace, None)))
