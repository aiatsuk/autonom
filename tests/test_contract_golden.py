from __future__ import annotations

import json
import unittest
from pathlib import Path

from contract_probe import GOLDEN, collect, compare

ROOT = Path(__file__).resolve().parents[1]


class ContractGoldenTests(unittest.TestCase):
    """VER-001 — the Android JSON contract must survive the platform refactor.

    The golden was recorded from 0.4.0 before `platform.py` existed. A key that
    is renamed rather than removed still passes every hand-written assertion in
    the rest of the suite, which is exactly why this test is recorded and not
    written by hand.
    """

    def test_golden_exists(self) -> None:
        self.assertTrue(GOLDEN.exists(), "record it with: python3 tests/contract_probe.py --write")

    def test_no_android_contract_key_was_removed(self) -> None:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        problems = compare(collect(), golden)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_legacy_serial_is_still_emitted(self) -> None:
        """DEC-004 — `--serial` and the `serial` response key are permanent."""
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        current = collect()
        for probe in ("session_start", "session_show", "ui_tap_selector", "screenshot"):
            keys = set(current[probe]["keys"])
            self.assertTrue(
                any(key.endswith("serial:str") for key in keys),
                f"{probe} no longer reports a serial: {sorted(keys)}",
            )
            self.assertLessEqual(set(golden[probe]["keys"]), keys)


if __name__ == "__main__":
    unittest.main()
