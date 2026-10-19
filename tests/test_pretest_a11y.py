"""Accessibility rule: nodes that look interactive count as controls (fu-010).

A node is a control when it is clickable, long-clickable or checkable, or when
its class or role is a button, link, switch, checkbox, radio, slider or text
field (Android class names, iOS ``XCUIElementType…`` and ``AX…`` names), or it
has an action trait. Also the ticket-pretest skill notes of this run: the iOS
log stream, the app language by launch arguments, the Android clipboard recipe
and the race count. Pure functions and file reads only; no device.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib.pretest import a11y  # noqa: E402

APP = "com.example.app"
SKILL = ROOT / "plugins" / "autonom" / "skills" / "ticket-pretest" / "SKILL.md"
KIT = ROOT / "plugins" / "autonom" / "skills" / "ticket-pretest" / "references" / "edge-case-kit.md"


def node(ref: str, top: int, size: int = 30, **fields: object) -> dict:
    """A labelled node of `size` x `size` at the left edge, not clickable unless told."""
    base = {"ref": ref, "parent": None, "bounds": [0, top, size, top + size],
            "text": f"label {ref}", "clickable": False}
    base.update(fields)
    return base


def small(result: dict) -> list:
    return sorted(ref for issue in result["issues"] if issue["rule"] == "small_target"
                  for ref in issue["refs"])


class InteractiveNodeTests(unittest.TestCase):
    def test_a_30_pt_non_clickable_ios_button_is_a_small_target(self) -> None:
        button = node("n1", 0, role="XCUIElementTypeButton", **{"class": "XCUIElementTypeButton"})
        result = a11y.evaluate([button], "ios")
        self.assertEqual(result["controls"], 1)
        (issue,) = result["issues"]
        self.assertEqual((issue["rule"], issue["refs"]), ("small_target", ["n1"]))
        self.assertEqual(issue["size"], {"width": 30.0, "height": 30.0, "unit": "pt",
                                         "minimum": 44})

    def test_ios_and_ax_role_names_count(self) -> None:
        nodes = [node("n1", 0, role="AXLink"), node("n2", 100, role="XCUIElementTypeSwitch"),
                 node("n3", 200, **{"class": "XCUIElementTypeSlider"}),
                 node("n4", 300, role="AXCheckBox"), node("n5", 400, role="XCUIElementTypeTextField"),
                 node("n6", 500, role="AXRadioButton"), node("n7", 600, role="SecureTextField")]
        self.assertEqual(small(a11y.evaluate(nodes, "ios")), ["n1", "n2", "n3", "n4", "n5", "n6", "n7"])

    def test_android_control_classes_count_without_clickable(self) -> None:
        classes = ["android.widget.ImageButton", "android.widget.Switch",
                   "androidx.appcompat.widget.SwitchCompat", "android.widget.CheckBox",
                   "android.widget.RadioButton", "android.widget.SeekBar", "android.widget.EditText",
                   "com.google.android.material.button.MaterialButton",
                   "com.google.android.material.chip.Chip"]
        nodes = [node(f"n{index}", index * 100, package=APP, **{"class": name})
                 for index, name in enumerate(classes)]
        result = a11y.evaluate(nodes, "android", density=160, app_id=APP)
        self.assertEqual(result["controls"], len(classes))
        self.assertEqual(len(small(result)), len(classes))

    def test_action_traits_and_custom_actions_count(self) -> None:
        nodes = [node("n1", 0, role="other", traits=["Button"]),
                 node("n2", 100, role="other", traits="UIAccessibilityTraitLink"),
                 node("n3", 200, role="other", traits="StaticText, Adjustable"),
                 node("n4", 300, role="other", actions=["activate"])]
        self.assertEqual(small(a11y.evaluate(nodes, "ios")), ["n1", "n2", "n3", "n4"])

    def test_static_and_odd_nodes_are_not_controls(self) -> None:
        nodes = [node("n1", 0, role="text", **{"class": "android.widget.TextView"}, package=APP),
                 node("n2", 100, role="image", traits=["Image", "StaticText"], package=APP),
                 node("n3", 200, role=None, traits=7, actions=[], package=APP, **{"class": None}),
                 node("n4", 300, role="node", traits=[None, 3], actions="activate", package=APP),
                 node("n5", 400, role="buttonbar", **{"class": "android.view.View"}, package=APP)]
        result = a11y.evaluate(nodes, "android", density=160, app_id=APP)
        self.assertEqual((result["controls"], result["issues"]), (0, []))

    def test_hidden_empty_and_foreign_buttons_stay_out(self) -> None:
        nodes = [node("n1", 0, role="button", visible=False, package=APP),
                 {"ref": "n2", "parent": None, "bounds": [0, 0, 0, 0], "role": "button",
                  "package": APP},
                 node("n3", 200, **{"class": "android.widget.Button"},
                      package="com.android.systemui"),
                 node("n4", 300, **{"class": "android.widget.Button"}, package="com.other.app")]
        result = a11y.evaluate(nodes, "android", density=160, app_id=APP)
        self.assertEqual((result["controls"], result["issues"]), (0, []))

    def test_an_unlabelled_interactive_node_is_reported(self) -> None:
        link = {"ref": "n1", "parent": None, "bounds": [0, 0, 100, 100],
                "role": "XCUIElementTypeLink"}
        result = a11y.evaluate([link], "ios")
        self.assertEqual([issue["rule"] for issue in result["issues"]], ["unlabeled"])

    def test_a_large_enough_interactive_node_is_not_flagged(self) -> None:
        button = node("n1", 0, size=44, role="XCUIElementTypeButton")
        result = a11y.evaluate([button], "ios")
        self.assertEqual((result["controls"], result["issues"]), (1, []))

    def test_word_and_looks_interactive(self) -> None:
        self.assertEqual(a11y._word("XCUIElementTypeButton"), "button")
        self.assertEqual(a11y._word("AXRadioButton"), "radiobutton")
        self.assertEqual(a11y._word(None), "")
        self.assertFalse(a11y.looks_interactive({}))
        self.assertTrue(a11y.looks_interactive({"class": "Foo$Button"}))
        self.assertFalse(a11y.looks_interactive({"traits": {"Button": True}}))


class SkillNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")
        self.kit = KIT.read_text(encoding="utf-8")

    def test_ios_log_stream_note(self) -> None:
        self.assertIn("On iOS, start the session with `--log-stream`", self.skill)
        self.assertIn("`pretest health`", self.skill)

    def test_app_language_by_launch_arguments(self) -> None:
        recipe = "--arg=-AppleLanguages --arg='(ru)' --arg=-AppleLocale --arg=ru_RU"
        self.assertIn(recipe, self.skill)
        self.assertIn(recipe, self.kit)

    def test_android_clipboard_recipe(self) -> None:
        self.assertIn("`clipboard_unavailable`", self.skill)
        self.assertIn("long-press it, tap Paste", self.skill)
        self.assertIn("`ui find --text '<expected>'`", self.skill)

    def test_race_case_needs_the_observed_count(self) -> None:
        for text in (self.skill, self.kit):
            self.assertIn("--verdict pass --observed-count N", text)
            self.assertIn("`race_needs_count`", text)
        self.assertIn("`double-tap`", self.skill)

    def test_a11y_control_rule_is_described(self) -> None:
        self.assertIn("looks interactive without saying so", self.skill)


if __name__ == "__main__":
    unittest.main()
