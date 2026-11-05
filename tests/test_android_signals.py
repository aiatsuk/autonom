"""TEST-006: keyboard and dialog signals of Android reads (AS-01, AS-02, RISK-012).

The signal rules run on fixtures (`tests/fixtures/android_helper/`): the
runtime permission dialog, a framework alert (Settings' Force stop), an app's
Compose dialog in its own window over the app's, Settings' modal Compose
"Force stop?" dialog of the 2026-10-25 live run (`force_stop_compose_dialog.xml`:
only the dialog's window is listed, beside the system bars), and screens with
no dialog, each with the windows list the helper reports for it
(`signals_windows.json`) and the read's display size. The CLI
cases run `ui tree`, an observed tap and `ui do` in this process against
`tests/fakes/fake_adb.py` and `tests/fakes/fake_android_helper.py`; no device,
no javac or d8.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FIXTURES = ROOT / "tests/fixtures/android_helper"
HOME_XML = (FIXTURES / "settings_home.xml").read_text(encoding="utf-8")
HOME_DUMP = FIXTURES / "settings_home_uiautomator.txt"
PERMISSION_XML = (FIXTURES / "permission_dialog.xml").read_text(encoding="utf-8")
ALERT_XML = (FIXTURES / "alert_dialog.xml").read_text(encoding="utf-8")
COMPOSE_XML = (FIXTURES / "compose_dialog.xml").read_text(encoding="utf-8")
# Settings' "Force stop?" dialog as the helper read it on 2026-10-25 (API 36):
# Compose, no resource ids, each clickable button holds its text and a
# label-less `android.widget.Button` node side by side
FORCE_STOP_XML = (FIXTURES / "force_stop_compose_dialog.xml").read_text(encoding="utf-8")
WINDOWS = json.loads((FIXTURES / "signals_windows.json").read_text(encoding="utf-8"))
DISPLAY = {"width": 1080, "height": 2400, "rotation": 0}
FORCE_STOP_DIALOG = {"kind": "window", "package": "com.android.settings", "title": None,
                     "buttons": [{"label": "Cancel", "resource_id": None},
                                 {"label": "OK", "resource_id": None}]}
SERIAL = "emulator-5612"
BUILD = "0123456789abcdef"
DUMP = ["exec-out", "uiautomator", "dump", "/dev/tty"]
KEYBOARD = "com.google.android.inputmethod.latin"
# two sensitive values of one `ui do` batch (twelve characters each)
FIRST, SECOND = "aaaa11112222", "bbbbcccc9999"

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/fakes"))
from autonom_lib import android_helper as helper_mod  # noqa: E402
from autonom_lib import android_helper_build as build_mod  # noqa: E402
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import observe as observe_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui, ui_android  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402
import fake_android_helper  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def signals(xml: str, windows: Any = None, display: Any = None) -> dict[str, Any]:
    return ui_android.screen_signals(xml, windows, display)


def window(kind: str, bounds: list[int], *, active: bool = False, layer: int = 0,
           package: str | None = "com.example.app", title: str | None = None) -> dict[str, Any]:
    return {"type": kind, "layer": layer, "active": active, "focused": active,
            "title": title, "package": package, "bounds": bounds}


STATUS_BAR = window("system", [0, 0, 1080, 136], layer=1, package="com.android.systemui")
NAVIGATION_BAR = window("system", [0, 2337, 1080, 2400], layer=1, package="com.android.systemui")


def hierarchy(*nodes: str) -> str:
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
            f'<hierarchy rotation="0">{"".join(nodes)}</hierarchy>')


def node(text: str = "", rid: str = "", cls: str = "android.widget.TextView",
         pkg: str = "com.example.app", bounds: str = "[0,0][100,100]",
         clickable: bool = False, children: str = "") -> str:
    head = (f'<node index="0" text="{text}" resource-id="{rid}" class="{cls}" '
            f'package="{pkg}" content-desc="" checkable="false" checked="false" '
            f'clickable="{str(clickable).lower()}" enabled="true" focusable="false" '
            f'focused="false" scrollable="false" long-clickable="false" password="false" '
            f'selected="false" bounds="{bounds}"')
    return head + (f">{children}</node>" if children else " />")


class PermissionDialogTests(unittest.TestCase):
    """AS-02-S01: the runtime permission dialog is `permission`, with its
    Allow and Deny buttons."""

    def test_the_permission_fixture_is_a_permission_dialog_with_its_buttons(self) -> None:
        dialog = signals(PERMISSION_XML, WINDOWS["permission"])["dialog"]
        self.assertEqual(dialog["kind"], "permission")
        self.assertEqual(dialog["package"], "com.google.android.permissioncontroller")
        self.assertEqual(dialog["title"], "Allow Camera to take pictures and record video?")
        labels = [button["label"] for button in dialog["buttons"]]
        self.assertEqual(labels, ["While using the app", "Only this time", "Don’t allow"])
        ids = [button["resource_id"] for button in dialog["buttons"]]
        self.assertTrue(any(":id/permission_allow" in rid for rid in ids), "an Allow button")
        self.assertTrue(any(":id/permission_deny" in rid for rid in ids), "a Deny button")

    def test_the_tree_alone_finds_it_on_a_uiautomator_read(self) -> None:
        found = signals(PERMISSION_XML)
        self.assertIsNone(found["keyboard"], "uiautomator gives no windows: unknown")
        self.assertEqual(found["dialog"]["kind"], "permission")
        self.assertEqual(len(found["dialog"]["buttons"]), 3)

    def test_an_allow_or_deny_id_names_it_in_another_package(self) -> None:
        xml = hierarchy(node(cls="android.widget.FrameLayout", pkg="com.vendor.permissions",
                             bounds="[0,0][1080,2400]", children=(
                                 node("Allow", "com.vendor.permissions:id/permission_allow_button",
                                      "android.widget.Button", "com.vendor.permissions",
                                      "[100,1000][500,1100]", clickable=True)
                                 + node("Deny", "com.vendor.permissions:id/permission_deny_button",
                                        "android.widget.Button", "com.vendor.permissions",
                                        "[500,1000][900,1100]", clickable=True))))
        dialog = signals(xml)["dialog"]
        self.assertEqual((dialog["kind"], dialog["package"]),
                         ("permission", "com.vendor.permissions"))
        self.assertEqual([b["label"] for b in dialog["buttons"]], ["Allow", "Deny"])
        self.assertIsNone(dialog["title"])

    def test_a_permission_window_names_it_when_the_read_is_of_the_app(self) -> None:
        dialog = signals(HOME_XML, WINDOWS["permission"])["dialog"]
        self.assertEqual(dialog["kind"], "permission")
        self.assertEqual(dialog["package"], "com.google.android.permissioncontroller")
        self.assertEqual(dialog["title"], "Permission request", "the window's title")
        self.assertEqual(dialog["buttons"], [], "none of its buttons are in this read")


class AlertDialogTests(unittest.TestCase):
    def test_the_force_stop_alert_is_an_alert_with_its_buttons(self) -> None:
        for windows in (WINDOWS["alert"], None):
            with self.subTest(windows=windows is not None):
                dialog = signals(ALERT_XML, windows)["dialog"]
                self.assertEqual(dialog, {
                    "kind": "alert", "package": "com.android.settings", "title": "Force stop?",
                    "buttons": [{"label": "Cancel", "resource_id": "android:id/button2"},
                                {"label": "OK", "resource_id": "android:id/button1"}]})

    def test_the_framework_title_alone_is_an_alert(self) -> None:
        xml = hierarchy(node("Delete?", "android:id/alertTitle", pkg="com.example.app"))
        dialog = signals(xml)["dialog"]
        self.assertEqual((dialog["kind"], dialog["title"], dialog["buttons"]),
                         ("alert", "Delete?", []))

    def test_an_app_namespace_title_without_framework_ids_is_no_alert(self) -> None:
        xml = hierarchy(node("Header", "com.example.app:id/alertTitle"))
        self.assertIsNone(signals(xml)["dialog"])

    def test_buttons_are_capped(self) -> None:
        buttons = "".join(node(f"B{i}", f"android:id/button{1 + i % 3}",
                               "android.widget.Button", clickable=True) for i in range(12))
        dialog = signals(hierarchy(node(cls="android.widget.FrameLayout",
                                        children=buttons)))["dialog"]
        self.assertEqual(len(dialog["buttons"]), ui_android.SIGNAL_BUTTONS_MAX)


class WindowDialogTests(unittest.TestCase):
    def test_an_app_window_over_the_app_is_a_window_dialog(self) -> None:
        dialog = signals(COMPOSE_XML, WINDOWS["compose_dialog"])["dialog"]
        self.assertEqual(dialog, {
            "kind": "window", "package": "com.example.app", "title": None,
            "buttons": [{"label": "Cancel", "resource_id": None},
                        {"label": "Delete", "resource_id": "com.example.app:id/confirm"}]})

    def test_without_windows_a_compose_dialog_cannot_be_seen(self) -> None:
        self.assertIsNone(signals(COMPOSE_XML)["dialog"])

    def test_split_screen_is_no_dialog(self) -> None:
        self.assertIsNone(signals(HOME_XML, WINDOWS["split_screen"])["dialog"])

    def test_two_app_windows_side_by_side_are_no_dialog(self) -> None:
        apart = [{"type": "application", "layer": 1, "package": "a", "bounds": [0, 0, 540, 2400]},
                 {"type": "application", "layer": 0, "package": "b",
                  "bounds": [540, 0, 1080, 2400]}]
        self.assertIsNone(signals(HOME_XML, apart)["dialog"])

    def test_the_layer_decides_which_window_is_on_top(self) -> None:
        listed = list(reversed(WINDOWS["compose_dialog"]))  # bottom first
        dialog = signals(COMPOSE_XML, listed)["dialog"]
        self.assertEqual(dialog["kind"], "window")
        self.assertEqual([b["label"] for b in dialog["buttons"]], ["Cancel", "Delete"])

    def test_a_permission_dialog_is_reported_as_one_not_as_a_window(self) -> None:
        self.assertEqual(signals(PERMISSION_XML, WINDOWS["permission"])["dialog"]["kind"],
                         "permission")


class ModalDialogTests(unittest.TestCase):
    """AS-02: a modal dialog leaves the app window beneath it out of the
    windows list; it is seen from the active window leaving the screen
    partly uncovered (CASE-003 s08, 2026-10-25)."""

    def test_the_force_stop_compose_dialog_is_a_window_dialog_with_its_buttons(self) -> None:
        found = signals(FORCE_STOP_XML, WINDOWS["modal_dialog"], DISPLAY)
        self.assertEqual(found["dialog"], FORCE_STOP_DIALOG)
        self.assertFalse(found["keyboard"]["visible"])

    def test_a_uiautomator_read_of_it_stays_as_today(self) -> None:
        self.assertEqual(signals(FORCE_STOP_XML), {"keyboard": None, "dialog": None})

    def test_without_a_usable_display_size_it_is_not_judged(self) -> None:
        for display in (None, {}, {"width": 0, "height": 2400}, {"width": True, "height": 2400},
                        [1080, 2400]):
            with self.subTest(display=display):
                self.assertIsNone(
                    signals(FORCE_STOP_XML, WINDOWS["modal_dialog"], display)["dialog"])

    def test_compose_buttons_beside_their_text_are_reported_by_their_container(self) -> None:
        nodes = ui_android.parse_all(FORCE_STOP_XML)
        labels = [b["label"] for b in ui_android._buttons(nodes, lambda node: True)]  # noqa: SLF001
        self.assertEqual(labels, ["Cancel", "OK"])

    def test_the_dialog_is_seen_with_the_keyboard_shown(self) -> None:
        listed = [window("input_method", [0, 1580, 1080, 2337], layer=2,
                         package="com.google.android.inputmethod.latin"),
                  *WINDOWS["modal_dialog"]]
        found = signals(FORCE_STOP_XML, listed, DISPLAY)
        self.assertTrue(found["keyboard"]["visible"])
        self.assertEqual(found["dialog"], FORCE_STOP_DIALOG)

    def test_with_no_active_window_the_topmost_application_window_is_taken(self) -> None:
        listed = [dict(item, active=False, focused=False) for item in WINDOWS["modal_dialog"]]
        self.assertEqual(signals(FORCE_STOP_XML, listed, DISPLAY)["dialog"], FORCE_STOP_DIALOG)

    def test_an_active_window_that_is_no_application_window_is_no_dialog(self) -> None:
        shade = window("system", [0, 0, 1080, 1200], active=True, layer=3,
                       package="com.android.systemui")
        listed = [shade] + [dict(item, active=False) for item in WINDOWS["modal_dialog"]]
        self.assertIsNone(signals(FORCE_STOP_XML, listed, DISPLAY)["dialog"])
        broken = [STATUS_BAR, NAVIGATION_BAR,
                  window("application", [0, 987, 1080, 987], active=True)]
        self.assertIsNone(signals(FORCE_STOP_XML, broken, DISPLAY)["dialog"])

    def test_the_limit_is_ten_percent_of_the_display_uncovered(self) -> None:
        self.assertEqual(ui_android.DIALOG_UNCOVERED_MIN_PERCENT, 10)
        # the bars cover rows 0-136 and 2337-2400; 240 rows of 2400 are 10%
        at_limit = [STATUS_BAR, NAVIGATION_BAR,
                    window("application", [0, 136, 1080, 2097], active=True)]
        under = [STATUS_BAR, NAVIGATION_BAR,
                 window("application", [0, 136, 1080, 2098], active=True)]
        self.assertEqual(ui_android.uncovered_area(at_limit, 1080, 2400), 1080 * 240)
        self.assertEqual(signals(HOME_XML, at_limit, DISPLAY)["dialog"]["kind"], "window")
        self.assertIsNone(signals(HOME_XML, under, DISPLAY)["dialog"])

    def test_landscape_uses_the_display_as_it_is_turned(self) -> None:
        turned = {"width": 2400, "height": 1080, "rotation": 1}
        bars = [window("system", [0, 0, 2400, 63], layer=1, package="com.android.systemui"),
                window("system", [0, 1017, 2400, 1080], layer=1, package="com.android.systemui")]
        dialog = bars + [window("application", [600, 300, 1800, 800], active=True)]
        self.assertEqual(signals(HOME_XML, dialog, turned)["dialog"]["kind"], "window")
        # an app kept out of a 136-pixel display cutout: about 5%, no dialog
        cutout = bars + [window("application", [136, 63, 2400, 1017], active=True)]
        self.assertIsNone(signals(HOME_XML, cutout, turned)["dialog"])

    def test_windows_past_the_screen_are_clipped_and_overlaps_count_once(self) -> None:
        rects = [window("application", [-100, -100, 1180, 1300]),
                 window("system", [0, 1200, 1080, 2500]), window("system", [0, 1000, 1080, 1400])]
        self.assertEqual(ui_android.uncovered_area(rects, 1080, 2400), 0)
        self.assertEqual(ui_android.uncovered_area([window("system", [0, 0, 1080, 1200]),
                                                    window("system", [0, 600, 1080, 1800])],
                                                   1080, 2400), 1080 * 600)

    def test_broken_window_entries_with_a_display_are_skipped(self) -> None:
        broken = ["x", None, {"type": "application", "active": True, "bounds": [0, 0, "a", 10]},
                  {"type": "application", "bounds": [0, 0, True, 10]},
                  {"type": "application", "bounds": None}, {"bounds": [0, 0, 1080, 300]}]
        self.assertIsNone(signals(FORCE_STOP_XML, broken, DISPLAY)["dialog"])
        self.assertIsNone(signals(FORCE_STOP_XML, {"windows": "nope"}, DISPLAY)["dialog"])
        # an unreadable entry beside the real list changes nothing
        self.assertEqual(signals(FORCE_STOP_XML, broken[:2] + WINDOWS["modal_dialog"],
                                 DISPLAY)["dialog"], FORCE_STOP_DIALOG)

    def test_too_many_windows_are_not_judged(self) -> None:
        many = [window("system", [i, 0, i + 1, 1]) for i in range(ui_android.SIGNAL_WINDOWS_MAX + 1)]
        self.assertIsNone(ui_android.uncovered_area(many, 1080, 2400))
        listed = [*WINDOWS["modal_dialog"], *many]
        self.assertIsNone(signals(FORCE_STOP_XML, listed, DISPLAY)["dialog"])

    # -- where it must not fire

    def test_a_full_screen_app_with_the_bars_is_no_dialog(self) -> None:
        for name in ("home", "app_clipped"):
            with self.subTest(windows=name):
                self.assertIsNone(signals(HOME_XML, WINDOWS[name], DISPLAY)["dialog"])

    def test_an_app_with_the_keyboard_shown_is_no_dialog(self) -> None:
        for name in ("keyboard", "keyboard_app_clipped"):
            with self.subTest(windows=name):
                found = signals(HOME_XML, WINDOWS[name], DISPLAY)
                self.assertTrue(found["keyboard"]["visible"])
                self.assertIsNone(found["dialog"])

    def test_the_launcher_is_no_dialog(self) -> None:
        self.assertIsNone(signals(HOME_XML, WINDOWS["launcher"], DISPLAY)["dialog"])

    def test_a_picture_in_picture_window_over_an_app_adds_no_dialog(self) -> None:
        listed = WINDOWS["picture_in_picture"]
        nodes = ui_android.parse_all(HOME_XML)
        self.assertIsNone(ui_android._modal_window_dialog(nodes, listed, DISPLAY))  # noqa: SLF001
        # what the two-overlapping-windows rule says of it is unchanged
        self.assertEqual(signals(HOME_XML, listed, DISPLAY)["dialog"],
                         signals(HOME_XML, listed)["dialog"])

    def test_split_screen_is_no_dialog(self) -> None:
        self.assertIsNone(signals(HOME_XML, WINDOWS["split_screen"], DISPLAY)["dialog"])
        # the divider decides, even where the listed windows leave a gap
        gap = [STATUS_BAR, NAVIGATION_BAR,
               window("split_screen_divider", [0, 1180, 1080, 1220], layer=1, package=None),
               window("application", [0, 136, 1080, 1000], active=True),
               window("application", [0, 1400, 1080, 2337], package="com.android.settings")]
        self.assertGreater(ui_android.uncovered_area(gap, 1080, 2400), 1080 * 240)
        self.assertIsNone(signals(HOME_XML, gap, DISPLAY)["dialog"])
        # stacked apps with no divider listed cover the screen
        stacked = [STATUS_BAR, NAVIGATION_BAR,
                   window("application", [0, 136, 1080, 1200], active=True),
                   window("application", [0, 1200, 1080, 2337],
                          package="com.android.settings")]
        self.assertIsNone(signals(HOME_XML, stacked, DISPLAY)["dialog"])


class NoDialogTests(unittest.TestCase):
    def test_a_plain_screen_has_no_dialog_and_no_keyboard(self) -> None:
        self.assertEqual(signals(HOME_XML, WINDOWS["home"]),
                         {"keyboard": {"visible": False, "package": None, "bounds": None},
                          "dialog": None})

    def test_the_uiautomator_dump_of_it_says_keyboard_unknown(self) -> None:
        self.assertEqual(signals(HOME_DUMP.read_text(encoding="utf-8")),
                         {"keyboard": None, "dialog": None})

    def test_an_unparsable_read_says_no_dialog(self) -> None:
        self.assertEqual(signals("<hierarchy><node", WINDOWS["home"])["dialog"], None)
        self.assertEqual(signals("", None), {"keyboard": None, "dialog": None})

    def test_broken_window_entries_are_skipped(self) -> None:
        listed = ["x", None, {"type": "application", "bounds": "nope"},
                  {"type": "input_method", "bounds": [0, 0, 0, 0]},
                  {"type": "application", "layer": True, "bounds": [0, 0, 10, 10]}]
        self.assertEqual(signals(HOME_XML, listed),
                         {"keyboard": {"visible": False, "package": None, "bounds": None},
                          "dialog": None})


class KeyboardTests(unittest.TestCase):
    def test_an_input_method_window_is_a_visible_keyboard(self) -> None:  # AS-01-S01
        self.assertEqual(signals(HOME_XML, WINDOWS["keyboard"])["keyboard"],
                         {"visible": True, "package": KEYBOARD, "bounds": [0, 1580, 1080, 2337]})

    def test_a_window_known_only_by_its_type_number_counts(self) -> None:
        listed = [{"type_id": 2, "package": KEYBOARD, "bounds": [0, 1500, 1080, 2400]}]
        self.assertTrue(signals(HOME_XML, listed)["keyboard"]["visible"])

    def test_an_input_method_window_without_area_is_hidden(self) -> None:
        listed = [{"type": "input_method", "package": KEYBOARD, "bounds": [0, 2400, 1080, 2400]}]
        self.assertFalse(signals(HOME_XML, listed)["keyboard"]["visible"])


class MaskingTests(unittest.TestCase):
    def test_a_title_or_label_that_shows_the_typed_value_is_masked(self) -> None:
        found = {"keyboard": None, "dialog": {
            "kind": "alert", "package": "p", "title": "Send 4111 1111 1111 1111?",
            "buttons": [{"label": "4111111111111111", "resource_id": "android:id/button1"},
                        {"label": "Cancel", "resource_id": "android:id/button2"}]}}
        masked = observe_mod.masked_signals(found, "4111111111111111")
        self.assertEqual(masked["dialog"]["title"], "<16 chars>")
        self.assertEqual([b["label"] for b in masked["dialog"]["buttons"]],
                         ["<16 chars>", "Cancel"])
        self.assertEqual(found["dialog"]["title"], "Send 4111 1111 1111 1111?", "not mutated")
        self.assertIs(observe_mod.masked_signals(found, None), found)

    def test_every_value_of_a_sensitive_batch_is_masked_not_only_the_first(self) -> None:
        from autonom_lib.flow import inline as inline_mod

        found = {"keyboard": None, "dialog": {
            "kind": "alert", "package": "p", "title": f"Send to {SECOND}?",
            "buttons": [{"label": f"Pay {FIRST}", "resource_id": "android:id/button1"},
                        {"label": "Cancel", "resource_id": "android:id/button2"},
                        {"label": None, "resource_id": "android:id/button3"}]}}
        with inline_mod.masking([FIRST, SECOND]) as secret:
            self.assertEqual(secret, FIRST, "observe is given the first value only")
            masked = observe_mod.masked_signals(found, secret)
        self.assertEqual(masked["dialog"]["title"], "<12 chars>")
        self.assertEqual([b["label"] for b in masked["dialog"]["buttons"]],
                         ["<12 chars>", "Cancel", None])
        self.assertEqual([b["resource_id"] for b in masked["dialog"]["buttons"]],
                         ["android:id/button1", "android:id/button2", "android:id/button3"])
        self.assertNotIn(FIRST, json.dumps(masked))
        self.assertNotIn(SECOND, json.dumps(masked))
        # the mask is put back when the batch closes: one value masks itself only
        alone = observe_mod.masked_signals(found, FIRST)
        self.assertEqual(alone["dialog"]["title"], f"Send to {SECOND}?")
        self.assertEqual(alone["dialog"]["buttons"][0]["label"], "<12 chars>")


class SignalsCase(EnvSandboxMixin, unittest.TestCase):
    """A private state directory, the fake adb and a fake helper; the backend
    left to auto (the harness default is uiautomator)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "adb-state.json"
        self.log = self.root / "adb-log.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        (self.root / "user").mkdir()
        self.set_env(AUTONOM_HOME=str(self.root / "state"), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), PATH=str(bin_dir),
                     HOME=str(self.root / "user"), JAVA_HOME=None, ANDROID_HOME=None,
                     ANDROID_SDK_ROOT=None, AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_UI_BACKEND=None, AUTONOM_ANDROID_UI_BACKEND=None,
                     AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None)
        self.helper = fake_android_helper.FakeHelper(state_path=self.state, xml=HOME_XML).start()
        self.addCleanup(self.helper.close)
        self.helper.windows = WINDOWS["home"]
        self.state.write_text(json.dumps({
            "devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
            "helper_port": self.helper.port, "ui_dump": str(HOME_DUMP),
            "wm_size": "Physical size: 1080x2400", "device_files": ["autonom-helper.log"]}),
            encoding="utf-8")
        self.target = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        dex = self.root / "classes.dex"
        dex.write_bytes(b"dex\n035\0fake")
        patch = mock.patch.object(helper_mod.build_mod, "ensure",
                                  return_value=build_mod.Build(dex, BUILD, None, None))
        self.built = patch.start()
        self.addCleanup(patch.stop)
        ui_android.set_preference(None)
        self.addCleanup(ui_android.set_preference, None)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        self.cli_module: Any = None

    def no_tools(self) -> None:
        self.built.return_value = build_mod.Build(
            None, None, "javac not found", "Install a JDK (17 or newer) and put javac on PATH.")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"][2:] if row["argv"][:1] == ["-s"] else row["argv"] for row in rows]

    def inputs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:2] == ["shell", "input"]]

    def ops(self) -> list[str]:
        return [item["op"] for item in self.helper.ops() if item["op"] != "hello"]

    def cli(self, *argv: str, stdin: str | None = None) -> tuple[int, dict[str, Any]]:
        if self.cli_module is None:
            spec = importlib.util.spec_from_file_location("autonom_cli_signals", CLI)
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
            code = self.cli_module.main(["--adb", str(FAKE_ADB), "--serial", SERIAL, *argv])
        stream = out.getvalue() if out.getvalue().strip() else err.getvalue()
        self.assertNotIn("Traceback", stream)
        return code, json.loads(stream.strip().splitlines()[-1]) if stream.strip() else {}

    def start_session(self, *extra: str) -> dict[str, Any]:
        code, payload = self.cli("session", "start", "--app-id", "com.example.app", *extra)
        self.assertEqual(code, 0, payload)
        return payload["session"]


class TreeSignalTests(SignalsCase):
    def test_keyboard_up_through_the_helper(self) -> None:  # AS-01-S01
        self.helper.windows = WINDOWS["keyboard"]
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertEqual(payload["signals"]["keyboard"],
                         {"visible": True, "package": KEYBOARD, "bounds": [0, 1580, 1080, 2337]})
        self.assertIsNone(payload["signals"]["dialog"])
        # and down again
        self.helper.windows = WINDOWS["home"]
        code, payload = self.cli("ui", "tree")
        self.assertFalse(payload["signals"]["keyboard"]["visible"])

    def test_a_permission_dialog_is_reported_and_nothing_is_clicked(self) -> None:  # AS-02-S01
        self.helper.xml = PERMISSION_XML
        self.helper.windows = WINDOWS["permission"]
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        dialog = payload["signals"]["dialog"]
        self.assertEqual(dialog["kind"], "permission")
        self.assertIn("While using the app", [b["label"] for b in dialog["buttons"]])
        self.assertIn("Don’t allow", [b["label"] for b in dialog["buttons"]])
        self.assertEqual(self.ops(), ["read"], "one read, no input to the helper")
        self.assertEqual(self.inputs(), [], "no adb input")

    def test_a_modal_compose_dialog_is_a_window_dialog(self) -> None:  # AS-02, CASE-003 s08
        self.helper.xml = FORCE_STOP_XML
        self.helper.windows = WINDOWS["modal_dialog"]
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["ui_backend"], "helper")
        self.assertEqual(payload["signals"]["dialog"], FORCE_STOP_DIALOG)
        self.assertEqual(self.ops(), ["read"], "one read, no input to the helper")
        self.assertEqual(self.inputs(), [], "no adb input")

    def test_the_saved_tree_carries_the_signals(self) -> None:
        self.start_session()
        self.helper.xml = ALERT_XML
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        saved = json.loads(Path(payload["saved"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["signals"], payload["signals"])
        self.assertEqual(saved["signals"]["dialog"]["kind"], "alert")

    def test_an_agent_outline_carries_the_signals_too(self) -> None:
        self.start_session("--profile", "agent")
        self.helper.windows = WINDOWS["keyboard"]
        code, payload = self.cli("ui", "tree")
        self.assertEqual(payload["format"], "outline")
        self.assertTrue(payload["signals"]["keyboard"]["visible"])

    def test_an_auto_fallback_read_has_keyboard_null_and_asks_nothing_more(self) -> None:
        self.no_tools()
        self.state.write_text(json.dumps({**json.loads(self.state.read_text()),
                                          "ui_dump": str(FIXTURES / "alert_dialog.xml")}))
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["fallback_reason"], "javac not found")
        self.assertIsNone(payload["signals"]["keyboard"])
        self.assertEqual(payload["signals"]["dialog"]["kind"], "alert", "from the tree alone")
        fallback_calls = self.calls()
        self.assertIn(DUMP, fallback_calls)
        self.assertFalse([call for call in fallback_calls if "input_method" in " ".join(call)])
        # the same adb calls as the explicit uiautomator choice makes
        self.log.unlink()
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        code, opted_out = self.cli("ui", "tree")
        self.assertEqual(code, 0, opted_out)
        self.assertEqual(self.calls(), fallback_calls)

    def test_the_explicit_uiautomator_choice_keeps_todays_reply(self) -> None:  # DEC-018
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        self.state.write_text(json.dumps({**json.loads(self.state.read_text()),
                                          "ui_dump": str(FIXTURES / "alert_dialog.xml")}))
        code, payload = self.cli("ui", "tree")
        self.assertEqual(code, 0, payload)
        for key in ("signals", "ui_backend", "settled", "fallback_reason"):
            self.assertNotIn(key, payload)
        self.assertEqual(self.helper.ops(), [], "no helper contacted")

    def test_ui_find_adds_no_signals(self) -> None:
        self.helper.windows = WINDOWS["keyboard"]
        code, payload = self.cli("ui", "find", "--text", "Battery")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("signals", payload)

    def test_read_signals_is_empty_off_android_and_before_a_read(self) -> None:
        ios = Target("ios", "AAAAAAAA-1111-2222-3333-CCCCCCCCCCCC", "xcrun", {})
        self.assertEqual(ui.read_signals(ios), {})
        self.assertEqual(ui.read_signals(self.target), {})
        ui.snapshot(self.target)
        self.assertEqual(ui.read_signals(self.target)["signals"]["dialog"], None)


class ObservedSignalTests(SignalsCase):
    def test_an_observed_tap_reports_the_dialog_it_opened(self) -> None:
        self.helper.xml_after = ALERT_XML
        self.helper.windows = WINDOWS["alert"]
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700", "--observe")
        self.assertEqual(code, 0, payload)
        # the guard's one display lookup, then the tap that carried the screen
        self.assertEqual(self.ops(), ["display", "tap"], "no read of observe's own")
        dialog = payload["screen"]["signals"]["dialog"]
        self.assertEqual((dialog["kind"], dialog["title"]), ("alert", "Force stop?"))
        self.assertFalse(payload["screen"]["signals"]["keyboard"]["visible"])
        self.assertEqual(self.inputs(), [])

    def test_an_observed_tap_reports_the_modal_compose_dialog_it_opened(self) -> None:
        # the read after the tap carries its own display: the rule uses it
        self.helper.xml_after = FORCE_STOP_XML
        self.helper.windows = WINDOWS["modal_dialog"]
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.ops(), ["display", "tap"], "no read of observe's own")
        self.assertEqual(payload["screen"]["signals"]["dialog"], FORCE_STOP_DIALOG)
        self.assertEqual(self.inputs(), [])

    def test_an_observed_tap_in_a_session_saves_the_signals(self) -> None:
        self.start_session("--profile", "agent")
        self.helper.xml_after = PERMISSION_XML
        self.helper.windows = WINDOWS["permission"]
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["screen"]["signals"]["dialog"]["kind"], "permission")
        saved = json.loads(Path(payload["screen"]["saved"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["signals"], payload["screen"]["signals"])

    def test_an_observed_tap_under_the_explicit_choice_has_no_signals(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        code, payload = self.cli("ui", "tap", "--x", "540", "--y", "700", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertIn("screen", payload)
        self.assertNotIn("signals", payload["screen"])

    def test_ui_do_reports_the_final_screens_signals(self) -> None:
        self.start_session()
        self.helper.xml = COMPOSE_XML
        self.helper.windows = WINDOWS["compose_dialog"]
        code, payload = self.cli("ui", "do", stdin='[{"assertVisible":{"text":"Delete"}}]')
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "passed")
        dialog = payload["screen"]["signals"]["dialog"]
        self.assertEqual((dialog["kind"], [b["label"] for b in dialog["buttons"]]),
                         ("window", ["Cancel", "Delete"]))
        self.assertEqual([op for op in self.ops() if op != "read"], [], "nothing but reads")
        self.assertEqual(self.inputs(), [])

    def test_ui_do_masks_every_sensitive_value_in_the_dialog_it_reports_and_saves(self) -> None:
        self.start_session()
        field = node(text="", rid="com.example.app:id/field", cls="android.widget.EditText",
                     bounds="[100,1000][980,1100]").replace('focused="false"', 'focused="true"')
        screen = hierarchy(node(cls="android.widget.FrameLayout", bounds="[0,0][1080,2400]",
                                children=(
            node(text=f"Send to {SECOND}?", rid="android:id/alertTitle",
                 bounds="[100,900][980,1000]")
            + field
            + node(text=f"Pay {FIRST}", rid="android:id/button1", cls="android.widget.Button",
                   clickable=True, bounds="[700,1200][980,1300]")
            + node(text="Cancel", rid="android:id/button2", cls="android.widget.Button",
                   clickable=True, bounds="[400,1200][680,1300]"))))
        self.helper.xml = screen
        steps = json.dumps([{"inputText": {"value": FIRST, "sensitive": True}},
                            {"inputText": {"value": SECOND, "sensitive": True}}])
        code, payload = self.cli("ui", "do", stdin=steps)
        self.assertIn("screen", payload, payload)
        dialog = payload["screen"]["signals"]["dialog"]
        self.assertEqual(dialog["kind"], "alert")
        self.assertEqual(dialog["title"], "<12 chars>", "the second value, not only the first")
        self.assertEqual([b["label"] for b in dialog["buttons"]], ["<12 chars>", "Cancel"])
        saved = Path(payload["screen"]["saved"]).read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved)["signals"], payload["screen"]["signals"])
        for value in (FIRST, SECOND):
            self.assertNotIn(value, json.dumps(payload))
            self.assertNotIn(value, saved)

    def test_ui_do_on_a_fallback_read_reports_keyboard_unknown(self) -> None:
        self.start_session()
        self.no_tools()
        code, payload = self.cli("ui", "do", stdin='[{"assertVisible":{"text":"Battery"}}]')
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["screen"]["signals"], {"keyboard": None, "dialog": None})


if __name__ == "__main__":
    unittest.main()
