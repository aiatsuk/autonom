"""Capture the ambient environment before any other test module loads.

Ordering contract: ``unittest discover -s tests`` imports test modules in
alphabetical order, so the ``test_aa_`` prefix makes this the first module
imported and the snapshot below the earliest possible view of the process
environment. (``tests/__init__.py`` is *not* an anchor: under
``discover -s tests`` the modules import under top-level names and the
package initializer never executes — verified empirically.)

``test_zz_env_hygiene.py`` — alphabetically last — compares the environment
against this snapshot after every other module has run its tests, which
catches any set-without-restore or pop-without-restore anywhere in between.

The contract is asserted, not assumed: if this module is imported *after*
some sibling test module (a subset run, a different runner, parallelized
discovery), the snapshot would be taken too late and the guard would pass
vacuously — so that situation fails loudly here instead.

One harness default is applied right before the snapshot, so a plain
``unittest discover -s tests`` run has it too (``tests/run_parallel.py``
puts it in every worker's environment, ``tests/__init__.py`` covers
``python3 -m unittest tests.test_x``): ``AUTONOM_ANDROID_UI_BACKEND=
uiautomator`` (INV-5), so no unrelated test builds, pushes or reaches the
on-device Android helper. A value already set is kept, and the snapshot and
the hygiene guard both see the same one.
"""
from __future__ import annotations

import os
import sys
import unittest

ANDROID_UI_BACKEND = "AUTONOM_ANDROID_UI_BACKEND"
HARNESS_ANDROID_UI_BACKEND = "uiautomator"
os.environ.setdefault(ANDROID_UI_BACKEND, HARNESS_ANDROID_UI_BACKEND)

ENV_SNAPSHOT = dict(os.environ)

# Sibling test modules already imported when the snapshot was taken. The zz
# module importing us directly is the one legitimate late importer (a
# developer running `python3 -m unittest tests.test_zz_env_hygiene`), and in
# that mode the restoration guard is skipped rather than trusted.
_IMPORTED_BEFORE_SNAPSHOT = sorted(
    name for name in sys.modules
    if name.rpartition(".")[2].startswith("test_")
    and name.rpartition(".")[2] not in (__name__.rpartition(".")[2],
                                        "test_zz_env_hygiene")
)


def snapshot_is_authoritative() -> bool:
    return not _IMPORTED_BEFORE_SNAPSHOT


class SnapshotTakenTests(unittest.TestCase):
    def test_snapshot_was_taken_first(self) -> None:
        self.assertIsInstance(ENV_SNAPSHOT, dict)
        self.assertEqual(
            _IMPORTED_BEFORE_SNAPSHOT, [],
            "test_aa_env_snapshot imported after other test modules — the "
            "environment snapshot is late and the zz hygiene guard would pass "
            "vacuously. Run the suite via 'unittest discover -s tests' (or "
            "run_checks.sh), which imports modules alphabetically.",
        )

    def test_android_ui_backend_has_the_harness_default(self) -> None:
        # INV-5: a test reaches the Android helper path only when it asks.
        self.assertEqual(
            ENV_SNAPSHOT.get(ANDROID_UI_BACKEND), HARNESS_ANDROID_UI_BACKEND,
            f"{ANDROID_UI_BACKEND} must be {HARNESS_ANDROID_UI_BACKEND!r} for the suite; "
            "unset it in the shell (the harness sets it) — tests of the helper path "
            "set their own value.",
        )


if __name__ == "__main__":
    unittest.main()
