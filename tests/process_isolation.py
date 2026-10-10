"""Keep machine-wide proxy discovery inside one test's own temporary root.

``processes.discover_proxies`` scans every process on the machine for the
proxy signature (``mitm_addon.py`` on a mitmdump command line) — by design: a
proxy whose registry row was lost must still be found. In a parallel test run
that means a fake proxy started by another module (``test_network``,
``test_fix_logs_proc``) shows up here as an orphan, and a ``cleanup()`` here
terminates it out from under its own test.

``scope_proxy_discovery`` keeps the real discovery path (``ps`` and the
signature match) but drops every proxy whose ``autonom_dir=`` artifacts
directory is not under the test's root. A proxy without an artifacts
directory cannot be shown to be this test's and is dropped too.

``own_entries`` filters a report produced in a subprocess the same way.

Import as a top-level module (``from process_isolation import ...``), with a
``tests.process_isolation`` fallback, as with ``env_isolation``.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock


def _under(directory: Any, root: Path) -> bool:
    if not directory:
        return False
    try:
        Path(os.path.realpath(str(directory))).relative_to(root)
    except ValueError:
        return False
    return True


def scope_proxy_discovery(case: unittest.TestCase, processes: ModuleType,
                          root: "str | os.PathLike[str]") -> None:
    """Until ``case`` ends, ``processes.discover_proxies`` (and so ``scan``
    and ``cleanup``) reports only proxies whose artifacts dir is under
    ``root``."""
    real = processes.discover_proxies
    base = Path(os.path.realpath(os.fspath(root)))

    def scoped() -> list[dict[str, Any]]:
        return [found for found in real() if _under(found.get("artifacts_dir"), base)]

    patcher = mock.patch.object(processes, "discover_proxies", side_effect=scoped)
    patcher.start()
    case.addCleanup(patcher.stop)


def own_entries(entries: list[dict[str, Any]],
                root: "str | os.PathLike[str]") -> list[dict[str, Any]]:
    """``entries`` (a report's ``orphans``, or a ``scan`` list) without the
    proxies that only signature discovery found outside ``root``.

    For a test that runs ``autonom doctor`` (or ``processes``) in a
    subprocess, where ``scope_proxy_discovery`` cannot reach: an entry from
    the test's own registry or proxy.json is kept; a proxy found only by
    its command line (``source == "signature"``) is kept only when its
    artifacts dir is under ``root`` — anything else is another test's, or
    the developer's own, proxy.
    """
    base = Path(os.path.realpath(os.fspath(root)))
    return [entry for entry in entries
            if entry.get("source") != "signature"
            or _under(entry.get("artifacts_dir"), base)]
