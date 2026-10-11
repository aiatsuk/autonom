"""QA review of a pre-test pack (tp-018).

``write(pack, decision, ...)`` records one decision as
``<pack>/annotations/review-<n>.json`` (schema ``autonom.pretest-review/v1``).
The ``annotations/`` tree is outside the pack's integrity set, so
``report verify`` stays ok after any number of reviews; the pack itself is
verified before a decision is written, so nobody reviews a tampered pack.

Decisions: ``approve``, ``more-checks`` (optionally with the titles of the
checks to add; the CLI starts the linked child pre-test) and
``return-to-dev`` (needs at least one finding id: ``review_needs_finding``).
``confirm`` and ``dismiss`` take automatic finding ids only
(``not_automatic`` for an agent finding, ``unknown_finding`` for an id the
pack does not hold). Review notes and titles go through the free-text masking.

Every OSError while preparing ``annotations/`` (creating it, reading it, or
making it owner-only, including the fallback chmod) is ``path_forbidden``
with a hint; a review file that cannot be written (an OS error, or text that
cannot be encoded) is ``output_not_writable``, and a partially written review
file is always removed, whatever stopped the write. A pack path that cannot
be resolved is ``not_a_pretest_pack``; malformed JSON in the pack is a
verification failure (``report_integrity_failed``), never a Python error.

Pack files and review files are opened only after ``lstat`` shows a
regular file; anything else is never read.

``status(pack)`` reads every readable decision, oldest first, and returns
the latest one plus the effective confirmed and dismissed automatic findings
(a later decision overrides an earlier one for the same finding).
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable

from .. import errors
from . import pack as pack_mod
from . import store

REVIEW_SCHEMA = "autonom.pretest-review/v1"
DECISIONS = ("approve", "more-checks", "return-to-dev")
ANNOTATIONS_DIR = "annotations"
_REVIEW_FILE = re.compile(r"^review-([1-9]\d*)\.json$")
NOTE_LIMIT = 4000
TITLE_LIMIT = 200
AUTHOR_LIMIT = 100
MAX_ITEMS = 100


def _ids(values: Iterable[Any] | None, what: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    if len(out) > MAX_ITEMS:
        raise errors.AutonomError(errors.INVALID_VALUE, f"at most {MAX_ITEMS} {what}",
                                  hint="Split the review into several decisions.")
    return out


def _load(path: Path) -> Any:
    """JSON of ``path``, read only when ``lstat`` shows a regular file (a
    FIFO, device or symlink is an OSError and never opened, contract 14.3)."""
    return json.loads(pack_mod.read_regular_bytes(path).decode("utf-8"))


def _pack_findings(pack: Path) -> dict[str, dict[str, Any]]:
    try:
        summary = _load(pack / "summary.json")
    except (OSError, ValueError):
        summary = {}
    found: dict[str, dict[str, Any]] = {}
    for item in (summary.get("findings") if isinstance(summary, dict) else None) or []:
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str):
            found[item["finding_id"]] = item
    return found


def _unknown(finding_id: str, pack: Path) -> errors.AutonomError:
    return errors.AutonomError(
        errors.UNKNOWN_FINDING, f"the pack holds no finding {finding_id!r}",
        hint="See the pack's summary.md or index.html for the finding ids (f-1, f-2, ...).",
        finding_id=finding_id, pack=str(pack))


def _pack_id(pack: Path) -> str | None:
    try:
        manifest = _load(pack / "manifest.json")
    except (OSError, ValueError):
        return None
    value = manifest.get("pretest_id") if isinstance(manifest, dict) else None
    return value if isinstance(value, str) else None


def _integrity(pack: Path) -> str | None:
    try:
        finalized = _load(pack / "finalized.json")
    except (OSError, ValueError):
        return None
    value = finalized.get("integrity_sha256") if isinstance(finalized, dict) else None
    return value if isinstance(value, str) else None


def _annotations(pack: Path) -> Path:
    return pack / ANNOTATIONS_DIR


def _unsafe_annotations(folder: Path) -> errors.AutonomError:
    return errors.AutonomError(
        errors.PATH_FORBIDDEN,
        f"{folder} is not a real directory inside the pack",
        hint="annotations/ must be a plain directory in the pack, not a symlink or a "
             "file. Remove it (or replace it with an empty directory) and run the "
             "review again.", path=str(folder))


def _annotations_error(folder: Path, exc: OSError, doing: str) -> errors.AutonomError:
    reason = exc.strerror or type(exc).__name__
    return errors.AutonomError(
        errors.PATH_FORBIDDEN, f"cannot {doing} {folder}: {reason}",
        hint="annotations/ must be a directory inside the pack that belongs to you and "
             "that you can write; fix its owner or permissions (or the pack "
             "directory's), or remove annotations/ and run the review again.",
        path=str(folder))


def _annotations_state(pack: Path) -> Path | None:
    """The annotations directory, or ``None`` when it does not exist yet.

    Refuses (``path_forbidden``) when ``annotations`` is a symlink or anything
    but a real directory, so a shared pack cannot redirect reviews (or the
    owner-only chmod) to a directory outside it.
    """
    folder = _annotations(pack)
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unsafe_annotations(folder) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe_annotations(folder)
    return folder


def _numbers(pack: Path) -> list[tuple[int, Path]]:
    folder = _annotations_state(pack)
    if folder is None:
        return []
    out = []
    try:
        entries = list(folder.iterdir())
    except OSError as exc:
        raise _annotations_error(folder, exc, "read") from exc
    for path in entries:
        match = _REVIEW_FILE.match(path.name)
        if match and pack_mod.entry_kind(path) == "file":
            out.append((int(match.group(1)), path))
    return sorted(out)


def _prepare_annotations(root: Path) -> Path:
    """The pack's owner-only ``annotations/`` directory, created when missing.

    Every OSError (creating it, or making it owner-only, including the
    fallback chmod on systems without ``follow_symlinks``) becomes
    ``path_forbidden`` with a hint."""
    folder = _annotations_state(root)
    if folder is None:
        folder = _annotations(root)
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _annotations_error(folder, exc, "create") from exc
        folder = _annotations_state(root)
        if folder is None:
            raise _unsafe_annotations(_annotations(root))
    try:
        try:
            os.chmod(folder, 0o700, follow_symlinks=False)
        except NotImplementedError:
            # no lchmod here: the lstat above proved a real directory
            os.chmod(folder, 0o700)
    except OSError as exc:
        raise _annotations_error(folder, exc, "make owner-only") from exc
    return folder


def _write_new(path: Path, payload: dict[str, Any]) -> bool:
    """Create ``path`` (0o600) with ``payload``; False when it already exists.
    A write failure removes the partial file and raises ``output_not_writable``."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"cannot write {path}: {exc.strerror or type(exc).__name__}",
            hint="Make the pack's annotations/ directory writable for you and run the "
                 "review again.", path=str(path)) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.chmod(path, 0o600)
    except BaseException as exc:
        # never leave a partial review behind, whatever stopped the write
        try:
            path.unlink()
        except OSError:
            pass
        if not isinstance(exc, (OSError, ValueError)):
            raise
        reason = (exc.strerror if isinstance(exc, OSError) else None) or type(exc).__name__
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE, f"cannot write {path}: {reason}",
            hint="Free disk space or fix the permissions of the pack's annotations/ "
                 "directory (text that cannot be stored as UTF-8 must be retyped), then "
                 "run the review again.", path=str(path)) from exc
    return True


def _root(pack: str | Path) -> Path:
    """The pack directory, resolved; a path that cannot even be resolved (an
    embedded NUL byte, a symlink loop) is ``not_a_pretest_pack``."""
    try:
        return Path(pack).expanduser().resolve()
    except (OSError, ValueError, RuntimeError, TypeError):
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"{pack!r} is not a pre-test pack",
            hint="Pass the directory 'autonom pretest pack' printed.") from None


def write(pack: str | Path, decision: str, *, note: str | None = None,
          checks: Iterable[str] = (), findings: Iterable[str] = (),
          confirm: Iterable[str] = (), dismiss: Iterable[str] = (),
          author: str = "qa") -> dict[str, Any]:
    """Record one review decision beside the pack (see the module text)."""
    root = _root(pack)
    pack_mod.verify(root)
    if decision not in DECISIONS:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"unknown review decision {decision!r}",
            hint="Decisions: " + ", ".join(DECISIONS) + ".", decision=decision)
    finding_ids = _ids(findings, "finding ids")
    confirm_ids = _ids(confirm, "confirmations")
    dismiss_ids = _ids(dismiss, "dismissals")
    if decision == "return-to-dev" and not finding_ids:
        raise errors.AutonomError(
            errors.REVIEW_NEEDS_FINDING, "return-to-dev needs at least one finding",
            hint="Name the findings the developer must fix: --finding f-1 [--finding f-2].")
    both = sorted(set(confirm_ids) & set(dismiss_ids))
    if both:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"a finding cannot be confirmed and dismissed at once: "
                                  f"{', '.join(both)}",
            hint="Pass each automatic finding to either --confirm or --dismiss.",
            finding_ids=both)
    known = _pack_findings(root)
    for finding_id in finding_ids + confirm_ids + dismiss_ids:
        if finding_id not in known:
            raise _unknown(finding_id, root)
    for finding_id in confirm_ids + dismiss_ids:
        if known[finding_id].get("origin") != "auto":
            raise errors.AutonomError(
                errors.NOT_AUTOMATIC,
                f"finding {finding_id} is not automatic; only automatic findings are "
                "confirmed or dismissed",
                hint="An agent finding is already confirmed. Use return-to-dev --finding "
                     f"{finding_id} to send it back, or leave it out.",
                finding_id=finding_id)
    raw_titles = [checks] if isinstance(checks, str) else list(checks or ())
    if len(raw_titles) > MAX_ITEMS:
        raise errors.AutonomError(errors.INVALID_VALUE, f"at most {MAX_ITEMS} checks",
                                  hint="Split the review into several decisions.")
    titles: list[str] = []
    for title in raw_titles:
        clean = store.clip(title, TITLE_LIMIT)
        if not clean:
            raise errors.AutonomError(errors.INVALID_VALUE, "a check title is empty",
                                      hint='Name the check, e.g. --check "Rotate on results".')
        if clean not in titles:
            titles.append(clean)
    clean_author = store.clip(author, AUTHOR_LIMIT) or "qa"
    payload: dict[str, Any] = {
        "schema": REVIEW_SCHEMA,
        "pretest_id": _pack_id(root),
        "pack_integrity_sha256": _integrity(root),
        "decision": decision,
        "note": store.clip(note, NOTE_LIMIT) or None,
        "checks": titles,
        "findings": finding_ids,
        "confirm": confirm_ids,
        "dismiss": dismiss_ids,
        "author": clean_author,
        "created_at": store.now(),
    }
    folder = _prepare_annotations(root)
    numbers = _numbers(root)
    number = (numbers[-1][0] if numbers else 0) + 1
    while True:
        path = folder / f"review-{number}.json"
        payload["review_id"] = f"review-{number}"
        payload["n"] = number
        validate(payload)
        if _write_new(path, payload):
            break
        number += 1
    return {**payload, "path": str(path)}


def review_failures(payload: Any) -> list[str]:
    """Hand-written validator for one review annotation."""
    if not isinstance(payload, dict):
        return ["review must be an object"]
    failures: list[str] = []
    if payload.get("schema") != REVIEW_SCHEMA:
        failures.append(f"schema must be {REVIEW_SCHEMA}")
    if payload.get("decision") not in DECISIONS:
        failures.append("decision must be one of " + ", ".join(DECISIONS))
    for key in ("checks", "findings", "confirm", "dismiss"):
        value = payload.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            failures.append(f"{key} must be a list of strings")
    if not isinstance(payload.get("author"), str) or not payload.get("author"):
        failures.append("author must be a non-empty string")
    if payload.get("note") is not None and not isinstance(payload.get("note"), str):
        failures.append("note must be a string or null")
    if not isinstance(payload.get("created_at"), str):
        failures.append("created_at must be a string")
    number = payload.get("n")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        failures.append("n must be an integer >= 1")
    if payload.get("decision") == "return-to-dev" and not payload.get("findings"):
        failures.append("return-to-dev needs at least one finding")
    return failures


def validate(payload: Any) -> dict[str, Any]:
    failures = review_failures(payload)
    if failures:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"review does not satisfy {REVIEW_SCHEMA}",
            hint="This is a bug in the review writer or a hand-edited annotation.",
            failures=failures)
    return payload


def status(pack: str | Path) -> dict[str, Any]:
    """``{"ok", "pack", "pretest_id", "decision", "latest", "decisions",
    "confirmed", "dismissed", "skipped"}`` — the pack is verified first."""
    root = _root(pack)
    pack_mod.verify(root)
    decisions: list[dict[str, Any]] = []
    skipped: list[str] = []
    for number, path in _numbers(root):
        try:
            payload = _load(path)
        except (OSError, ValueError):
            skipped.append(path.name)
            continue
        if review_failures(payload) or payload.get("n") != number:
            skipped.append(path.name)
            continue
        decisions.append(payload)
    confirmed: dict[str, str] = {}
    dismissed: dict[str, str] = {}
    for payload in decisions:
        for finding_id in payload.get("confirm") or []:
            dismissed.pop(finding_id, None)
            confirmed[finding_id] = payload["author"]
        for finding_id in payload.get("dismiss") or []:
            confirmed.pop(finding_id, None)
            dismissed[finding_id] = payload["author"]
    latest = decisions[-1] if decisions else None
    return {
        "ok": True, "pack": str(root), "pretest_id": _pack_id(root),
        "decision": latest["decision"] if latest else None,
        "latest": latest, "decisions": decisions,
        "confirmed": [{"finding_id": key, "author": value} for key, value in confirmed.items()],
        "dismissed": [{"finding_id": key, "author": value} for key, value in dismissed.items()],
        "skipped": skipped,
    }
