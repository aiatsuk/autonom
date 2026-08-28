from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
# Captured from a real run: idb 1.1.7 / idb-companion 1.1.8 / Xcode 26.1.1 /
# iOS 26.0 on iPhone 17 Pro, Settings app (TASK-2.0.1, VER-003).
FLAT = ROOT / "tests/fixtures/idb_describe_all_sample.json"
NESTED = ROOT / "tests/fixtures/idb_describe_all_nested.json"
ANDROID_DUMP = ROOT / "tests/fixtures/ui_dump.xml"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, selector, ui_android, ui_ios  # noqa: E402

UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"


class CompactSchemaTests(unittest.TestCase):
    """CAP-IOSUI-001 — iOS must fill the *same* compact node schema as Android."""

    def setUp(self) -> None:
        self.nested = ui_ios.parse_all(NESTED.read_text(encoding="utf-8"))
        self.flat = ui_ios.parse_all(FLAT.read_text(encoding="utf-8"))

    def test_key_set_is_identical_to_android(self) -> None:
        android = ui_android.parse_all(ANDROID_DUMP.read_text(encoding="utf-8"))
        self.assertTrue(android and self.nested)
        self.assertEqual(set(android[0]), set(self.nested[0]))

    def test_both_tree_shapes_produce_the_same_nodes(self) -> None:
        # Depth differs by construction between a nested and a flattened dump;
        # every other field must agree, which is what U-001 tolerance means.
        strip = lambda nodes: [  # noqa: E731
            {key: value for key, value in node.items() if key != "depth"} for node in nodes
        ]
        self.assertEqual(strip(self.nested), strip(self.flat))

    def test_label_and_identifier_are_mapped_from_real_output(self) -> None:
        button = next(node for node in self.flat if node["desc"] == "General")
        self.assertEqual(button["role"], "button")
        self.assertTrue(button["clickable"])
        # Plan section 2.4 assumed resource_id is always null on iOS. Real output
        # shows AXUniqueId populated, which is what makes stable selectors work.
        self.assertEqual(button["resource_id"], "com.apple.settings.general")
        self.assertEqual(button["bounds"], [16, 380, 386, 432])

    def test_float_frames_are_truncated_to_int_bounds(self) -> None:
        heading = next(node for node in self.flat if node["role"] == "heading")
        # Real frames carry floats (y=119.666..., height=40.666...).
        self.assertEqual(heading["bounds"], [16, 119, 149, 160])
        self.assertTrue(all(isinstance(value, int) for value in heading["bounds"]))

    def test_disabled_element_is_reported_disabled(self) -> None:
        payload = json.dumps([
            {"type": "Button", "AXLabel": "Log In", "enabled": False,
             "frame": {"x": 0, "y": 0, "width": 10, "height": 10}}
        ])
        self.assertFalse(ui_ios.parse_all(payload)[0]["enabled"])

    def test_meaningful_filter_drops_the_unlabelled_group(self) -> None:
        nodes, _ = ui_ios.parse_tree(FLAT.read_text(encoding="utf-8"))
        everything, _ = ui_ios.parse_tree(FLAT.read_text(encoding="utf-8"), meaningful_only=False)
        self.assertEqual(len(everything), 15)
        self.assertEqual(len(nodes), 14)
        self.assertFalse(any(node["role"] == "group" and not node["desc"] for node in nodes))

    def test_sparse_tree_is_reported_rather_than_looking_empty(self) -> None:
        _nodes, warnings = ui_ios.parse_tree("[]")
        codes = {warning["code"] for warning in warnings}
        self.assertIn("sparse_accessibility_tree", codes)

    def test_tree_without_identifiers_says_so(self) -> None:
        payload = json.dumps([
            {"type": "Button", "AXLabel": "One", "frame": {"x": 0, "y": 0, "width": 10, "height": 10}},
            {"type": "Button", "AXLabel": "Two", "frame": {"x": 0, "y": 20, "width": 10, "height": 10}},
            {"type": "Button", "AXLabel": "Three", "frame": {"x": 0, "y": 40, "width": 10, "height": 10}},
        ])
        _nodes, warnings = ui_ios.parse_tree(payload)
        self.assertIn("no_accessibility_identifiers", {warning["code"] for warning in warnings})

    def test_screen_size_comes_from_the_application_frame(self) -> None:
        self.assertEqual(ui_ios.screen_size_from(FLAT.read_text(encoding="utf-8")), (402, 874))


class SelectorParityTests(unittest.TestCase):
    """CAP-PLAT-004 — the same duplicate rule on both platforms."""

    def test_duplicate_labels_require_an_index_on_ios(self) -> None:
        nodes = ui_ios.parse_all(FLAT.read_text(encoding="utf-8"))
        with self.assertRaises(errors.AutonomError) as caught:
            selector.select(nodes, {"desc": "Settings"}, mode="exact")
        self.assertEqual(caught.exception.code, errors.AMBIGUOUS_SELECTOR)
        self.assertIn("matched", caught.exception.message)

    def test_index_selects_and_negative_counts_from_the_end(self) -> None:
        nodes = ui_ios.parse_all(FLAT.read_text(encoding="utf-8"))
        first = selector.select(nodes, {"desc": "Settings"}, mode="exact", index=0)
        last = selector.select(nodes, {"desc": "Settings"}, mode="exact", index=-1)
        self.assertEqual(first[0]["role"], "app")
        self.assertEqual(last[0]["role"], "heading")

    def test_out_of_range_index_is_an_error(self) -> None:
        nodes = ui_ios.parse_all(FLAT.read_text(encoding="utf-8"))
        with self.assertRaises(errors.AutonomError) as caught:
            selector.select(nodes, {"desc": "Settings"}, mode="exact", index=9)
        self.assertEqual(caught.exception.code, errors.SELECTOR_INDEX_OUT_OF_RANGE)


class CoordinateGuardTests(unittest.TestCase):
    """VER-004 / INV-06 — no scaling, and impossible taps are refused."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "argv.jsonl"
        self.state = Path(self.tmp.name) / "state.json"
        self._write_state(str(FLAT))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_state(self, describe_path: str) -> None:
        self.state.write_text(
            json.dumps({"idb_describe_all": describe_path}), encoding="utf-8"
        )

    def _run(self, *args: str):
        env = dict(os.environ)
        env["AUTONOM_FAKE_LOG"] = str(self.log)
        env["AUTONOM_FAKE_STATE"] = str(self.state)
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "ios", "--target", UDID,
             "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB), *args],
            cwd=self.tmp.name, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def _taps(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [call["argv"] for call in calls
                if call["tool"] == "idb" and call["argv"][:2] == ["ui", "tap"]]

    def test_tap_dispatches_the_unscaled_frame_centre(self) -> None:
        result = self._run("ui", "tap", "--desc", "General", "--mode", "exact")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # Real frame x=16 y=380.33 w=370 h=52 -> bounds [16,380,386,432],
        # centre (201, 406) in points. iPhone 17 Pro is 3x, so a pixel mix-up
        # would land at (603, 1218) - outside the 402x874 point screen.
        self.assertEqual((payload["x"], payload["y"]), (201, 406))
        dispatched = self._taps()
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][:4], ["ui", "tap", "201", "406"])
        self.assertIn(UDID, dispatched[0])

    def test_display_scale_is_never_applied(self) -> None:
        self._run("ui", "tap", "--desc", "General", "--mode", "exact")
        dispatched = self._taps()[0]
        x, y = int(dispatched[2]), int(dispatched[3])
        for factor in (2, 3):
            self.assertNotEqual((x, y), (201 * factor, 406 * factor))
            self.assertNotEqual((x, y), (201 // factor, 406 // factor))

    def test_a_tripled_tree_is_refused_and_dispatches_nothing(self) -> None:
        inflated = Path(self.tmp.name) / "inflated.json"
        payload = json.loads(FLAT.read_text(encoding="utf-8"))
        for element in payload:
            if element.get("type") == "Application":
                continue  # the screen rect stays honest; the contents do not
            for key in ("x", "y", "width", "height"):
                element["frame"][key] *= 3
        inflated.write_text(json.dumps(payload), encoding="utf-8")
        self._write_state(str(inflated))

        result = self._run("ui", "tap", "--desc", "General", "--mode", "exact")
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stderr)
        self.assertEqual(error["error_code"], "coordinate_space_mismatch")
        self.assertEqual(error["screen"], [402, 874])
        self.assertEqual(self._taps(), [], "no tap may be dispatched after the guard trips")

    def test_explicit_out_of_bounds_coordinates_are_refused(self) -> None:
        result = self._run("ui", "tap", "--x", "4000", "--y", "4000")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"], "coordinate_space_mismatch")
        self.assertEqual(self._taps(), [])


class KeyAndGestureTests(unittest.TestCase):
    """CAP-IOSUI-004 / CAP-IOSUI-005 — refuse rather than silently do nothing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "argv.jsonl"
        self.state = Path(self.tmp.name) / "state.json"
        self.state.write_text(json.dumps({"idb_describe_all": str(FLAT)}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str, platform: str = "ios", target: str = UDID):
        env = dict(os.environ)
        env["AUTONOM_FAKE_LOG"] = str(self.log)
        env["AUTONOM_FAKE_STATE"] = str(self.state)
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", platform, "--target", target,
             "--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
             "--adb", str(ROOT / "tests/fakes/fake_adb.py"), *args],
            cwd=self.tmp.name, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def _calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_named_button_maps_to_idb_button(self) -> None:
        result = self._run("ui", "key", "HOME")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["ui", "button", "HOME"], [call[:3] for call in self._calls()])

    def test_android_keycode_on_ios_is_rejected_with_guidance(self) -> None:
        result = self._run("ui", "key", "KEYCODE_BACK")
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stderr)
        self.assertEqual(error["error_code"], "unsupported_key_for_platform")
        self.assertIn("HOME", error["hint"])
        self.assertIn("no global Back button", error["hint"])

    def test_shake_is_refused_on_android(self) -> None:
        result = self._run("ui", "shake", platform="android", target="emulator-5554")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"], "unsupported_on_platform")

    def test_unbacked_gestures_are_refused_before_reaching_idb(self) -> None:
        """idb has no pinch/rotate/shake, so nothing may be dispatched for them.

        These shipped for several releases building `idb ui pinch` and friends,
        which every real machine rejected with an argparse usage dump wearing an
        `backend_failed` code and a hint pointing at `doctor` — a tool that was
        working fine. The refusal has to happen here, above the wrapper.
        """
        for gesture, extra in (("pinch", ("--at", "10,10")), ("rotate", ()), ("shake", ())):
            with self.subTest(gesture=gesture):
                before = len(self._calls())
                result = self._run("ui", gesture, *extra)
                self.assertEqual(result.returncode, 2)
                error = json.loads(result.stderr)
                self.assertEqual(error["error_code"], "unsupported_on_platform")
                self.assertIn("idb provides no", error["error"])
                self.assertIn("ui swipe", error["hint"])
                self.assertEqual(self._calls()[before:], [], "nothing may reach idb")

    def test_type_passes_unicode_through_unmangled(self) -> None:
        result = self._run("ui", "type", "café@example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        texts = [call for call in self._calls() if call[:2] == ["ui", "text"]]
        self.assertEqual(texts[0][2], "café@example.com")


if __name__ == "__main__":
    unittest.main()
