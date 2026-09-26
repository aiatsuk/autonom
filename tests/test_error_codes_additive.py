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


if __name__ == "__main__":
    unittest.main()
