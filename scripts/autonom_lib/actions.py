"""Per-action detail records — the Session→Flow compiler's raw material.

The journal stays scrubbed and summary-shaped by design, which is exactly
why it cannot feed a compiler: a tap's matched node, the selector that found
it, and the surrounding tree were computed and thrown away. Instrumented
handlers write one JSON file per action under ``<session>/actions/`` (owner
-only, like every other artifact that can carry screen content) and put the
relative path into their response payload, which the journal choke point
lifts into the timeline via the ``detail`` summary key — so each journal
entry links to its rich record without the journal itself carrying it.

Best-effort like the journal: recording detail never fails the command. A
failed write comes back as an ``evidence_write_failed`` warning that callers
put into the command's ``warnings``; the device input is never repeated.
"""
from __future__ import annotations

import errno
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from . import session as session_mod

_SLUG = re.compile(r"[^a-z0-9]+")
# An existing detail name: its numeric prefix is what the next name builds on.
_INDEXED = re.compile(r"^(\d+)_.*\.json$")

# A warning code (never an errors.py code): the device action ran, only its
# detail record could not be saved.
EVIDENCE_WRITE_FAILED = "evidence_write_failed"
# How many names one writer tries before it gives up on a detail record.
MAX_NAME_ATTEMPTS = 64
# `os.link` errors that mean "this filesystem has no hard links": fall back to
# an exclusive create of the final name instead.
_NO_LINK_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None)
                      for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "EXDEV"))
    if code is not None)
_ERROR_LIMIT = 300


class _NamesExhausted(Exception):
    """Every candidate name was taken by another writer."""


def _next_index(directory: Path) -> int:
    """1 + the highest numeric prefix of an existing detail (never a count)."""
    highest = 0
    for name in os.listdir(directory):
        match = _INDEXED.match(name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _after_collision(directory: Path, index: int) -> int:
    """The index to try after ``index`` was taken: at least the next one, and
    past every name written meanwhile, so a writer that was descheduled while
    another wrote many records does not spend its attempts one name at a time."""
    try:
        return max(index + 1, _next_index(directory))
    except OSError:
        return index + 1


def _write_exclusive(final: Path, data: bytes) -> None:
    """Create ``final`` only if it does not exist; FileExistsError otherwise."""
    descriptor = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            os.unlink(final)
        except OSError:
            pass
        raise
    os.chmod(final, 0o600)


def _publish(directory: Path, slug: str, data: bytes) -> Path:
    """Publish ``data`` under the first free ``NNNN_<slug>.json``.

    The bytes go to a same-directory temporary first and are hard-linked to
    the final name, so a reader never sees a partial file and two writers
    never share a name (a taken name is FileExistsError: try the next index).
    """
    descriptor, temporary = tempfile.mkstemp(prefix=".detail-", suffix=".tmp",
                                             dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o600)
        index = _next_index(directory)
        use_link = True
        for _ in range(MAX_NAME_ATTEMPTS):
            final = directory / f"{index:04d}_{slug}.json"
            if use_link:
                try:
                    os.link(temporary, final)
                    return final
                except FileExistsError:
                    index = _after_collision(directory, index)
                    continue
                except OSError as exc:
                    if exc.errno not in _NO_LINK_ERRNOS:
                        raise
                    use_link = False
            try:
                _write_exclusive(final, data)
                return final
            except FileExistsError:
                index = _after_collision(directory, index)
        raise _NamesExhausted(
            f"no free detail name after {MAX_NAME_ATTEMPTS} attempts")
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _one_line(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:_ERROR_LIMIT]


def record_detail_checked(session: dict[str, Any] | None, hint: str,
                          payload: dict[str, Any]
                          ) -> tuple[str | None, dict[str, Any] | None]:
    """Write ``actions/NNNN_<hint>.json`` without ever overwriting a record.

    Returns ``(artifact-relative path, None)`` on success, ``(None, warning)``
    when the write failed (no exception escapes; the warning never carries
    the payload), and ``(None, None)`` when there is no session.
    """
    if not session:
        return None, None
    directory: Path | None = None
    try:
        payload = _with_pretest(session, payload)
        directory = session_mod.artifact_path(session, "actions")
        directory.mkdir(parents=True, exist_ok=True)
        slug = _SLUG.sub("-", hint.lower()).strip("-") or "action"
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        final = _publish(directory, slug, data)
        return str(final.relative_to(session["artifacts_dir"])), None
    except Exception as exc:  # noqa: BLE001 — evidence loss never fails a command
        if directory is None:
            try:
                directory = Path(str(session.get("artifacts_dir", ""))) / "actions"
            except Exception:  # noqa: BLE001
                directory = Path("actions")
        return None, {
            "code": EVIDENCE_WRITE_FAILED,
            "error": _one_line(exc),
            "hint": ("The device action ran; only its detail record was not "
                     f"saved. Check free space and permissions of {directory}."),
            "artifact": "actions",
            "hint_path": str(directory),
        }


def record_detail(session: dict[str, Any] | None, hint: str,
                  payload: dict[str, Any]) -> str | None:
    """Write ``actions/NNNN_<hint>.json``; return the artifact-relative path."""
    return record_detail_checked(session, hint, payload)[0]


def _with_pretest(session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """While a pre-test is active, the detail names it and its open check
    (the check id doubles as the ``task`` when none was given). Without one,
    the payload is written exactly as passed."""
    try:
        from .pretest import store as pretest_store

        attribution = pretest_store.attribution(session)
    except Exception:  # noqa: BLE001
        return payload
    if not attribution:
        return payload
    extended = dict(payload)
    for key, value in attribution.items():
        extended.setdefault(key, value)
    if attribution.get("check_id") and extended.get("task") is None:
        extended["task"] = attribution["check_id"]
    return extended


def read_details(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """All detail records keyed by their artifact-relative path."""
    out: dict[str, dict[str, Any]] = {}
    try:
        directory = session_mod.artifact_path(session, "actions")
        for path in sorted(directory.glob("*.json")):
            try:
                out[f"actions/{path.name}"] = json.loads(
                    path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    except Exception:  # noqa: BLE001
        pass
    return out
