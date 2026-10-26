"""Error codes are additive-only (docs/COMPATIBILITY.md).

New codes must exist with their exact wire values, every code value must be
unique, and pre-existing codes must keep the values host agents branch on.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402


NEW_CODES = {
    "SESSION_ALREADY_ACTIVE": "session_already_active",
    "IOS_HID_FRAMEWORK_MISSING": "ios_hid_framework_missing",
    "OUTPUT_NOT_WRITABLE": "output_not_writable",
    "SELECTOR_REQUIRED": "selector_required",
    "RUN_NOT_FOUND": "run_not_found",
    "STEP_NOT_FOUND": "step_not_found",
    "FLOW_SOURCE_CHANGED": "flow_source_changed",
    "COMMENTS_WOULD_BE_LOST": "comments_would_be_lost",
    "INVALID_SIMULATOR_ACTION": "invalid_simulator_action",
    "SIGNAL_UNSTABLE": "signal_unstable",
    "STALE_REF": "stale_ref",
}

# Canvas actions (run canvas-multi-device, contract 5.3).
NEW_CODES.update({
    "RECORDING_NOT_ACTIVE": "recording_not_active",
    "CAPTURE_NOT_FOUND": "capture_not_found",
    "CAPTURE_TOO_LARGE": "capture_too_large",
    "INSTALL_NOT_CONFIGURED": "install_not_configured",
    "INSTALL_PATH_NOT_ALLOWED": "install_path_not_allowed",
    "INSTALL_FAILED": "install_failed",
})

# Canvas workspace CLI (run canvas-multi-device, contract 6.1).
NEW_CODES.update({
    "CANVAS_NOT_FOUND": "canvas_not_found",
    "CANVAS_AMBIGUOUS": "canvas_ambiguous",
    "WORKSPACE_IN_USE": "workspace_in_use",
})

# Control lease (run ws-p1p2-control, contract 4.6).
NEW_CODES.update({
    "CONTROL_HELD": "control_held",
    "CONTROL_GENERATION_STALE": "control_generation_stale",
    "CONTROL_LEASE_EXPIRED": "control_lease_expired",
    "CONTROL_NOT_HELD": "control_not_held",
})

# Ticket pre-test follow-ups (0.36.2).
FOLLOWUP_CODES = {
    "CLIPBOARD_UNAVAILABLE": "clipboard_unavailable",
    "PRETEST_DAMAGED": "pretest_damaged",
    "RACE_NEEDS_COUNT": "race_needs_count",
    "CHECK_NOT_ENDED": "check_not_ended",
    "GIT_MISSING": "git_missing",
}
NEW_CODES.update(FOLLOWUP_CODES)

# Android reads (agent loop, AL-05): a screen uiautomator never caught idle.
ANDROID_READ_CODES = {
    "UI_NOT_IDLE": "ui_not_idle",
}
NEW_CODES.update(ANDROID_READ_CODES)
ANDROID_READ_SECTION = "### Android reads: never-idle screens and element state (Unreleased)"

# Agent profile round (agent loop): the exit-130 envelope of an interrupt.
AGENT_LOOP_CODES = {
    "INTERRUPTED": "interrupted",
}
NEW_CODES.update(AGENT_LOOP_CODES)
AGENT_LOOP_SECTION = "### Agent profile, one-line JSON and observe (Unreleased)"

# iOS device daemon (speed plan 2b): warnings of an unconfirmed input and the
# daemon's failures. Their COMPATIBILITY.md rows come with the docs task.
IOS_DAEMON_CODES = {
    "INPUT_UNCONFIRMED": "input_unconfirmed",
    "IOS_INPUT_BLOCKED": "ios_input_blocked",
    "IOS_DISPLAY_NOT_LIT": "ios_display_not_lit",
    "COMPANION_TOO_OLD": "companion_too_old",
    "DAEMON_UNREACHABLE": "daemon_unreachable",
}
NEW_CODES.update(IOS_DAEMON_CODES)

EXISTING_CODES = {
    "FLOW_NOT_FOUND": "flow_not_found",
    "FLOW_FILE_NOT_FOUND": "flow_file_not_found",
    "BACKEND_FAILED": "backend_failed",
    "USAGE_ERROR": "usage_error",
    "INVALID_VALUE": "invalid_value",
    "AMBIGUOUS_TARGET": "ambiguous_target",
    "NO_ACTIVE_SESSION": "no_active_session",
    "SESSION_NOT_FOUND": "session_not_found",
    "NO_MATCHING_NODE": "no_matching_node",
    "TOOL_MISSING": "tool_missing",
}


def _code_constants() -> dict[str, str]:
    """Every upper-case string constant in the module is an error code."""
    return {name: value for name, value in vars(errors).items()
            if name.isupper() and not name.startswith("_")
            and isinstance(value, str)}


class AdditiveErrorCodeTests(unittest.TestCase):
    def test_new_codes_exist_with_exact_values(self) -> None:
        for name, value in NEW_CODES.items():
            with self.subTest(code=name):
                self.assertTrue(hasattr(errors, name), f"errors.{name} is missing")
                self.assertEqual(getattr(errors, name), value)

    def test_existing_codes_keep_their_values(self) -> None:
        for name, value in EXISTING_CODES.items():
            with self.subTest(code=name):
                self.assertEqual(getattr(errors, name), value)

    def test_code_values_are_unique(self) -> None:
        seen: dict[str, str] = {}
        for name, value in _code_constants().items():
            with self.subTest(code=name):
                self.assertNotIn(value, seen,
                                 f"{name} reuses the value of {seen.get(value)}")
                seen[value] = name

    def test_code_values_are_snake_case_of_their_name(self) -> None:
        for name, value in _code_constants().items():
            with self.subTest(code=name):
                self.assertEqual(value, name.lower())

    def test_compatibility_records_the_followup_codes_and_changes(self) -> None:
        text = (ROOT / "docs/COMPATIBILITY.md").read_text(encoding="utf-8")
        parts = text.split("### Ticket pre-test follow-ups (0.36.2)", 1)
        self.assertEqual(len(parts), 2, "COMPATIBILITY.md has no 0.36.2 follow-ups section")
        section = parts[1].split("\n### ", 1)[0]
        for name, value in FOLLOWUP_CODES.items():
            with self.subTest(code=name):
                self.assertIn(f"| `{value}` |", section)
        # each changed emission: the situation, what it was and what it is now
        changes = {
            "Android `simulator clipboard get`": ("`unsupported_on_platform`",
                                                  "`clipboard_unavailable`"),
            "`pretest start` git failures": ("`backend_failed`", "`invalid_value`",
                                             "`git_missing`"),
            "`--env`": ("`flow_command_invalid`", "`invalid_value`"),
        }
        rows = [line for line in section.splitlines() if line.startswith("| ")]
        for situation, codes in changes.items():
            with self.subTest(situation=situation):
                row = next((line for line in rows if line.startswith(f"| {situation}")), None)
                self.assertIsNotNone(row, situation)
                for code in codes:
                    self.assertIn(code, row)

    def test_compatibility_records_the_android_read_code_and_change(self) -> None:
        text = (ROOT / "docs/COMPATIBILITY.md").read_text(encoding="utf-8")
        parts = text.split(ANDROID_READ_SECTION, 1)
        self.assertEqual(len(parts), 2, "COMPATIBILITY.md has no Android read section")
        section = parts[1].split("\n### ", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("| ")]
        for name, value in ANDROID_READ_CODES.items():
            with self.subTest(code=name):
                self.assertIn(f"| `{value}` |", section)
        changed = next((line for line in rows
                        if line.startswith("| Android tree read on a screen that never goes idle")),
                       None)
        self.assertIsNotNone(changed)
        for code in ("`backend_failed`", "`ui_not_idle`"):
            self.assertIn(code, changed)
        self.assertIn("ui_not_idle", text.split("## Response shape", 1)[0],
                      "the Error codes list names the round")

    def test_compatibility_records_the_agent_loop_code(self) -> None:
        text = (ROOT / "docs/COMPATIBILITY.md").read_text(encoding="utf-8")
        parts = text.split(AGENT_LOOP_SECTION, 1)
        self.assertEqual(len(parts), 2, "COMPATIBILITY.md has no agent profile section")
        section = parts[1].split("\n### ", 1)[0]
        for name, value in AGENT_LOOP_CODES.items():
            with self.subTest(code=name):
                self.assertIn(f"| `{value}` |", section)
        self.assertNotIn("No `error_code` is added", section)
        self.assertIn("`interrupted`", text.split("## Response shape", 1)[0],
                      "the Error codes list names the round")


if __name__ == "__main__":
    unittest.main()
