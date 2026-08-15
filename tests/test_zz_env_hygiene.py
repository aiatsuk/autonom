"""Environment hygiene guard — runs alphabetically last, on purpose.

Compares ``os.environ`` against the snapshot ``test_aa_env_snapshot.py`` took
before any other test module imported. A suite member that sets a sentinel
key without restoring it — or pops an ambient value it never owned, the exact
defect that shipped twice in ``tearDown`` implementations — fails here with
the offending keys named, instead of silently redirecting every later test to
the operator's real ``~/.autonom``.

Also generalizes ``test_network.py``'s single-store hygiene check: with
``AUTONOM_HOME`` redirected, all four machine-store resolvers must resolve
under the redirected root; and the documented fallback chains
(``~/.autonom`` for sessions, ``$XDG_STATE_HOME/autonom`` else
``~/.local/state/autonom`` for CA/processes/mocks) stay covered even though
``run_checks.sh`` exports a scratch ``AUTONOM_HOME`` for the whole run.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import processes, session  # noqa: E402
from autonom_lib.network import mocks, proxy  # noqa: E402

try:
    from env_isolation import EnvSandbox  # noqa: E402  (discover -s tests)
    from test_aa_env_snapshot import ENV_SNAPSHOT, snapshot_is_authoritative  # noqa: E402
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandbox  # noqa: E402
    from tests.test_aa_env_snapshot import (  # noqa: E402
        ENV_SNAPSHOT,
        snapshot_is_authoritative,
    )

SENTINEL_EXACT = ("CI", "HOME", "XDG_STATE_HOME")
SENTINEL_PREFIX = "AUTONOM_"


def _sentinels(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if key.startswith(SENTINEL_PREFIX) or key in SENTINEL_EXACT
    }


class EnvRestorationGuardTests(unittest.TestCase):
    def test_sentinel_environment_matches_the_pre_suite_snapshot(self) -> None:
        if not snapshot_is_authoritative():
            self.skipTest("snapshot taken late (subset run) — the aa module's "
                          "own test fails loudly in that mode")
        before = _sentinels(ENV_SNAPSHOT)
        after = _sentinels(dict(os.environ))
        lost = {k: before[k] for k in before.keys() - after.keys()}
        gained = {k: after[k] for k in after.keys() - before.keys()}
        changed = {
            k: (before[k], after[k])
            for k in before.keys() & after.keys()
            if before[k] != after[k]
        }
        self.assertEqual(
            (lost, gained, changed), ({}, {}, {}),
            "a test mutated os.environ without restoring it "
            "(lost / gained / changed shown above) — use env_isolation.EnvSandboxMixin",
        )


class MachineStoreResolutionTests(EnvSandbox):
    def test_all_four_stores_resolve_under_a_redirected_home(self) -> None:
        root = self.sandbox_home()
        for name, resolved in (
            ("sessions", session.sessions_home()),
            ("processes", processes.registry_dir()),
            ("ca", proxy.ca_store()),
            ("mocks", mocks.registry_dir()),
        ):
            with self.subTest(store=name):
                self.assertTrue(
                    str(resolved).startswith(str(root)),
                    f"{name} store resolved outside the redirected AUTONOM_HOME: {resolved}",
                )

    def test_fallback_chain_without_autonom_home(self) -> None:
        home = self.sandbox_home()  # keeps a temp dir alive and restores env
        self.set_env(AUTONOM_HOME=None, HOME=str(home), XDG_STATE_HOME=None)
        self.assertEqual(session.sessions_home(), home / ".autonom" / "sessions")
        state = home / ".local/state/autonom"
        self.assertEqual(processes.registry_dir(), state / "processes")
        self.assertEqual(proxy.ca_store(), state / "ca")
        self.assertEqual(mocks.registry_dir(), state / "mocks")

    def test_xdg_state_home_takes_precedence_for_registries(self) -> None:
        home = self.sandbox_home()
        xdg = home / "xdg-state"
        self.set_env(AUTONOM_HOME=None, HOME=str(home), XDG_STATE_HOME=str(xdg))
        self.assertEqual(processes.registry_dir(), xdg / "autonom" / "processes")
        self.assertEqual(proxy.ca_store(), xdg / "autonom" / "ca")
        self.assertEqual(mocks.registry_dir(), xdg / "autonom" / "mocks")
        # Sessions ignore XDG and use ~/.autonom — documented asymmetry.
        self.assertEqual(session.sessions_home(), home / ".autonom" / "sessions")


if __name__ == "__main__":
    unittest.main()
