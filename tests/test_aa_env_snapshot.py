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
"""
from __future__ import annotations

import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
