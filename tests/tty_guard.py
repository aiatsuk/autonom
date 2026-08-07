#!/usr/bin/env python3
"""Assert the test suite never reads from the terminal.

The consent gate prompts for its confirmation phrase whenever stdin is a TTY.
That branch is invisible to a headless run, so the suite passed everywhere it
was measured while, on a developer's actual terminal, three tests stopped and
waited for a human to type a sentence — and mistyping it failed a test about
proxy restoration.

This runs the whole suite with a stdin that *claims to be a TTY* but raises on
read. Any prompt therefore surfaces instantly, naming the test that reached for
the terminal, instead of hanging a shell. It cannot live inside the suite
itself: it has to own stdin before any test starts.

Run by `scripts/run_checks.sh`. Exit 0 = no test touches the terminal.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TtyThatRefusesToBeRead:
    """isatty() is true, so the interactive branch is taken if reached at all."""

    def isatty(self) -> bool:
        return True

    def readline(self, *_args, **_kwargs):
        raise AssertionError(
            "a test reached for the terminal — consent must be injected via the "
            "`prompt` seam, never read from stdin during tests"
        )

    read = readline

    def fileno(self) -> int:
        return 0


def main() -> int:
    sys.stdin = TtyThatRefusesToBeRead()
    result = unittest.main(
        module=None,
        argv=["tty_guard", "discover", "-s", str(ROOT / "tests")],
        exit=False,
    ).result
    if result.wasSuccessful():
        print(f"\nNo test read the terminal ({result.testsRun} tests).")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
