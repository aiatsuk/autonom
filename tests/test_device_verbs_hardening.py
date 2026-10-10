"""Device verbs report real failures (CLI-003, CLI-005, CLI-007, CLI-008, CLI-010).

Each case reproduces what a real device or simulator printed during the
2026-09 sweep — with a scripted `run_adb`, a temp iOS container, or the fake
emulator — so no test here ever reaches adb, simctl, idb or an emulator.
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
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib import (  # noqa: E402
    device_state, emulator, errors, follow, logs, processes, session, ui,
)
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

try:
    from process_isolation import scope_proxy_discovery  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.process_isolation import scope_proxy_discovery  # noqa: E402

FAKE_ADB = str(ROOT / "tests/fakes/fake_adb.py")
FAKE_EMULATOR = str(ROOT / "tests/fakes/fake_emulator.py")
ANDROID = Target("android", "emulator-5554", "/fake/adb", {"serial": "emulator-5554"})
IOS = Target("ios", "UDID-1234", "/fake/xcrun", {"udid": "UDID-1234"})


class ScriptedAdb:
    """Stand-in for `adb.run_adb`: answers by argv prefix and records every call.

    Rules are `(joined-argv prefix, stdout, returncode)`; the first match wins
    and an unmatched call succeeds silently, like `fake_adb.py`. `check=True`
    raises the same `AdbError` the real helper does.
    """

    def __init__(self, rules: list[tuple[str, str, int]] | None = None) -> None:
        self.rules = rules or []
        self.calls: list[list[str]] = []

    def __call__(self, adb: str, args, *, serial=None, timeout=30, check=True,
                 binary=False):
        argv = list(args)
        self.calls.append(argv)
        joined = " ".join(argv)
        stdout, code = "", 0
        for prefix, out, rc in self.rules:
            if joined.startswith(prefix):
                stdout, code = out, rc
                break
        if check and code != 0:
            raise adb_mod.AdbError(stdout.strip() or f"adb {joined} failed ({code})")
        if binary:
            return subprocess.CompletedProcess(argv, code, stdout.encode(), b"")
        return subprocess.CompletedProcess(argv, code, stdout, None)

    def joined(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


def scripted(*rules: tuple[str, str, int]) -> ScriptedAdb:
    return ScriptedAdb(list(rules))


# --- CLI-003 / CLI-010: permissions -----------------------------------------

DUMPSYS_PACKAGE = """\
Packages:
  Package [com.android.chrome] (a1b2c3):
    requested permissions:
      android.permission.CAMERA
      android.permission.RECORD_AUDIO
    install permissions:
      android.permission.INTERNET: granted=true
    User 0: ceDataInode=1 installed=true
      runtime permissions:
        android.permission.POST_NOTIFICATIONS: granted=false, flags=[ USER_SENSITIVE_WHEN_GRANTED ]
        android.permission.CAMERA: granted=true, flags=[ USER_SET ]
        android.permission.RECORD_AUDIO: granted=true, flags=[ USER_SET ]
      enabledComponents:
"""

UNKNOWN_PERMISSION = (
    "Exception occurred while executing 'grant':\n"
    "java.lang.IllegalArgumentException: Unknown permission: camera\n"
    "\tat com.android.server.pm.permission.PermissionManagerServiceImpl.grantRuntimePermission"
)


class PermissionTests(unittest.TestCase):
    def test_reset_all_never_resets_every_app(self) -> None:
        """Before: `pm reset-permissions com.android.chrome` — which takes no
        package and reset YouTube's RECORD_AUDIO too."""
        fake = scripted(("shell dumpsys package com.android.chrome", DUMPSYS_PACKAGE, 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            detail = device_state.permissions(ANDROID, "reset", "all", "com.android.chrome")
        self.assertFalse(any("reset-permissions" in call for call in fake.joined()))
        revokes = [call for call in fake.joined() if call.startswith("shell pm revoke")]
        self.assertEqual(revokes, [
            "shell pm revoke com.android.chrome android.permission.CAMERA",
            "shell pm revoke com.android.chrome android.permission.RECORD_AUDIO",
        ])
        self.assertEqual(detail["revoked"], ["android.permission.CAMERA",
                                             "android.permission.RECORD_AUDIO"])
        self.assertEqual(detail["scope"], "package")

    def test_reset_one_permission_revokes_only_it(self) -> None:
        fake = scripted()
        with mock.patch.object(adb_mod, "run_adb", fake):
            detail = device_state.permissions(ANDROID, "reset", "camera", "com.example.app")
        self.assertEqual(detail["revoked"], ["android.permission.CAMERA"])
        self.assertIn("shell pm revoke com.example.app android.permission.CAMERA", fake.joined())
        self.assertFalse(any("reset-permissions" in call for call in fake.joined()))

    def test_reset_all_of_a_missing_package_is_named(self) -> None:
        fake = scripted(("shell dumpsys package", "Unable to find package: com.nope\n", 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                device_state.permissions(ANDROID, "reset", "all", "com.nope")
        self.assertEqual(caught.exception.code, errors.APP_NOT_INSTALLED)

    def test_granted_runtime_permissions_skips_install_permissions(self) -> None:
        self.assertEqual(device_state.granted_runtime_permissions(DUMPSYS_PACKAGE),
                         ["android.permission.CAMERA", "android.permission.RECORD_AUDIO"])

    def test_short_name_grant_maps_to_the_android_permission(self) -> None:
        fake = scripted()
        with mock.patch.object(adb_mod, "run_adb", fake):
            detail = device_state.permissions(ANDROID, "grant", "camera", "com.example.app")
        self.assertEqual(fake.joined(),
                         ["shell pm grant com.example.app android.permission.CAMERA"])
        self.assertEqual(detail["permissions"], ["android.permission.CAMERA"])
        self.assertEqual(detail["service"], "camera")

    def test_location_grants_the_whole_group(self) -> None:
        fake = scripted()
        with mock.patch.object(adb_mod, "run_adb", fake):
            device_state.permissions(ANDROID, "grant", "location", "com.example.app")
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("android.permission.ACCESS_COARSE_LOCATION", fake.joined()[1])

    def test_unknown_permission_is_invalid_value_with_a_hint(self) -> None:
        """Before: backend_failed carrying the raw Java trace."""
        fake = scripted(("shell pm grant", UNKNOWN_PERMISSION, 255))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                device_state.permissions(ANDROID, "grant", "cameraa", "com.example.app")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertIn("android.permission.CAMERA", caught.exception.hint)
        self.assertNotIn("java.lang", caught.exception.message)

    def test_undeclared_permission_is_invalid_value(self) -> None:
        text = ("Exception occurred while executing 'grant':\njava.lang.SecurityException: "
                "Package com.example.app has not requested permission android.permission.CAMERA")
        fake = scripted(("shell pm grant", text, 255))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                device_state.permissions(ANDROID, "grant", "android.permission.CAMERA",
                                         "com.example.app")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertIn("manifest", caught.exception.hint)

    def test_location_hint_names_no_missing_flag(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.parse_coordinates("abc")
        self.assertNotIn("--at", caught.exception.hint)
        self.assertIn("location set", caught.exception.hint)


# --- CLI-003: container files -------------------------------------------------


class AndroidFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "pulled"

    def test_pull_of_a_missing_file_is_not_written_as_the_file(self) -> None:
        """Before: exit 0 and a file containing `cat: ...: No such file`."""
        fake = scripted(("exec-out run-as com.example.app cat files/missing",
                         "cat: files/missing: No such file or directory\n", 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                device_state.file_pull(ANDROID, "com.example.app", "files/missing", self.out)
        self.assertEqual(caught.exception.code, errors.BODY_FILE_NOT_FOUND)
        self.assertEqual(caught.exception.extra.get("detail"), "path_not_found")
        self.assertFalse(self.out.exists())
        self.assertNotIn("Documents/", caught.exception.hint)

    def test_pull_of_a_real_file_still_works(self) -> None:
        fake = scripted(("exec-out run-as", "hello\n", 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            detail = device_state.file_pull(ANDROID, "com.example.app", "files/a", self.out)
        self.assertEqual(self.out.read_bytes(), b"hello\n")
        self.assertEqual(detail["bytes"], 6)

    def test_ls_of_a_missing_dir_is_not_a_listing(self) -> None:
        fake = scripted(("exec-out run-as com.example.app ls",
                         "ls: files/missing: No such file or directory\n", 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                device_state.file_ls(ANDROID, "com.example.app", "files/missing")
        self.assertEqual(caught.exception.code, errors.BODY_FILE_NOT_FOUND)

    def test_unknown_package_is_not_installed_not_undebuggable(self) -> None:
        fake = scripted(("exec-out run-as", "run-as: unknown package: com.nope\n", 1))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                device_state.file_ls(ANDROID, "com.nope")
        self.assertEqual(caught.exception.code, errors.APP_NOT_INSTALLED)

    def test_android_path_hint_names_android_paths(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.safe_relative("/etc/passwd", "android")
        self.assertNotIn("Documents/state.json", caught.exception.hint)
        self.assertIn("shared_prefs", caught.exception.hint)

    def test_double_dot_inside_a_name_is_not_an_escape(self) -> None:
        self.assertEqual(device_state.safe_relative("a..b"), "a..b")
        self.assertEqual(device_state.safe_relative("files/v1..2.json"), "files/v1..2.json")


class IosFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.container = Path(self.tmp.name) / "container"
        (self.container / "Documents").mkdir(parents=True)
        (self.container / "Documents/state.json").write_text("{}", encoding="utf-8")
        patcher = mock.patch.object(device_state.ios_simctl, "app_container",
                                    return_value=self.container)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.idb_pull = mock.patch.object(device_state.ios_idb, "file_pull").start()
        self.addCleanup(mock.patch.stopall)
        self.out = Path(self.tmp.name) / "out/pulled"

    def test_pull_of_a_directory_is_typed_not_a_traceback(self) -> None:
        """Before: IsADirectoryError from read_bytes."""
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.file_pull(IOS, "com.example.app", "Documents", self.out)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(caught.exception.extra.get("detail"), "is_a_directory")

    def test_pull_of_a_missing_path_matches_ls(self) -> None:
        """Before: fell through to idb, which failed as backend_failed."""
        with self.assertRaises(errors.AutonomError) as pulled:
            device_state.file_pull(IOS, "com.example.app", "Documents/nope.json", self.out)
        with self.assertRaises(errors.AutonomError) as listed:
            device_state.file_ls(IOS, "com.example.app", "Documents/nope.json")
        for caught in (pulled, listed):
            self.assertEqual(caught.exception.code, errors.BODY_FILE_NOT_FOUND)
            self.assertEqual(caught.exception.extra.get("detail"), "path_not_found")
        self.idb_pull.assert_not_called()

    def test_missing_name_with_two_dots_is_not_outside_the_container(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.file_ls(IOS, "com.example.app", "a..b")
        self.assertEqual(caught.exception.code, errors.BODY_FILE_NOT_FOUND)

    def test_pull_of_a_file_still_works(self) -> None:
        detail = device_state.file_pull(IOS, "com.example.app", "Documents/state.json", self.out)
        self.assertEqual(detail["bytes"], 2)

    def test_no_container_hint_does_not_claim_all_system_apps_lack_one(self) -> None:
        with mock.patch.object(device_state.ios_simctl, "app_container", return_value=None):
            with self.assertRaises(errors.AutonomError) as caught:
                device_state.file_ls(IOS, "com.apple.Maps")
        self.assertEqual(caught.exception.code, errors.APP_NOT_INSTALLED)
        self.assertIn("com.apple.Maps has one", caught.exception.hint)
        self.assertNotIn("(com.apple.*) expose no data container", caught.exception.hint)


# --- CLI-007: launch ----------------------------------------------------------

BAD_ACTIVITY = ("Starting: Intent { cmp=com.example.app/.Bad }\nError type 3\n"
                "Error: Activity class {com.example.app/.Bad} does not exist.\n")


class LaunchTests(unittest.TestCase):
    def test_monkey_fallback_disables_system_keys(self) -> None:
        """With no resolvable launcher activity, resume falls back to monkey,
        which must pass `--pct-syskeys 0`: it exited 251 on an AVD with
        hw.keyboard=no."""
        fake = scripted()
        with mock.patch.object(adb_mod, "run_adb", fake):
            session.launch_app("adb", "emulator-5554", "com.android.settings")
        monkey = next(call for call in fake.calls if call[:2] == ["shell", "monkey"])
        self.assertEqual(monkey[:3], ["shell", "monkey", "-p"])
        index = monkey.index("--pct-syskeys")
        self.assertEqual(monkey[index + 1], "0")
        self.assertEqual(monkey[-1], "1")

    def test_monkey_without_activities_is_named(self) -> None:
        fake = scripted(("shell monkey", "** No activities found to run, monkey aborted.\n", 252))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                session.launch_app("adb", "emulator-5554", "com.nope")
        self.assertEqual(caught.exception.code, errors.APP_NOT_INSTALLED)

    def test_am_start_error_with_exit_zero_raises(self) -> None:
        fake = scripted(("shell am start", BAD_ACTIVITY, 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                session.launch_app("adb", "emulator-5554", "com.example.app", activity=".Bad")
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertIn("does not exist", caught.exception.message)

    def test_fresh_launch_error_with_exit_zero_raises(self) -> None:
        fake = scripted(("shell cmd package resolve-activity",
                         "priority=0\ncom.example.app/.Bad\n", 0),
                        ("shell am start", BAD_ACTIVITY, 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError):
                session.launch_app_fresh("adb", "emulator-5554", "com.example.app")

    def test_warning_brought_to_front_is_not_an_error(self) -> None:
        text = ("Starting: Intent { cmp=com.example.app/.Main }\nWarning: Activity not "
                "started, its current task has been brought to the front\n")
        fake = scripted(("shell am start", text, 0))
        with mock.patch.object(adb_mod, "run_adb", fake):
            session.launch_app("adb", "emulator-5554", "com.example.app", activity=".Main")

    def test_fresh_launch_with_an_explicit_activity(self) -> None:
        """Before: `--fresh --activity` fell back to a plain resume."""
        fake = scripted()
        with mock.patch.object(adb_mod, "run_adb", fake):
            detail = session.launch_app_fresh("adb", "emulator-5554", "com.example.app",
                                              activity=".Detail")
        self.assertEqual(detail, {"mode": "fresh", "component": "com.example.app/.Detail"})
        start = fake.calls[-1]
        self.assertEqual(start[:3], ["shell", "am", "start"])
        self.assertIn("com.example.app/.Detail", start)
        self.assertIn(session.FRESH_TASK_FLAGS, start)
        self.assertFalse(any("resolve-activity" in call for call in fake.joined()))


# --- CLI-005: ui helpers ------------------------------------------------------


def node(ref: str, **fields: Any) -> dict[str, Any]:
    base = {"ref": ref, "role": "text", "text": None, "desc": None, "resource_id": None,
            "class": None, "bounds": [0, 0, 10, 10], "clickable": False, "enabled": True,
            "focusable": False, "focused": False, "scrollable": False, "selected": False,
            "checked": False, "depth": 0}
    base.update(fields)
    return base


class TypingTargetTests(unittest.TestCase):
    def test_focused_button_on_android_is_not_verified(self) -> None:
        """Before: any focused node counted as a verified typing target."""
        nodes = [node("n1", role="button", **{"class": "android.widget.Button"}, focused=True)]
        focused, certainty = ui.typing_target("android", nodes)
        self.assertEqual(focused["ref"], "n1")
        self.assertNotEqual(certainty, "focused")

    def test_focused_edit_text_is_verified(self) -> None:
        nodes = [node("n1", role="textfield", **{"class": "android.widget.EditText"},
                      focused=True)]
        self.assertEqual(ui.typing_target("android", nodes)[1], "focused")
        nodes = [node("n2", role="autocompletetextview",
                      **{"class": "android.widget.AutoCompleteTextView"}, focused=True)]
        self.assertEqual(ui.typing_target("android", nodes)[1], "focused")


class SettleTests(unittest.TestCase):
    def _run(self, trees: list[list[dict[str, Any]]], **kwargs: Any) -> dict[str, Any]:
        now = [0.0]
        feed = iter(trees)
        last: list[list[dict[str, Any]]] = [trees[-1]]

        def take() -> list[dict[str, Any]]:
            try:
                last[0] = next(feed)
            except StopIteration:
                pass
            return last[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        return ui.settle(None, snapshot_fn=take, clock=lambda: now[0], sleep=sleep, **kwargs)

    def test_settles_once_two_snapshots_agree(self) -> None:
        a, b, c = ([node("n1", text=label)] for label in ("a", "b", "c"))
        result = self._run([a, b, c, c], timeout_ms=5000, quiet_ms=0, interval_ms=100)
        self.assertTrue(result["settled"])
        self.assertEqual(result["snapshots"], 4)
        self.assertEqual(result["changes"], 2)

    def test_bounds_change_is_a_change(self) -> None:
        moving = [[node("n1", bounds=[0, i, 10, 10 + i])] for i in range(100)]
        result = self._run(moving, timeout_ms=1000, quiet_ms=0, interval_ms=100)
        self.assertFalse(result["settled"])
        self.assertLessEqual(result["elapsed_ms"], 1000)

    def test_quiet_window_must_hold(self) -> None:
        still = [[node("n1", text="x")]]
        result = self._run(still, timeout_ms=5000, quiet_ms=500, interval_ms=100)
        self.assertTrue(result["settled"])
        self.assertGreaterEqual(result["elapsed_ms"], 500)


class OutlineTests(unittest.TestCase):
    def test_one_line_per_node_indented_by_depth(self) -> None:
        nodes = [
            node("n0", role="window", bounds=[0, 0, 1080, 1920], depth=1),
            node("n1", role="button", text="Save", resource_id="com.x:id/save",
                 bounds=[10, 20, 110, 70], clickable=True, depth=2),
            node("n2", role="text", desc='Say "hi"', enabled=False, depth=3),
        ]
        lines = ui.outline(nodes).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "n0 window @0,0 1080x1920")
        self.assertEqual(lines[1], '  n1 button "Save" id=com.x:id/save @10,20 100x50 [clickable]')
        self.assertEqual(lines[2], '    n2 text "Say \\"hi\\"" @0,0 10x10 [disabled]')

    def test_interactable_filter(self) -> None:
        nodes = [node("n1", role="button", clickable=True),
                 node("n2", role="text"),
                 node("n3", role="button", clickable=True, enabled=False)]
        self.assertEqual([n["ref"] for n in ui.interactable(nodes)], ["n1"])
        self.assertEqual(ui.outline(nodes, interactable_only=True).count("\n"), 0)


class SelectorTests(unittest.TestCase):
    def test_find_without_a_selector_is_selector_required(self) -> None:
        """Before: matched every node and raised ambiguous_selector."""
        xml = (ROOT / "tests/fixtures/ui_dump.xml").read_text(encoding="utf-8")
        with self.assertRaises(errors.AutonomError) as caught:
            ui.find_nodes(xml)
        self.assertEqual(caught.exception.code, errors.SELECTOR_REQUIRED)

    def test_ios_text_miss_that_matches_desc_hints_desc(self) -> None:
        nodes = [node("n1", role="button", desc="Continue")]
        error = ui.no_match_error("ios", nodes, {"text": "Continue"})
        self.assertEqual(error.code, errors.NO_MATCHING_NODE)
        self.assertIn("--desc", error.hint)
        self.assertIn("--desc", ui.no_match_hint("android", nodes, {"text": "Continue"}))
        self.assertIsNone(ui.no_match_hint("ios", nodes, {"text": "Elsewhere"}))

    def test_clip_is_exact_at_the_limit(self) -> None:
        """Before: a screen of exactly max_nodes nodes read as truncated."""
        three = [node(f"n{i}") for i in range(3)]
        self.assertEqual(ui.clip(three, 3), (three, False))
        clipped, truncated = ui.clip(three + [node("n3")], 3)
        self.assertEqual((len(clipped), truncated), (3, True))

    def test_tree_clipped_asks_for_one_more(self) -> None:
        four = [node(f"n{i}") for i in range(4)]
        with mock.patch.object(ui, "tree", return_value=(four, [])) as tree:
            nodes, _, truncated = ui.tree_clipped(ANDROID, max_nodes=3)
        self.assertEqual(tree.call_args.kwargs["max_nodes"], 4)
        self.assertTrue(truncated)
        self.assertEqual(len(nodes), 3)


# --- logs / follow ------------------------------------------------------------


class GrepTests(unittest.TestCase):
    def test_logs_tail_invalid_regex_is_invalid_value(self) -> None:
        """Before: a raw re.error traceback."""
        fake = scripted()
        with mock.patch.object(adb_mod, "run_adb", fake):
            with self.assertRaises(errors.AutonomError) as caught:
                logs.tail(ANDROID, grep="(", since_seconds=0)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(fake.calls, [], "refused before touching the device")
        with self.assertRaises(errors.AutonomError) as caught:
            logs.tail(IOS, grep="[", stream_path=Path("/nonexistent/stream.ndjson"))
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)

    def test_follow_invalid_regex_keeps_its_compatible_code(self) -> None:
        """`logs follow` keeps backend_failed (a pinned contract), with a hint."""
        with self.assertRaises(errors.AutonomError) as caught:
            follow.follow_file(Path("/nonexistent/x.log"), source="t", emit=lambda _: None,
                               grep="(", max_seconds=1)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertIn("backslash", caught.exception.hint)


class FollowSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "logs").mkdir()
        (self.base / "journal.ndjson").write_text("", encoding="utf-8")
        self.record = {"artifacts_dir": str(self.base), "streams": []}

    def test_unknown_source_lists_device(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            follow.resolve_source(self.record, "nope")
        self.assertIn("device", caught.exception.message)
        self.assertIn("journal", caught.exception.message)

    def test_single_device_stream_is_the_default(self) -> None:
        self.assertIsNone(follow.default_source(self.record))
        self.record["streams"] = [{"id": "log_stream", "kind": "device_log",
                                   "path": "logs/stream.ndjson"}]
        self.assertEqual(follow.default_source(self.record), "log_stream")
        self.record["streams"].append({"id": "second", "kind": "device_log",
                                       "path": "logs/second.ndjson"})
        self.assertIsNone(follow.default_source(self.record))


# --- CLI-008: emulator ownership ---------------------------------------------


class EmulatorOwnershipTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        home = self.sandbox_home()
        # `cleanup(include_live=True)` below must never reach another test's
        # proxy: machine-wide signature discovery sees only this home's.
        scope_proxy_discovery(self, processes, home)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state.json"
        self.state.write_text(json.dumps({"devices": []}), encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(Path(tmp.name) / "log.jsonl"))

    def _live_child(self) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

        def reap() -> None:
            child.kill()
            child.wait()

        self.addCleanup(reap)
        return child

    def test_booted_emulator_is_harness_owned_not_an_orphan(self) -> None:
        """Before: registered without artifacts_dir, so scan() called it an
        orphan and `cleanup` would have killed it under a running session."""
        child = self._live_child()
        processes.register("emulator", child.pid, avd="Pixel_9",
                           owner=processes.HARNESS_OWNER)
        state = processes.scan()
        self.assertEqual(state["orphans"], [])
        self.assertEqual([entry["pid"] for entry in state["harness"]], [child.pid])
        self.assertEqual(state["live"], [], "live is read as session-owned")
        result = processes.cleanup(include_live=True)
        self.assertEqual(result["actions"], [])
        self.assertIsNone(child.poll(), "cleanup must never kill a booted emulator")

    def test_pre_owner_emulator_entry_is_not_an_orphan(self) -> None:
        child = self._live_child()
        processes.register("emulator", child.pid, avd="Pixel_9")
        self.assertEqual(processes.scan()["orphans"], [])

    def test_boot_records_owner_and_serial_and_shutdown_releases_it(self) -> None:
        detail = emulator.boot_avd(FAKE_EMULATOR, FAKE_ADB, "Pixel_9", timeout=30)
        self.assertTrue(detail["booted"])
        (entry,) = processes.entries()
        self.assertEqual(entry["owner"], processes.HARNESS_OWNER)
        self.assertEqual(entry["serial"], "emulator-5556")
        shutdown = emulator.kill_emulator(FAKE_ADB, "emulator-5556", timeout=5)
        self.assertTrue(shutdown["gone"])
        self.assertEqual(shutdown["registry_released"], [entry["pid"]])
        self.assertEqual(processes.entries(), [])
        self.assertEqual(processes.scan()["stale_entries"], [])

    def test_shutdown_releases_an_entry_booted_without_wait(self) -> None:
        processes.register("emulator", 999_999, avd="Pixel_9",
                           owner=processes.HARNESS_OWNER)
        processes.register("proxy", 999_998, artifacts_dir="/nowhere")
        self.state.write_text(json.dumps({
            "devices": [["emulator-5556", "device", ""]],
            "avd_names": {"emulator-5556": "Pixel_9"},
        }), encoding="utf-8")
        emulator.kill_emulator(FAKE_ADB, "emulator-5556", timeout=5)
        self.assertEqual([entry["kind"] for entry in processes.entries()], ["proxy"])


if __name__ == "__main__":
    unittest.main()
