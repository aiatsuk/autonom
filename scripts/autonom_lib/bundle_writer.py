"""Staged, content-addressed, atomically finalized bundle directories.

One writer serves every finalized evidence directory (Report Bundle v2 and the
pre-test pack). Files are written into a private sibling staging directory;
``finalize`` lists every file in ``integrity.json``, writes the optional
descriptor (``manifest.json``) and ``finalized.json``, then renames the staging
directory onto ``out`` in one step. A finalized directory is immutable: a second
finalize with identical content is an idempotent no-op that rewrites nothing, any
other content is refused.

Integrity rules (shared with ``verify_tree``):

- ``integrity.json`` lists every file present in staging at finalize time,
  except ``integrity.json`` itself, ``finalized.json`` and the top-level
  ``annotations/`` tree (mutable review notes written beside the evidence);
- the descriptor given to ``finalize`` is written after the catalogue, so it is
  not listed (the Bundle v2 layout); callers that want the descriptor covered
  write it with ``write_json`` and pass ``None``;
- files present on disk but not listed are ignored by verification.

Files are owner-only (0o600) and directories owner-only (0o700).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any

from . import errors
from .contracts import canonical_json, sha256_file

INTEGRITY_NAME = "integrity.json"
FINALIZED_NAME = "finalized.json"
DESCRIPTOR_NAME = "manifest.json"
ANNOTATIONS_DIR = "annotations"
FINALIZED_SCHEMA = "autonom.finalized/v1"
_RESERVED = (INTEGRITY_NAME, FINALIZED_NAME)


def safe_relative(value: str) -> Path:
    """A relative path that cannot escape the directory it is joined to."""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise errors.AutonomError(
            errors.PATH_FORBIDDEN,
            "artifact path escapes the session evidence root", path=value)
    return path


def blob_relative(digest: str) -> Path:
    return Path("blobs") / "sha256" / digest[:2] / digest


def _excluded(relative: str) -> bool:
    return (relative in _RESERVED
            or relative == ANNOTATIONS_DIR
            or relative.startswith(ANNOTATIONS_DIR + "/"))


def _mkdirs(path: Path, stop: Path) -> None:
    """Create ``path`` and missing parents below ``stop`` as 0o700."""
    missing = []
    current = path
    while current != stop and not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o700)


def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def _refuse_non_empty(out: Path) -> errors.AutonomError:
    return errors.AutonomError(
        errors.REPORT_BUNDLE_FINALIZED,
        "output directory is non-empty and not an Autonom finalized bundle",
        hint="Choose an empty output directory.", bundle=str(out))


def _refuse_differs(out: Path) -> errors.AutonomError:
    return errors.AutonomError(
        errors.REPORT_BUNDLE_FINALIZED,
        "bundle is finalized and differs from this content",
        hint="Choose a new output directory; finalized raw evidence is immutable.",
        bundle=str(out))


class BundleWriter:
    """Build one bundle in a staging directory and finalize it atomically.

    Use as a context manager: an exception inside the block removes the
    staging directory and leaves ``out`` untouched.
    """

    def __init__(self, out: Path) -> None:
        self.out = Path(out)
        if self.out.exists() and not self.out.is_dir():
            raise _refuse_non_empty(self.out)
        if (self.out.is_dir() and any(self.out.iterdir())
                and not (self.out / FINALIZED_NAME).is_file()):
            raise _refuse_non_empty(self.out)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.staging = Path(tempfile.mkdtemp(
            prefix=f".{self.out.name}.staging-", dir=self.out.parent))
        self._closed = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "BundleWriter":
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        if exc_type is not None:
            self.abort()

    def abort(self) -> None:
        """Discard the staging directory; ``out`` is never touched."""
        if not self._closed:
            self._closed = True
            shutil.rmtree(self.staging, ignore_errors=True)

    def _require_open(self) -> None:
        if self._closed:
            raise errors.AutonomError(
                errors.REPORT_BUNDLE_FINALIZED,
                "bundle writer is already finalized or aborted",
                hint="Start a new BundleWriter for another bundle.",
                bundle=str(self.out))

    def _target(self, relative: str | Path) -> tuple[str, Path]:
        self._require_open()
        rel = safe_relative(str(relative))
        posix = rel.as_posix()
        if posix in ("", ".") or posix in _RESERVED:
            raise errors.AutonomError(
                errors.PATH_FORBIDDEN,
                f"bundle path {posix!r} is reserved for finalization",
                hint="integrity.json and finalized.json are written by finalize().",
                path=posix)
        return posix, self.staging / rel

    def _prepare(self, destination: Path) -> None:
        _mkdirs(destination.parent, self.staging)

    # -- content -----------------------------------------------------------
    def ensure_dir(self, relative: str | Path) -> Path:
        _posix, destination = self._target(relative)
        _mkdirs(destination, self.staging)
        return destination

    def write_bytes(self, relative: str | Path, data: bytes) -> dict[str, Any]:
        posix, destination = self._target(relative)
        self._prepare(destination)
        destination.write_bytes(data)
        os.chmod(destination, 0o600)
        return {"path": posix, "size": len(data)}

    def write_json(self, relative: str | Path, value: Any) -> dict[str, Any]:
        return self.write_bytes(relative, canonical_json(value) + b"\n")

    def write_text(self, relative: str | Path, text: str) -> dict[str, Any]:
        return self.write_bytes(relative, text.encode("utf-8"))

    def copy_file(self, source: Path, relative: str | Path) -> dict[str, Any]:
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise errors.AutonomError(
                errors.INVALID_VALUE, "bundle source is not a regular file",
                hint="Pass an existing regular file (symlinks are not copied).",
                path=str(source))
        posix, destination = self._target(relative)
        self._prepare(destination)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        return {"path": posix, "size": destination.stat().st_size}

    def add_blob(self, source: Path) -> dict[str, Any]:
        """Store ``source`` content-addressed under ``blobs/sha256/``."""
        self._require_open()
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise errors.AutonomError(
                errors.INVALID_VALUE, "blob source is not a regular file",
                hint="Pass an existing regular file (symlinks are not copied).",
                path=str(source))
        digest = sha256_file(source)
        relative = blob_relative(digest)
        destination = self.staging / relative
        if not destination.exists():
            self._prepare(destination)
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
        return {"sha256": digest, "path": relative.as_posix(),
                "size": source.stat().st_size}

    def copy_tree(self, source_dir: Path, relative: str | Path) -> dict[str, Any]:
        """Copy a directory tree (for example a nested Bundle v2) under ``relative``."""
        source_dir = Path(source_dir)
        if source_dir.is_symlink() or not source_dir.is_dir():
            raise errors.AutonomError(
                errors.INVALID_VALUE, "bundle tree source is not a directory",
                hint="Pass an existing directory.", path=str(source_dir))
        posix, destination = self._target(relative)
        entries = sorted(source_dir.rglob("*"))
        for entry in entries:
            if entry.is_symlink():
                raise errors.AutonomError(
                    errors.PATH_FORBIDDEN,
                    "bundle tree source contains a symlink",
                    hint="Copy only trees made of regular files and directories.",
                    path=str(entry))
        _mkdirs(destination, self.staging)
        files = 0
        for entry in entries:
            target = destination / entry.relative_to(source_dir)
            if entry.is_dir():
                _mkdirs(target, self.staging)
            elif entry.is_file():
                _mkdirs(target.parent, self.staging)
                shutil.copyfile(entry, target)
                os.chmod(target, 0o600)
                files += 1
        return {"path": posix, "files": files}

    # -- finalization ------------------------------------------------------
    def finalize(self, descriptor: dict[str, Any] | None = None, *,
                 finalized_fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """List, seal and atomically publish the bundle.

        Returns ``{"integrity_sha256", "files", "idempotent", "path"}``.
        """
        self._require_open()
        try:
            if descriptor is not None and (self.staging / DESCRIPTOR_NAME).exists():
                raise errors.AutonomError(
                    errors.PATH_FORBIDDEN,
                    "manifest.json was already written; pass descriptor=None",
                    hint="Either write manifest.json yourself or pass it to finalize().",
                    path=DESCRIPTOR_NAME)
            files = []
            for path in sorted(self.staging.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.staging).as_posix()
                if _excluded(relative):
                    continue
                files.append({"path": relative, "sha256": sha256_file(path),
                              "size": path.stat().st_size})
            integrity_path = self.staging / INTEGRITY_NAME
            integrity_path.write_bytes(
                canonical_json({"algorithm": "sha256", "files": files}) + b"\n")
            os.chmod(integrity_path, 0o600)
            integrity_sha256 = sha256_file(integrity_path)
            if descriptor is not None:
                self.write_json(DESCRIPTOR_NAME, descriptor)
            finalized_path = self.staging / FINALIZED_NAME
            finalized_path.write_bytes(canonical_json({
                **(finalized_fields or {}),
                "schema": FINALIZED_SCHEMA,
                "integrity_sha256": integrity_sha256,
            }) + b"\n")
            os.chmod(finalized_path, 0o600)
            result = {"integrity_sha256": integrity_sha256, "files": len(files),
                      "path": str(self.out)}
            if (self.out / FINALIZED_NAME).is_file():
                if not self._same_as_out(descriptor is not None):
                    raise _refuse_differs(self.out)
                self.abort()
                return {**result, "idempotent": True}
            if self.out.exists():
                if not _is_empty_dir(self.out):
                    raise _refuse_non_empty(self.out)
                self.out.rmdir()
            self.staging.replace(self.out)
            self._closed = True
        except BaseException:
            self.abort()
            raise
        return {**result, "idempotent": False}

    def _same_as_out(self, with_descriptor: bool) -> bool:
        names = [FINALIZED_NAME] + ([DESCRIPTOR_NAME] if with_descriptor else [])
        for name in names:
            existing = self.out / name
            if not existing.is_file():
                return False
            if existing.read_bytes() != (self.staging / name).read_bytes():
                return False
        return True


def verify_tree(root: Path) -> list[dict[str, Any]]:
    """Check every file listed in ``integrity.json``; return the failures.

    A failure is ``{"path", "reason"}`` with reason ``missing``,
    ``digest-mismatch``, ``unreadable``, ``malformed-entry`` or
    ``finalization-mismatch``. Files not listed are ignored. A listed path that
    escapes ``root`` raises ``path_forbidden``.
    """
    root = Path(root)
    integrity_path = root / INTEGRITY_NAME
    if not integrity_path.is_file():
        return [{"path": INTEGRITY_NAME, "reason": "missing"}]
    try:
        integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return [{"path": INTEGRITY_NAME, "reason": "unreadable"}]
    if not isinstance(integrity, dict) or not isinstance(integrity.get("files") or [], list):
        return [{"path": INTEGRITY_NAME, "reason": "unreadable"}]
    failures: list[dict[str, Any]] = []
    finalized_path = root / FINALIZED_NAME
    if not finalized_path.is_file():
        failures.append({"path": FINALIZED_NAME, "reason": "missing"})
    else:
        try:
            record = json.loads(finalized_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            record = None
        if not isinstance(record, dict):
            failures.append({"path": FINALIZED_NAME, "reason": "unreadable"})
        elif record.get("integrity_sha256") != sha256_file(integrity_path):
            failures.append({"path": INTEGRITY_NAME,
                             "reason": "finalization-mismatch"})
    for item in integrity.get("files") or []:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)):
            failures.append({"path": str(item.get("path") if isinstance(item, dict)
                                         else item),
                             "reason": "malformed-entry"})
            continue
        path = root / safe_relative(item["path"])
        if not path.is_file():
            failures.append({"path": item["path"], "reason": "missing"})
        elif sha256_file(path) != item["sha256"]:
            failures.append({"path": item["path"], "reason": "digest-mismatch"})
    return failures
