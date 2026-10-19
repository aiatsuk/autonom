"""Error codes added by the pre-test follow-ups keep their exact wire values.

Host agents branch on ``error_code`` values, so each new code must exist with
the value the contract names, and no two codes in the module may share a value.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402


NEW_CODES = {
    "CLIPBOARD_UNAVAILABLE": "clipboard_unavailable",
    "PRETEST_DAMAGED": "pretest_damaged",
    "RACE_NEEDS_COUNT": "race_needs_count",
    "CHECK_NOT_ENDED": "check_not_ended",
    "GIT_MISSING": "git_missing",
}


def _module_codes() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(errors).items()
        if name.isupper() and isinstance(value, str)
    }


class FollowupErrorCodesTest(unittest.TestCase):
    def test_each_new_code_has_its_wire_value(self) -> None:
        for name, value in NEW_CODES.items():
            with self.subTest(name=name):
                self.assertTrue(hasattr(errors, name), f"errors.{name} is missing")
                self.assertEqual(getattr(errors, name), value)

    def test_code_values_are_unique_across_the_module(self) -> None:
        codes = _module_codes()
        seen: dict[str, str] = {}
        for name, value in codes.items():
            with self.subTest(name=name):
                self.assertNotIn(value, seen, f"{name} repeats the value of {seen.get(value)}")
                seen[value] = name

    def test_new_codes_render_in_the_error_payload(self) -> None:
        for name, value in NEW_CODES.items():
            with self.subTest(name=name):
                payload = errors.AutonomError(value, "message", hint="next step").as_dict()
                self.assertEqual(payload["error_code"], value)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["hint"], "next step")


if __name__ == "__main__":
    unittest.main()
