"""One owner for metrics artifact conventions (§4.2).

Every metrics writer shares the same compact UTC stamp, the same
``{stamp}-{label}-…`` naming order, the same 0600 mode, and the same
collision rule — a repeated stem within the same second gets a ``-2``,
``-3``… suffix instead of silently overwriting earlier evidence.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import session as session_mod


def stamp() -> str:
    """Compact UTC stamp for artifact filenames: 20260815T120000Z."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def write_text(path: Path, text: str) -> str:
    """Write a 0600 artifact and return its path as str."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def metrics_dir(record: dict[str, Any]) -> Path:
    """The session's metrics/ dir, created on first use."""
    return session_mod.artifact_path(record, "metrics", "anchor").parent


def unique_stem(directory: Path, stem: str, *suffixes: str) -> str:
    """The first stem whose files (stem+suffix for every suffix) are all new.

    Snapshot stamps have one-second granularity, so a fast series would
    otherwise write every sample to the same names.
    """
    candidate = stem
    counter = 1
    while any((directory / f"{candidate}{suffix}").exists()
              for suffix in suffixes):
        counter += 1
        candidate = f"{stem}-{counter}"
    return candidate
