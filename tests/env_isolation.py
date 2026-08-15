"""Shared environment sandbox for tests that must mutate ``os.environ``.

Two suites shipped the same defect independently: ``setUp`` wrote
``AUTONOM_HOME`` (and friends) into the process environment and ``tearDown``
popped the keys unconditionally, destroying any ambient value for the rest of
the process. Every later test then resolved the machine store to the
operator's real ``~/.autonom`` / ``~/.local/state/autonom``. The correct
save-and-restore idiom already existed in three places; this module makes it
the only idiom, so the class of bug cannot be reintroduced by copying the
wrong neighbour.

Rules the helper encodes:

- the previous value of a key is captured exactly once per test, before the
  first overwrite, and restored via ``addCleanup`` — LIFO, and it runs even
  when ``setUp`` fails after the mutation;
- restoring means putting back the exact observed value, or popping only when
  the key was genuinely absent before the test;
- a subclass with its own ``tearDown`` cannot skip restoration, because
  ``addCleanup`` does not depend on ``tearDown`` running.

Import as a top-level module (``from env_isolation import EnvSandboxMixin``):
``unittest discover -s tests`` puts ``tests/`` on ``sys.path``, the same
mechanism ``test_contract_golden.py`` relies on for ``contract_probe``.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


def _restore(key: str, value: "str | None") -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


class EnvSandboxMixin:
    """Mixin for ``unittest.TestCase`` subclasses. List it before TestCase."""

    def set_env(self, **pairs: "str | None") -> None:
        """Set (or, with ``None``, remove) environment keys for this test.

        The pre-existing value of each key is captured once per test and
        restored automatically, no matter how the test exits.
        """
        saved = getattr(self, "_env_sandbox_saved", None)
        if saved is None:
            saved = set()
            self._env_sandbox_saved = saved
        for key, value in pairs.items():
            if key not in saved:
                saved.add(key)
                self.addCleanup(_restore, key, os.environ.get(key))
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def sandbox_home(self) -> Path:
        """Redirect ``AUTONOM_HOME`` to a fresh temp dir; return its path."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.set_env(AUTONOM_HOME=tmp.name)
        return Path(tmp.name)


class EnvSandbox(EnvSandboxMixin, unittest.TestCase):
    pass
