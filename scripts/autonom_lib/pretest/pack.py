"""The pre-test pack (tp-013): one finalized, integrity-checked directory.

``build(session, record, out=None, target=None)`` ends the pre-test and
writes, through ``bundle_writer.BundleWriter`` (a private staging directory
and one atomic rename)::

    <pack>/
      manifest.json          kind autonom.pretest-pack/v1 (listed in integrity)
      pretest.json           the record as packed (see below)
      ticket.md context.json baseline.json budget.json health.json
      checks.ndjson findings.ndjson         the raw event logs
      checks/<chk-N>.json    folded check + evidence ids + repro steps
      findings/<f-N>.json    folded finding + evidence ids + repro steps
      runs/<run_id>/         a nested Report Bundle v2 per flow run
      blobs/sha256/ab/<digest>              content-addressed evidence
      summary.json summary.md index.html
      integrity.json finalized.json
      annotations/           review decisions, written later, outside integrity

Order: first the pack's parent directory is created and a test file is
written and removed there (``output_not_writable`` otherwise, with every
check left as it was); then an open check is ended ``inconclusive`` ("open
at pack time"), and if the pack still cannot be written before it is
finalized, the checks this call ended are open again. Then the
pre-test's end time is taken, health is computed and its automatic
findings written, then the pack is staged and finalized, and only then is
the end time saved and the record marked ``packed`` (which clears the active
pointer). The end time (``ended_at``, and so ``timing.wall_s``) is the time
of the successful pack: a failed attempt saves none, and a retry takes its
own. A stopped pre-test keeps the time it was stopped. The whole build holds
the pre-test lock, so no check or finding lands half way.

Evidence copied into blobs: screenshots whose index row names this
pre-test, saved UI trees and recordings whose file time falls in a check's
window, action details that name this pre-test, crash texts of new crashes,
and everything a finding's evidence refs resolve to. Text evidence is
masked on the way in (trees and action details through ``redact_value`` and
the free-text masking; typed text of every input action, ``ui type`` from
the CLI and ``text`` from Canvas alike, is always ``<redacted>``;
requests keep method, host, path and status only, never headers or bodies;
a log ref keeps its window, never the lines). Screenshots and recordings are
copied as they are. A screenshot whose index row (or, without the field, its
PNG metadata) says mocks were active is marked ``mocked: true`` with
``mocks_active: N`` in its evidence item.

Each check also lists the requests captured in its time window
(``check_requests``: method, host, path without the query, status, mocked,
duration and start time when known; never headers or bodies), at most
``MAX_CHECK_REQUESTS`` per check with ``requests_truncated`` counting the rest.
A request that never completed (its store row says ``completed: false``)
keeps ``completed: false`` and its ``error_kind`` (``client_abort``,
``upstream_error``, ``hang_cap`` or ``other``); a completed request carries
neither field.
A finding's steps run from its check's start to its latest evidence
(``repro.cut_at_evidence``).

``pretest.json`` inside the pack says ``status: "packed"`` and names the pack
path, with an empty ``integrity_sha256``: a file cannot hold the digest of
the catalogue that lists it. The digest lives in ``finalized.json`` and in the
session's own record.

Re-packing is idempotent: a packed pre-test is verified and its pack
returned unchanged (``idempotent: true``). A pack that was finalized but whose
record was never marked (a crash between the two) is verified and adopted
only when it still matches the pre-test. The match compares the sealed record
digest in the pack's manifest (``record_digest``: the folded checks, the
folded findings, the health report without its ``computed_at``, and the
record without ``status``, ``pack`` and ``ended_at``) with the same digest of
the pre-test as it is now (contract section 13.4); the end time is never
compared, so a ``pretest stop`` after the crash does not make a matching pack
stale. When the pre-test changed after that pack was sealed, the build
refuses with ``report_bundle_finalized`` (``stale: true`` and the names of the
differing record files) and leaves both the old pack and the record as they
are. Every refusal about a pack that already exists (another ``--out`` for
a packed pre-test, a changed pack, a stale pack found by ``_adopt``) uses
``FINALIZED_HINT``: verify the original pack or start a new pre-test; no
hint says to pack again into a new directory.

``verify(pack)`` first walks the whole pack with ``lstat`` only and refuses
it, reading nothing, when any entry is a symlink or special file (contract
section 16); then it checks every listed digest, the finalization record and
each nested bundle (``report_bundle.verify``), and reports every file or
directory the catalogue does not cover, except OS metadata files; it opens a
file only after ``lstat`` shows a regular file (see ``verify``). Empty
directories of a nested bundle are pruned before it is copied in, so a pack
holds no directory without a listed file;
``is_pack(path)`` says whether a directory is a pre-test pack (never raises).
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .. import contracts, errors
from .. import bundle_writer as bundle_writer_mod
from .. import report_bundle
from ..bundle_writer import BundleWriter, blob_relative
from ..network import redact
from . import checks as checks_mod
from . import findings as findings_mod
from . import health as health_mod
from . import kit as kit_mod
from . import repro as repro_mod
from . import store
from . import summary as summary_mod
from . import verdict as verdict_mod
from . import view as view_mod

PACK_KIND = "autonom.pretest-pack/v1"
PACK_DIR = "pack"
OPEN_AT_PACK = "open at pack time"
RECORD_FILES = (store.TICKET_FILE, store.CONTEXT_FILE, store.BASELINE_FILE,
                store.BUDGET_FILE, health_mod.HEALTH_FILE, store.CHECKS_FILE,
                store.FINDINGS_FILE)
CORE_FILES = ("manifest.json", store.RECORD_FILE, "summary.json", "summary.md",
              "index.html")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Files an operating system drops into any directory it shows (Finder,
# Explorer); compared by exact name (contract section 17), plus AppleDouble
# "._<name>" files.
_OS_METADATA = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_RECORDING_SUFFIXES = (".mp4", ".mov", ".webm")
MAX_CHECK_REQUESTS = 200
# The hint of every refusal about a pack that already exists (a tampered
# pack, a finalized re-pack, an earlier pack in --out): a pre-test has one
# pack, so the hint never says to pack it again.
FINALIZED_HINT = ("this pre-test is already packed; verify the original pack with "
                  "'autonom report verify <pack>' or start a new pre-test")
# The hint of a pack that fails verification (``verify``).
DAMAGED_HINT = ("Packs are immutable: restore the pack from a copy. A packed pre-test is "
                "never packed again; start a new pre-test to test the ticket again.")


# --- small helpers ---------------------------------------------------------------------


def _scrub(value: Any) -> Any:
    """``redact_value`` (secret-named keys) plus the free-text masking on
    every string, recursively."""
    value = contracts.redact_value(value)

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: walk(inner) for key, inner in item.items()}
        if isinstance(item, list):
            return [walk(inner) for inner in item]
        if isinstance(item, str):
            return store.redact_text(item)
        return item

    return walk(value)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       default=str) + "\n").encode("utf-8")


def _blob_bytes(writer: BundleWriter, data: bytes) -> dict[str, Any]:
    """``add_blob`` for bytes built in memory (masked text evidence)."""
    digest = contracts.sha256_bytes(data)
    relative = blob_relative(digest)
    if not (writer.staging / relative).exists():
        writer.write_bytes(relative, data)
    return {"sha256": digest, "path": relative.as_posix(), "size": len(data)}


def _inside(base: Path, value: str) -> Path | None:
    if not value or "\0" in value or Path(value).is_absolute():
        return None
    candidate = (base / value).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _regular(path: Path | None) -> bool:
    return path is not None and path.is_file() and not path.is_symlink()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read_regular_bytes(path: Path) -> bytes:
    """The bytes of ``path``, opened only after ``lstat`` shows a regular
    file (contract section 14.3): a symlink, FIFO, socket, device node or
    directory is an ``OSError`` and is never opened. The open itself does not
    follow a symlink and does not block, and the opened file is checked again,
    so a file swapped in between is refused too."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        return handle.read()


def _read_json(path: Path) -> Any:
    """JSON of a regular file (``read_regular_bytes``); ``None`` when it is
    missing, not a regular file, unreadable or not JSON."""
    try:
        return json.loads(read_regular_bytes(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def is_pack(path: str | Path) -> bool:
    """Whether ``path`` holds a pre-test pack manifest. Never raises."""
    try:
        manifest = _read_json(Path(path).expanduser() / bundle_writer_mod.DESCRIPTOR_NAME)
        return isinstance(manifest, dict) and manifest.get("kind") == PACK_KIND
    except Exception:  # noqa: BLE001
        return False


# --- evidence ----------------------------------------------------------------------------


class _Evidence:
    """The ``summary.evidence`` list, deduplicated by source."""

    def __init__(self, writer: BundleWriter, known_checks: set[str]) -> None:
        self.writer = writer
        self.known = known_checks
        self.items: list[dict[str, Any]] = []
        self.by_key: dict[str, str] = {}
        self.warnings: list[dict[str, Any]] = []

    def _owner(self, check_id: Any) -> str | None:
        return check_id if isinstance(check_id, str) and check_id in self.known else None

    def add(self, key: str, kind: str, path: str, label: str | None,
            check_id: Any = None, **extra: Any) -> str:
        if key in self.by_key:
            eid = self.by_key[key]
            item = next(entry for entry in self.items if entry["id"] == eid)
            if item.get("check_id") is None and self._owner(check_id):
                item["check_id"] = self._owner(check_id)
            return eid
        eid = f"e-{len(self.items) + 1}"
        item: dict[str, Any] = {"id": eid, "kind": kind, "path": path,
                                "label": store.clip(label, 200) if label else None,
                                "check_id": self._owner(check_id)}
        for name, value in extra.items():
            if value is not None:
                item[name] = value
        self.items.append(item)
        self.by_key[key] = eid
        return eid

    def file(self, key: str, kind: str, source: Path, label: str | None,
             check_id: Any = None, **extra: Any) -> str | None:
        if key in self.by_key:
            return self.add(key, kind, "", label, check_id)
        try:
            blob = self.writer.add_blob(source)
        except (errors.AutonomError, OSError) as exc:
            self.warn(key, f"could not copy: {getattr(exc, 'message', exc)}")
            return None
        return self.add(key, kind, blob["path"], label, check_id, **extra)

    def data(self, key: str, kind: str, value: Any, label: str | None,
             check_id: Any = None) -> str:
        if key in self.by_key:
            return self.add(key, kind, "", label, check_id)
        blob = _blob_bytes(self.writer, _json_bytes(_scrub(value)))
        return self.add(key, kind, blob["path"], label, check_id)

    def text(self, key: str, kind: str, text: str, label: str | None,
             check_id: Any = None) -> str:
        if key in self.by_key:
            return self.add(key, kind, "", label, check_id)
        blob = _blob_bytes(self.writer, (store.redact_text(text) or "").encode("utf-8"))
        return self.add(key, kind, blob["path"], label, check_id)

    def warn(self, what: str, reason: str) -> None:
        self.warnings.append({"evidence": what, "reason": store.clip(reason, 300)})


# Top-level fields of an action detail that hold what was typed or pasted:
# ``text`` (``ui type`` from the CLI, ``text`` from Canvas, both the dispatched
# op and the recorded kind), and ``value``, ``typed``, ``input`` and
# ``clipboard`` (any other input action).
# A tap or find keeps its selector text: that is nested under ``selector`` or
# ``node`` and names what the screen showed, not what was typed.
_TYPED_FIELDS = ("text", "value", "typed", "input", "clipboard")


def _action_payload(payload: Any) -> Any:
    """An action detail with every typed value replaced by ``<redacted>``, for
    every action kind and regardless of ``sensitive`` (contract section 10)."""
    if not isinstance(payload, dict):
        return payload
    if not any(payload.get(name) not in (None, "") for name in _TYPED_FIELDS):
        return payload
    payload = dict(payload)
    for name in _TYPED_FIELDS:
        if payload.get(name) not in (None, ""):
            payload[name] = redact.PLACEHOLDER
    return payload


def _collect_session_evidence(session: dict[str, Any], record: dict[str, Any],
                              checks: list[dict[str, Any]], health: dict[str, Any],
                              evidence: _Evidence) -> None:
    from .. import screenshot as screenshot_mod

    artifacts = Path(session["artifacts_dir"])
    pretest_id = record["pretest_id"]
    # screenshots that name this pre-test
    try:
        rows = screenshot_mod.load_index(session)
    except (OSError, UnicodeDecodeError) as exc:
        evidence.warn("shots/index.jsonl", f"unreadable: {exc}")
        rows = []
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or row.get("pretest_id") != pretest_id:
            continue
        path = _inside(artifacts, str(row.get("file") or ""))
        if not _regular(path):
            evidence.warn(f"shot:{number}", "the screenshot file is missing")
            continue
        evidence.file(f"shot:{number}", "screenshot", path,
                      row.get("label") or Path(str(row.get("file"))).name,
                      row.get("check_id"), width=row.get("width"), height=row.get("height"),
                      **_mocked(row, path))
    # saved trees taken during a check
    for path in sorted((artifacts / "trees").glob("[0-9]*.json")):
        owner = health_mod.attribute(_mtime(path), checks)
        if owner and _regular(path):
            tree_evidence(evidence, path, owner)
    # action details that name this pre-test
    for path in sorted((artifacts / "actions").glob("*.json")):
        payload = _read_json(path) if _regular(path) else None
        if isinstance(payload, dict) and payload.get("pretest_id") == pretest_id:
            evidence.data(f"action:{path.stem}", "action", _action_payload(payload),
                          path.stem, payload.get("check_id"))
    # recordings made during a check
    recordings = artifacts / "recordings"
    if recordings.is_dir():
        for path in sorted(recordings.iterdir()):
            if path.suffix.lower() not in _RECORDING_SUFFIXES or path.stem == "latest" \
                    or not _regular(path):
                continue
            owner = health_mod.attribute(_mtime(path), checks)
            if owner:
                evidence.file(f"recording:{path.name}", "recording", path, path.name, owner)
    # crash texts of new crashes
    crashes = (health.get("crashes") or {}) if isinstance(health, dict) else {}
    for crash in crashes.get("new") or []:
        if isinstance(crash, dict) and crash.get("name"):
            crash_evidence(session, evidence, str(crash["name"]), crash.get("check_id"),
                           crash)


def _mock_count(value: Any) -> int | None:
    """``mocks_active`` as written by a capture: a count, or (older rows and
    test fixtures) the list of active rules; None when absent or unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _mocked(row: Any, path: Path | None) -> dict[str, Any]:
    """``{"mocked": True, "mocks_active": N}`` for a screenshot taken while N
    mocks were active (the index row first, else the PNG's own metadata);
    ``{}`` otherwise."""
    count = _mock_count(row.get("mocks_active")) if isinstance(row, dict) else None
    if count is None and _regular(path):
        from .. import screenshot as screenshot_mod

        try:
            count = _mock_count(screenshot_mod.read_metadata(path).get("mocks_active"))
        except Exception:  # noqa: BLE001 — unreadable metadata marks nothing
            count = None
    return {"mocked": True, "mocks_active": count} if count else {}


def _flow_stamp(row: dict[str, Any]) -> Any:
    millis = row.get("started_at_ms")
    if isinstance(millis, (int, float)) and not isinstance(millis, bool):
        return millis / 1000.0
    return row.get("started_at")


def check_requests(session: dict[str, Any], checks: list[dict[str, Any]]
                   ) -> dict[str, dict[str, Any]]:
    """``{check_id: {"requests": [...], "truncated": N}}``: the captured
    requests whose start falls in each check's window (see the module text).
    Nothing when no traffic was captured or the store cannot be read."""
    from ..network import store as network_store

    out = {check["check_id"]: {"requests": [], "truncated": 0} for check in checks
           if isinstance(check, dict) and check.get("check_id")}
    try:
        text = read_regular_bytes(network_store.flows_path(session)).decode(
            "utf-8", errors="replace")
    except (OSError, KeyError, TypeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):  # a torn or hostile line is skipped
            continue
        # Too deep is skipped whether the parser refused it (RecursionError on
        # older Pythons) or read it, so every version keeps the same rows.
        if not isinstance(row, dict) or network_store.too_deep(row):
            continue
        stamp = _flow_stamp(row)
        owner = health_mod.attribute(stamp, checks)
        if owner not in out:
            continue
        bucket = out[owner]
        if len(bucket["requests"]) >= MAX_CHECK_REQUESTS:
            bucket["truncated"] += 1
            continue
        status = row.get("status")
        try:
            path = str(row.get("path") or "").split("?", 1)[0].split("#", 1)[0]
            item: dict[str, Any] = {
                "method": store.clip(row.get("method"), 16),
                "host": store.clip(row.get("host"), 255),
                "path": store.clip(path, 500) if path else None,
                "status": status if isinstance(status, int) and not isinstance(status, bool)
                else None,
                "mocked": bool(row.get("mocked") or row.get("mock_id")),
            }
        except RecursionError:  # a value nested too deep to print: skip the line
            continue
        if row.get("completed") is False:
            item["completed"] = False
            kind = row.get("error_kind")
            item["error_kind"] = (store.clip(kind, 32) if isinstance(kind, str) else None) \
                or "other"
        duration = row.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
                and duration >= 0:
            item["duration_ms"] = int(duration)
        started = health_mod._to_datetime(stamp)  # noqa: SLF001
        if started is not None:
            item["started_at"] = health_mod._iso(started)  # noqa: SLF001
        bucket["requests"].append(item)
    return out


def tree_evidence(evidence: _Evidence, path: Path, owner: Any) -> str | None:
    payload = _read_json(path)
    if payload is None:
        evidence.warn(f"tree:{path.name}", "the tree file is unreadable")
        return None
    return evidence.data(f"tree:{path.name}", "tree", payload, path.name, owner)


def crash_evidence(session: dict[str, Any], evidence: _Evidence, name: str, owner: Any,
                   data: dict[str, Any] | None = None) -> str:
    folder = Path(session["artifacts_dir"]) / "crashes"
    for suffix in ("", ".txt", ".ips"):
        path = _inside(folder, name + suffix)
        if _regular(path):
            try:
                text = read_regular_bytes(path).decode("utf-8", errors="replace")
            except OSError:
                break
            return evidence.text(f"crash:{name}", "crash", text, name, owner)
    shown = {"name": name}
    if isinstance(data, dict):
        shown.update({key: data.get(key) for key in ("time", "check_id") if key in data})
    return evidence.data(f"crash:{name}", "crash", shown, name, owner)


def _ref_evidence(session: dict[str, Any], evidence: _Evidence, ref: str,
                  owner: Any, nested: dict[str, str]) -> str | None:
    try:
        resolved = findings_mod.resolve_ref(session, ref)
    except errors.AutonomError as exc:
        evidence.warn(ref, exc.message)
        return None
    kind, data = resolved.get("kind"), resolved.get("data") or {}
    path = Path(resolved["path"]) if resolved.get("path") else None
    if kind == "shot":
        return evidence.file(f"shot:{data.get('index')}", "screenshot", path,
                             data.get("label") or (path.name if path else ref), owner,
                             width=data.get("width"), height=data.get("height"),
                             **_mocked(data, path))
    if kind == "tree":
        return tree_evidence(evidence, path, owner)
    if kind == "action":
        payload = _read_json(path)
        if payload is None:
            evidence.warn(ref, "the action detail is unreadable")
            return None
        return evidence.data(f"action:{path.stem}", "action", _action_payload(payload),
                             path.stem, owner)
    if kind == "request":
        shown = {key: data.get(key) for key in ("id", "method", "host", "path", "status")}
        label = f"{data.get('method') or '?'} {data.get('host') or ''}{data.get('path') or ''}"
        if data.get("status") is not None:
            label += f" → {data.get('status')}"
        return evidence.data(ref, "request", shown, label, owner)
    if kind == "log":
        return evidence.data(ref, "log", {"from": data.get("from"), "to": data.get("to")},
                             f"Log window {data.get('from')} .. {data.get('to')}", owner)
    if kind == "run":
        run_id = str(data.get("run_id"))
        if run_id not in nested:
            evidence.warn(ref, "the flow run could not be nested")
            return None
        return evidence.add(f"run:{run_id}", "run", nested[run_id], f"Flow run {run_id}",
                            owner)
    if kind == "crash":
        return crash_evidence(session, evidence, str(data.get("name") or ref.partition(":")[2]),
                              owner, data)
    evidence.warn(ref, f"unknown evidence kind {kind!r}")
    return None


# --- nested runs ---------------------------------------------------------------------------


def _run_ids(checks: list[dict[str, Any]], findings: list[dict[str, Any]]
             ) -> list[tuple[str, str | None]]:
    """(run id, owning check) in first-seen order."""
    out: dict[str, str | None] = {}
    for check in checks:
        for run_id in check.get("run_ids") or []:
            out.setdefault(str(run_id), check["check_id"])
    for finding in findings:
        for ref in finding.get("evidence") or []:
            if isinstance(ref, str) and ref.startswith("run:"):
                out.setdefault(ref[4:].strip(), finding.get("check_id"))
        for run_id in finding.get("repro_runs") or []:
            out.setdefault(str(run_id), finding.get("check_id"))
    return list(out.items())


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty directories under ``root`` (a nested bundle in scratch):
    a pack holds no directory without a listed file, so ``verify`` can report
    every unlisted directory (contract section 14.2)."""
    for current, _dirs, _names in os.walk(root, topdown=False):
        if Path(current) == root:
            continue
        try:
            # bottom-up, so a parent of removed empty directories goes too;
            # a directory with anything left in it refuses (ENOTEMPTY)
            os.rmdir(current)
        except OSError:
            pass


def _nest_runs(session: dict[str, Any], writer: BundleWriter, scratch: Path,
               runs: list[tuple[str, str | None]], evidence: _Evidence
               ) -> tuple[dict[str, str], list[dict[str, Any]]]:
    from ..flow import report as flow_report

    artifacts = Path(session["artifacts_dir"])
    nested: dict[str, str] = {}
    unavailable: list[dict[str, Any]] = []
    for run_id, owner in runs:
        if not _RUN_ID.match(run_id) or ".." in run_id:
            unavailable.append({"run_id": store.clip(run_id, 128), "reason": "not a run id"})
            continue
        out = scratch / run_id
        try:
            manifest = flow_report.load_manifest(artifacts / "flows" / run_id)
            report_bundle.build(manifest, artifacts_root=artifacts, out=out)
            _prune_empty_dirs(out)
            report_bundle.verify(out)
            writer.copy_tree(out, f"runs/{run_id}")
        except errors.AutonomError as exc:
            unavailable.append({"run_id": run_id, "reason": store.clip(exc.message, 300)})
            continue
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unavailable.append({"run_id": run_id,
                                "reason": store.clip(f"{type(exc).__name__}: {exc}", 300)})
            continue
        nested[run_id] = f"runs/{run_id}/"
        evidence.add(f"run:{run_id}", "run", nested[run_id], f"Flow run {run_id}", owner)
    return nested, unavailable


# --- parents (more-checks and rerun children) -------------------------------------------------


def _parent_summary(record: dict[str, Any]) -> dict[str, Any] | None:
    parent = record.get("parent_pack") or (record.get("rerun_of") or {}).get("pack")
    if not parent:
        return None
    doc = _read_json(Path(str(parent)).expanduser() / "summary.json")
    return doc if isinstance(doc, dict) else None


def _before_after(record: dict[str, Any], checks: list[dict[str, Any]],
                  parent: dict[str, Any] | None, check_evidence: dict[str, list[str]]
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """For a rerun child: each check's parent verdict (matched by title among
    the rerun's chosen checks) and its own."""
    if record.get("kind") != "rerun" or not parent:
        return {}, {}
    chosen = set((record.get("rerun_of") or {}).get("checks") or [])
    pool = [item for item in parent.get("checks") or [] if isinstance(item, dict)
            and (not chosen or item.get("check_id") in chosen)]
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    used: set[str] = set()
    for check in checks:
        match = next((item for item in pool if item.get("title") == check.get("title")
                      and item.get("check_id") not in used), None)
        if match is None:
            continue
        used.add(str(match.get("check_id")))
        before[check["check_id"]] = {"check_id": match.get("check_id"),
                                     "verdict": match.get("verdict"),
                                     "evidence": list(match.get("evidence") or [])}
        after[check["check_id"]] = {"verdict": check.get("verdict"),
                                    "evidence": list(check_evidence.get(check["check_id"]) or [])}
    return before, after


# --- build -----------------------------------------------------------------------------------


def _refuse_out(out: Path, why: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.REPORT_BUNDLE_FINALIZED, f"cannot write the pack to {out}: {why}",
        hint="Choose an empty or new --out directory, or omit --out for the default "
             "<pre-test>/pack.", out=str(out))


def _out_path(out: str | Path) -> Path:
    """``--out`` as an absolute path; one that cannot be resolved (an unknown
    ``~user``, a NUL byte, a symlink loop) is refused before anything changes."""
    try:
        return Path(out).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise errors.AutonomError(
            errors.REPORT_BUNDLE_FINALIZED, f"cannot write the pack to {out!r}: "
            f"the path cannot be resolved ({type(exc).__name__})",
            hint="Choose an empty or new --out directory, or omit --out for the default "
                 "<pre-test>/pack.", out=str(out)) from None


def _result(record: dict[str, Any], pack: Path, integrity: str, verdict: str | None,
            idempotent: bool, **extra: Any) -> dict[str, Any]:
    return {"pack": str(pack), "integrity_sha256": integrity, "verdict": verdict,
            "idempotent": idempotent, "pretest_id": record["pretest_id"], **extra}


def _existing(record: dict[str, Any], pack: Path, hint: str = FINALIZED_HINT
              ) -> dict[str, Any]:
    try:
        verified = verify(pack)
    except errors.AutonomError as exc:
        if exc.code == errors.REPORT_INTEGRITY_FAILED:
            # the pack of this pre-test was changed: the hint names what the
            # tool allows from here (never "pack it again" for a packed one)
            exc.hint = hint
        raise
    summary = _read_json(pack / "summary.json") or {}
    finalized = _read_json(pack / bundle_writer_mod.FINALIZED_NAME) or {}
    return _result(record, pack, str(finalized.get("integrity_sha256") or ""),
                   summary.get("verdict"), True, files=verified["files"], warnings=[])


def build(session: dict[str, Any], record: dict[str, Any], *, out: str | Path | None = None,
          target: Any = None) -> dict[str, Any]:
    """Pack the pre-test (see the module text).

    ``{"pack", "integrity_sha256", "verdict", "idempotent", "pretest_id",
    "files", "warnings"}``."""
    if not session or not session.get("artifacts_dir"):
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION, "packing needs the pre-test's session",
            hint="Run 'autonom pretest pack' inside the session that holds the pre-test.")
    if not isinstance(record, dict) or not isinstance(record.get("pretest_id"), str):
        raise errors.AutonomError(
            errors.INVALID_VALUE, "pack needs a pre-test record",
            hint="Pass the record from 'store.require_active' or 'store.load'.")
    pretest_id = record["pretest_id"]
    directory = store.pretest_dir(session, pretest_id)
    default = directory / PACK_DIR
    with store.lock(session, pretest_id):
        current = store.load(session, pretest_id)
        wanted = _out_path(out) if out is not None else None
        if current.get("status") == "packed":
            recorded = Path(current["pack"]["path"])
            if wanted is not None and wanted != recorded.resolve():
                raise errors.AutonomError(
                    errors.PRETEST_FINALIZED,
                    f"pre-test {pretest_id} is already packed at {recorded}",
                    hint=FINALIZED_HINT, pretest_id=pretest_id, pack=str(recorded))
            result = _existing(current, recorded)
            record.clear()
            record.update(current)
            return result
        target_dir = wanted if wanted is not None else default
        if target_dir.exists() and (not target_dir.is_dir() or any(target_dir.iterdir())):
            adopted = _adopt(session, current, target_dir)
            record.clear()
            record.update(current)
            return adopted
        result = _build_locked(session, current, target_dir, target)
        record.clear()
        record.update(current)
        return result


def _adopt(session: dict[str, Any], record: dict[str, Any], out: Path) -> dict[str, Any]:
    """A finalized pack of this very pre-test whose record was never marked
    (a crash between finalize and mark): verify it and mark the record.

    Only ``lstat`` is used until the up-front scan of contract section 16
    has passed; a pack holding a symlink or special file anywhere is
    ``report_integrity_failed`` before its manifest or record is opened."""
    if not out.is_dir() or not _plain_file(out / bundle_writer_mod.FINALIZED_NAME) \
            or entry_kind(out / bundle_writer_mod.DESCRIPTOR_NAME) is None:
        raise _refuse_out(out, "the directory is not empty")
    refusal = _scan_refusal(out)
    if refusal is not None:
        raise refusal
    if not is_pack(out):
        raise _refuse_out(out, "the directory is not empty")
    if not _plain_file(out / store.RECORD_FILE):
        # never open a FIFO, device or symlink in place of the record
        raise _refuse_out(out, f"its {store.RECORD_FILE} is not a regular file")
    try:
        inner = store.find_pack_record(out)
    except errors.AutonomError:
        raise _refuse_out(out, "it holds another pack") from None
    if inner.get("pretest_id") != record["pretest_id"]:
        raise _refuse_out(out, f"it holds the pack of {inner.get('pretest_id')}")
    result = _existing(record, out)
    differs = _pack_differences(session, record, out)
    sealed_end = inner.get("ended_at")
    if differs:
        # The pre-test changed after that pack was sealed (a check, finding,
        # action or health change since): adopting it would drop that work and
        # return a stale verdict. The old pack stays as it is (packs are
        # immutable); the pre-test stays unpacked.
        raise errors.AutonomError(
            errors.REPORT_BUNDLE_FINALIZED,
            f"{out} holds an earlier pack of {record['pretest_id']} that no longer "
            "matches the pre-test",
            hint=FINALIZED_HINT, out=str(out), pretest_id=record["pretest_id"], stale=True, differs=differs)
    _seal(session, record, out, result["integrity_sha256"], sealed_end)
    return result


def _seal(session: dict[str, Any], record: dict[str, Any], out: Path, integrity: str,
          ended_at: Any) -> None:
    """Save the pack's end time, then mark the record packed (both after the
    pack is finalized, so a failed attempt never fixes the end time)."""
    current = store.load(session, record["pretest_id"])
    if ended_at and current.get("ended_at") != ended_at:
        current["ended_at"] = ended_at
        store.save(session, current)
    store.mark_packed(session, record, {"path": str(out), "integrity_sha256": integrity})


# Record fields the pack itself changes (status, pack) or that a stop after a
# crash sets (ended_at): never part of the sealed record digest.
_RECORD_VOLATILE = ("status", "pack", "ended_at")
# digest part -> the record file it stands for (named in a stale refusal)
_DIGEST_PARTS = (("record", store.RECORD_FILE), ("checks", store.CHECKS_FILE),
                 ("findings", store.FINDINGS_FILE), ("health", health_mod.HEALTH_FILE))


def record_digest(session: dict[str, Any], record: dict[str, Any]) -> dict[str, str]:
    """The sealed record digest (contract section 13.4): one SHA-256 per part
    (``record``, ``checks``, ``findings``, ``health``) over canonical JSON, and
    ``sha256`` over the four.

    ``checks`` and ``findings`` are the folded states (a torn line that the
    readers skip changes nothing); ``health`` is ``health.json`` without
    ``computed_at`` (``None`` when the file is missing or unreadable); the
    record leaves out ``status``, ``pack`` and ``ended_at``."""
    directory = store.pretest_dir(session, record["pretest_id"])
    health = _read_json(directory / health_mod.HEALTH_FILE)
    if isinstance(health, dict):
        health = {key: value for key, value in health.items() if key != "computed_at"}
    values = {
        "record": {key: value for key, value in record.items()
                   if key not in _RECORD_VOLATILE},
        "checks": checks_mod.fold(session, record),
        "findings": findings_mod.fold(session, record),
        "health": health,
    }
    parts = {name: contracts.sha256_bytes(_canonical(values[name]))
             for name, _ in _DIGEST_PARTS}
    parts["sha256"] = contracts.sha256_bytes(_canonical(
        [parts[name] for name, _ in _DIGEST_PARTS]))
    return parts


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def _pack_differences(session: dict[str, Any], record: dict[str, Any],
                      out: Path) -> list[str]:
    """Names of the record files whose digest part differs between the pack
    at ``out`` (its manifest's ``record_digest``) and the pre-test as it is
    now; empty when the pack is the pre-test as it is. A manifest without a
    well-formed digest differs in every part."""
    manifest = _read_json(out / bundle_writer_mod.DESCRIPTOR_NAME)
    sealed = manifest.get("record_digest") if isinstance(manifest, dict) else None
    if not isinstance(sealed, dict):
        sealed = {}
    current = record_digest(session, record)
    if sealed.get("sha256") == current["sha256"]:
        return []
    differs = [file_name for name, file_name in _DIGEST_PARTS
               if sealed.get(name) != current[name]]
    return differs or [bundle_writer_mod.DESCRIPTOR_NAME]


def _probe_out(out: Path) -> None:
    """Refuse with ``output_not_writable`` unless the pack's parent directory
    can be created and written (a test file is created there and removed).
    Runs before any check is closed, so a pack that cannot be written leaves
    the pre-test as it was."""
    parent = out.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".pack-write-test-", dir=parent)
        try:
            os.write(fd, b"x")
        finally:
            os.close(fd)
            os.unlink(name)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"cannot write the pack to {out}: {parent} cannot be written ({reason})",
            hint="Choose an --out directory whose parent you can write, or omit --out for "
                 "the default <pre-test>/pack. Open checks were left open.",
            out=str(out), reason=reason) from None


def _checks_size(path: Path) -> int | None:
    try:
        return os.lstat(path).st_size
    except OSError:
        return None


def _reopen(session: dict[str, Any], pretest_id: str, path: Path, size: int | None,
            closed: list[str]) -> bool:
    """Undo the ``end`` events this build appended (the caller holds the
    pre-test lock, so nothing else was appended): cut ``checks.ndjson`` back
    to its size before them and point ``active.json`` at the open check
    again. Returns whether the checks are open again."""
    try:
        if size is None:
            os.unlink(path)
        else:
            os.truncate(path, size)
        store.set_open_check(session, pretest_id, closed[-1])
    except (OSError, errors.AutonomError):
        return False
    return True


def _build_locked(session: dict[str, Any], record: dict[str, Any], out: Path,
                  target: Any) -> dict[str, Any]:
    pretest_id = record["pretest_id"]
    directory = store.pretest_dir(session, pretest_id)
    # 0. the pack's parent must be writable before anything changes
    _probe_out(out)
    checks_path = directory / store.CHECKS_FILE
    checks_size = _checks_size(checks_path)
    closed: list[str] = []
    try:
        return _build_closing(session, record, out, target, closed)
    except BaseException as exc:
        # the bundle could not be written after all: the checks this call
        # closed are open again, so they can still be ended by hand
        if closed and _reopen(session, pretest_id, checks_path, checks_size, closed):
            if isinstance(exc, errors.AutonomError):
                exc.extra.setdefault("reopened_checks", list(closed))
        raise


def _build_closing(session: dict[str, Any], record: dict[str, Any], out: Path,
                   target: Any, closed: list[str]) -> dict[str, Any]:
    pretest_id = record["pretest_id"]
    directory = store.pretest_dir(session, pretest_id)
    # 1. end the pre-test: no check stays open, the end time is fixed once
    for item in checks_mod.fold(session, record):
        if item["state"] == "open":
            checks_mod.end(session, record, item["check_id"], verdict="inconclusive",
                           note=OPEN_AT_PACK)
            closed.append(item["check_id"])
    record.update(store.load(session, pretest_id))
    # the end time of this attempt, kept in memory until the pack is sealed;
    # a stopped pre-test keeps the time it was stopped
    if record.get("status") == "stopped" and record.get("ended_at"):
        ended_at = record["ended_at"]
    else:
        ended_at = store.now()
    record["ended_at"] = ended_at
    # 2. health and automatic findings (idempotent per data.key)
    health = health_mod.run(session, record, target)["health"]
    record.update(store.load(session, pretest_id))
    record["ended_at"] = ended_at
    checks = checks_mod.fold(session, record)
    findings = findings_mod.fold(session, record)
    known = {item["check_id"] for item in checks}
    verdict = verdict_mod.ticket_verdict(checks, findings)
    kit_cov = kit_mod.coverage(checks)
    actions = store.read_budget(session, pretest_id)["actions_used"]
    timing = summary_mod.timing_block(record, checks, findings, actions)
    check_repro: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        try:
            check_repro[check["check_id"]] = repro_mod.steps(session, check)
        except errors.AutonomError:
            check_repro[check["check_id"]] = []
    finding_repro: dict[str, list[dict[str, Any]]] = {}
    for item in findings:
        if not item.get("check_id"):
            continue
        steps = list(check_repro.get(item.get("check_id")) or [])
        try:
            finding_repro[item["finding_id"]] = repro_mod.cut_at_evidence(session, item, steps)
        except (errors.AutonomError, OSError, ValueError):
            finding_repro[item["finding_id"]] = steps
    requests = check_requests(session, checks)
    parent = _parent_summary(record)
    sealed_digest = record_digest(session, record)
    try:
        context = store.read_context(session, pretest_id)
    except (OSError, ValueError):
        context = {}

    scratch = Path(tempfile.mkdtemp(prefix=".pack-runs-", dir=directory))
    try:
        with BundleWriter(out) as writer:
            evidence = _Evidence(writer, known)
            _collect_session_evidence(session, record, checks, health, evidence)
            nested, unavailable = _nest_runs(session, writer, scratch,
                                             _run_ids(checks, findings), evidence)
            finding_evidence: dict[str, list[str]] = {}
            for finding in findings:
                ids: list[str] = []
                for ref in finding.get("evidence") or []:
                    eid = _ref_evidence(session, evidence, ref, finding.get("check_id"),
                                        nested)
                    if eid and eid not in ids:
                        ids.append(eid)
                finding_evidence[finding["finding_id"]] = ids
            check_evidence = {check["check_id"]: [item["id"] for item in evidence.items
                                                  if item.get("check_id") == check["check_id"]]
                              for check in checks}
            before, after = _before_after(record, checks, parent, check_evidence)
            layout = {"evidence": evidence.items, "check_evidence": check_evidence,
                      "finding_evidence": finding_evidence, "check_repro": check_repro,
                      "finding_repro": finding_repro, "before": before, "after": after,
                      "context": context, "actions": actions,
                      "check_requests": requests,
                      "parent_checks": (parent or {}).get("checks") or []}
            summary = summary_mod.build_summary(record, checks, findings, health, kit_cov,
                                                verdict, timing, layout)

            inner = dict(record)
            inner["status"] = "packed"
            inner["pack"] = {"path": str(out), "integrity_sha256": ""}
            store.validate_record(inner)
            writer.write_json(store.RECORD_FILE, inner)
            for name in RECORD_FILES:
                source = directory / name
                if _regular(source):
                    writer.copy_file(source, name)
            for check in summary["checks"]:
                writer.write_json(f"checks/{check['check_id']}.json", check)
            for finding in summary["findings"]:
                writer.write_json(f"findings/{finding['finding_id']}.json", finding)
            writer.write_json("summary.json", summary)
            roots = [session["artifacts_dir"], str(directory), str(out),
                     record.get("repo"), record.get("flows_dir"), record.get("parent_pack")]
            writer.write_text("summary.md", summary_mod.render_markdown(
                summary, roots=[root for root in roots if root]))
            writer.write_text("index.html", view_mod.render(summary))
            manifest = {
                "schema": PACK_KIND, "kind": PACK_KIND,
                "pretest_id": pretest_id, "session_id": record.get("session_id"),
                "title": record.get("title"), "ticket_ref": record.get("ticket_ref"),
                "verdict": verdict["verdict"], "packed_at": store.now(),
                "parent_pack": record.get("parent_pack"), "pack_kind": record.get("kind"),
                "files": {"record": store.RECORD_FILE, "summary": "summary.json",
                          "summary_md": "summary.md", "index": "index.html",
                          "integrity": bundle_writer_mod.INTEGRITY_NAME,
                          "finalized": bundle_writer_mod.FINALIZED_NAME},
                "checks": [item["check_id"] for item in checks],
                "findings": [item["finding_id"] for item in findings],
                "runs": sorted(nested),
                "runs_unavailable": unavailable,
                "evidence": len(evidence.items),
                "record_digest": sealed_digest,
            }
            validate_manifest(manifest)
            writer.write_json(bundle_writer_mod.DESCRIPTOR_NAME, manifest)
            sealed = writer.finalize(None, finalized_fields={"kind": PACK_KIND,
                                                             "pretest_id": pretest_id})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    # finalized: from here on the pack holds those check ends, so a failure
    # below never reopens them (a retry adopts the pack)
    closed.clear()
    _seal(session, record, out, sealed["integrity_sha256"], ended_at)
    return _result(record, out, sealed["integrity_sha256"], verdict["verdict"], False,
                   files=sealed["files"], warnings=evidence.warnings + [
                       {"evidence": f"run:{item['run_id']}", "reason": item["reason"]}
                       for item in unavailable])


# --- manifest validation -------------------------------------------------------------------


def manifest_failures(manifest: Any) -> list[str]:
    """Hand-written validator for ``schemas/pretest-pack-v1.schema.json``."""
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    failures: list[str] = []
    for key in ("schema", "kind", "pretest_id", "session_id", "title", "verdict",
                "packed_at", "files", "checks", "findings", "runs", "runs_unavailable",
                "evidence", "record_digest"):
        if key not in manifest:
            failures.append(f"missing {key}")
    for key in ("schema", "kind"):
        if manifest.get(key) != PACK_KIND:
            failures.append(f"{key} must be {PACK_KIND}")
    if not isinstance(manifest.get("pretest_id"), str) or not store.PRETEST_ID.match(
            manifest.get("pretest_id") or ""):
        failures.append("pretest_id must look like pt_YYYYMMDDTHHMMSS_xxxxxx")
    if manifest.get("verdict") not in summary_mod.VERDICTS:
        failures.append("verdict must be a ticket verdict")
    files = manifest.get("files")
    if not isinstance(files, dict) or not all(isinstance(value, str)
                                              for value in files.values()):
        failures.append("files must map names to pack-relative paths")
    for key in ("checks", "findings", "runs"):
        value = manifest.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            failures.append(f"{key} must be a list of ids")
    if not isinstance(manifest.get("runs_unavailable"), list):
        failures.append("runs_unavailable must be a list")
    if not isinstance(manifest.get("evidence"), int) or isinstance(manifest.get("evidence"),
                                                                  bool):
        failures.append("evidence must be a count")
    digest = manifest.get("record_digest")
    if "record_digest" in manifest and (
            not isinstance(digest, dict)
            or set(digest) != {"sha256", *(name for name, _ in _DIGEST_PARTS)}
            or not all(isinstance(value, str) and _SHA256.match(value)
                       for value in digest.values())):
        failures.append("record_digest must map sha256, record, checks, findings and "
                        "health to SHA-256 hex digests")
    return failures


def validate_manifest(manifest: Any) -> dict[str, Any]:
    failures = manifest_failures(manifest)
    if failures:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"pack manifest does not satisfy {PACK_KIND}",
            hint="This is a bug in the pack writer.", failures=failures)
    return manifest


# --- verify ------------------------------------------------------------------------------------


def _not_a_pack(path: Path) -> errors.AutonomError:
    return errors.AutonomError(
        errors.NOT_A_PRETEST_PACK, f"{path} is not a pre-test pack",
        hint="Pass the directory 'autonom pretest pack' printed (it holds a "
             f"manifest.json of kind {PACK_KIND}).", path=str(path))


def _os_metadata(name: str) -> bool:
    return name in _OS_METADATA or name.startswith("._")


def _plain_file(path: Path) -> bool:
    """A regular file, not followed through a symlink (a FIFO, socket or
    device node is not one)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def entry_kind(path: Path) -> str | None:
    """``dir``, ``file`` (regular), ``symlink`` or ``special`` (FIFO, socket,
    device node) by ``lstat``; ``None`` when it vanished or cannot be read."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def catalogue_path(raw: Any) -> tuple[str | None, str | None]:
    """``(normalized, None)`` for a usable catalogue path, else
    ``(None, reason)`` (contract section 15.3).

    The path is normalized as a POSIX path before it is compared with
    entries on disk, the same way ``bundle_writer.verify_tree`` joins it, so
    ``./x`` and ``a//b`` name ``x`` and ``a/b``. An absolute path or one with a
    ``..`` segment is ``path-forbidden``; a path that is not a string, holds a
    NUL or names nothing (``""``, ``.``) is ``malformed-entry``."""
    if not isinstance(raw, str) or not raw or "\0" in raw:
        return None, "malformed-entry"
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        return None, "path-forbidden"
    if not path.parts:
        return None, "malformed-entry"
    return path.as_posix(), None


def _listed_dirs(listed: set[Any]) -> set[str]:
    """Every directory that holds a listed file (its parents, at any depth)."""
    out: set[str] = set()
    for path in listed:
        parts = str(path).split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            out.add("/".join(parts[:depth]))
    return out


def _unlisted(root: Path, listed: set[str]) -> tuple[list[dict[str, Any]], set[str]]:
    """Entries on disk that the integrity list does not cover (see ``verify``),
    and the set of listed paths that exist on disk as regular files under
    exactly that name.

    Names are compared exactly, byte for byte, on every volume type
    (contract section 17): an entry whose name differs from a listed path
    only by letter case is ``unlisted``, and the listed path is missing.

    Each entry is classified by ``lstat`` and never opened: a symlink is
    ``symlink``; a FIFO, socket or device node is ``not-a-regular-file`` when
    the catalogue lists its path and ``unlisted`` otherwise; a directory that
    holds no listed file (empty or not, whatever its name) is ``unlisted`` and
    is not entered; an unlisted regular file is ``unlisted`` unless it is an
    OS metadata file. Listed paths under a directory that cannot be listed
    count as present (that directory is already reported ``unreadable``)."""
    out: list[dict[str, Any]] = []
    present: set[str] = set()
    unreadable_dirs: list[str] = []
    annotations = root / bundle_writer_mod.ANNOTATIONS_DIR
    if entry_kind(annotations) not in (None, "dir"):
        out.append({"path": bundle_writer_mod.ANNOTATIONS_DIR, "reason": "not-a-directory"})
    reserved = {bundle_writer_mod.INTEGRITY_NAME, bundle_writer_mod.FINALIZED_NAME,
                bundle_writer_mod.ANNOTATIONS_DIR}
    directories = _listed_dirs(listed)

    def unreadable(exc: OSError) -> None:
        # a directory that cannot be listed cannot be checked: report it
        where = Path(exc.filename) if exc.filename else root
        try:
            relative = where.relative_to(root).as_posix()
        except ValueError:
            relative = str(where)
        relative = relative if relative != "." else ""
        unreadable_dirs.append(relative)
        out.append({"path": relative, "reason": "unreadable"})

    for current, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
        base = Path(current)
        relative_dir = base.relative_to(root)
        kept = []
        # os.walk files a symlink to a directory under dirs: classify every
        # name by lstat, whichever list it came in
        for name in sorted(dirs + names):
            relative = (relative_dir / name).as_posix()
            if relative in reserved:
                continue
            kind = entry_kind(base / name)
            if kind is None:
                out.append({"path": relative, "reason": "unreadable"})
            elif kind == "symlink":
                out.append({"path": relative, "reason": "symlink"})
            elif kind == "dir":
                if relative in directories:
                    kept.append(name)
                else:
                    out.append({"path": relative, "reason": "unlisted"})
            elif kind == "special":
                out.append({"path": relative, "reason": "not-a-regular-file"
                            if relative in listed else "unlisted"})
            elif relative in listed:
                present.add(relative)
            elif not _os_metadata(name):
                out.append({"path": relative, "reason": "unlisted"})
        dirs[:] = kept
    for path in listed:
        if any(not folder or path.startswith(folder + "/") for folder in unreadable_dirs):
            present.add(path)
    return out, present


def _unsafe_entries(base: Path) -> list[dict[str, Any]]:
    """Every entry under ``base`` that is neither a directory nor a regular
    file (by ``lstat``, never opened), or that cannot be listed: reading such
    a tree could follow a link out of the pack or block on a FIFO.

    The walk never follows a symlink (``os.walk(followlinks=False)`` and an
    ``lstat`` of every name, whichever list ``os.walk`` put it in) and opens
    no file; it is the up-front scan of contract section 16 when ``base`` is
    the pack root, and covers ``annotations/`` and nested bundles too."""
    out: list[dict[str, Any]] = []

    def unreadable(exc: OSError) -> None:
        where = Path(exc.filename) if exc.filename else base
        try:
            relative = where.relative_to(base).as_posix()
        except ValueError:
            relative = str(where)
        out.append({"path": relative if relative != "." else "", "reason": "unreadable"})

    for current, dirs, names in os.walk(base, followlinks=False, onerror=unreadable):
        here = Path(current)
        kept = []
        for name in sorted(dirs + names):
            relative = (here / name).relative_to(base).as_posix()
            kind = entry_kind(here / name)
            if kind == "dir":
                kept.append(name)
            elif kind == "symlink":
                out.append({"path": relative, "reason": "symlink"})
            elif kind == "special":
                out.append({"path": relative, "reason": "not-a-regular-file"})
            elif kind is None:
                out.append({"path": relative, "reason": "unreadable"})
        dirs[:] = kept
    return out


def _integrity_refusal(root: Path, failures: list[dict[str, Any]],
                       nested: list[dict[str, Any]]) -> errors.AutonomError:
    return errors.AutonomError(
        errors.REPORT_INTEGRITY_FAILED, "pre-test pack integrity verification failed",
        hint="A file changed after the pack was finalized (see failures). "
             + DAMAGED_HINT, pack=str(root), failures=failures, nested=nested)


def _scan_refusal(root: Path) -> errors.AutonomError | None:
    """The up-front scan of contract section 16: ``None`` when every entry
    in the pack (``annotations/`` and nested bundles included) is a real
    directory or a regular file, else the ``report_integrity_failed`` error
    naming every symlink, FIFO, socket, device node or unreadable entry.

    Only ``lstat`` and directory listings are used; no file is opened or
    hashed, and nothing else is read when the scan fails (the failures end
    with ``integrity.json: digests-not-checked``, and every run directory or
    run symlink in a real ``runs/`` is ``ok: false``, because nothing was
    checked)."""
    found = _unsafe_entries(root)
    if not found:
        return None
    # manifest.json first, so the reason a pack is damaged is the first line
    found.sort(key=lambda item: item["path"] != bundle_writer_mod.DESCRIPTOR_NAME)
    found.append({"path": bundle_writer_mod.INTEGRITY_NAME, "reason": "digests-not-checked"})
    nested: list[dict[str, Any]] = []
    runs_dir = root / "runs"
    if entry_kind(runs_dir) == "dir":
        try:
            names = sorted(child.name for child in runs_dir.iterdir()
                           if _RUN_ID.match(child.name)
                           and entry_kind(child) in ("dir", "symlink"))
        except OSError:
            names = []
        nested = [{"run_id": name, "ok": False} for name in names]
    return _integrity_refusal(root, found, nested)


def verify(pack: str | Path) -> dict[str, Any]:
    """``{"ok": true, "pack", "files", "nested": [{"run_id", "ok"}]}``, or
    ``report_integrity_failed`` naming every changed, missing or unlisted file
    (nested bundle failures are prefixed with ``runs/<run_id>/``).

    One up-front scan comes first (contract section 16): once
    ``manifest.json`` is known to exist (by ``lstat``), the whole pack tree,
    ``annotations/`` and nested bundles included, is walked with ``lstat``
    only, before any file is opened or hashed (``manifest.json`` too). When
    any entry anywhere is a symlink, FIFO, socket, device node or other
    non-regular non-directory, or cannot be listed, verify raises
    ``report_integrity_failed`` naming those paths (reasons ``symlink``,
    ``not-a-regular-file``, ``unreadable``, plus ``integrity.json:
    digests-not-checked``) and reads nothing else. So a directory with
    ``manifest.json`` and a symlink is a damaged pack even if its manifest
    is not a pre-test manifest. The per-path checks below still run after a
    clean scan; they are no longer the guarantee.

    Unlisted means present in the pack but absent from ``integrity.json``
    (reason ``unlisted``); a directory is unlisted when it holds no listed
    file, whether it is empty or not and whatever its name (also one named
    like OS metadata). A symlink anywhere in the pack is reported with reason
    ``symlink`` because a pack holds regular files and directories only, and
    that includes ``integrity.json`` and ``finalized.json``, which must be
    regular files. A listed path that is a FIFO, socket or device node is
    ``not-a-regular-file``. The only things outside the integrity list are
    ``integrity.json``, ``finalized.json`` and the top-level ``annotations/``
    directory, which must be a real directory (reason ``not-a-directory``
    otherwise).

    OS metadata files are ignored anywhere in the pack: ``.DS_Store``,
    AppleDouble ``._*`` files, ``Thumbs.db`` and ``desktop.ini`` (exact
    names, regular files only; a directory, symlink,
    FIFO, socket or device node of that name is still reported). Every other
    unlisted file or directory outside ``annotations/**`` is reported.

    Every file verify reads (``manifest.json``, ``integrity.json``,
    ``finalized.json``, the listed files, nested bundle files) is opened only
    after ``lstat`` shows a regular file (contract section 14.3). When a
    listed path, ``integrity.json`` or ``finalized.json`` is not one, the
    digests are not read at all (reason ``digests-not-checked``); a nested
    bundle holding anything but directories and regular files is reported
    entry by entry and not read.

    Catalogue paths are normalized as POSIX paths before they are compared
    with entries on disk (``./x`` is ``x``, ``a//b`` is ``a/b``); an absolute
    path or one with ``..`` is ``path-forbidden``, an empty one
    ``malformed-entry``, and either stops the digests from being read
    (contract section 15.3). Normalized catalogue paths are then compared
    with the names on disk exactly, on every volume type (contract section
    17): an entry on disk that does not exactly match a listed path is
    ``unlisted`` and a listed path without an exactly named regular file on
    disk is ``missing``, even where the volume would open it under another
    letter case.

    Nested bundles are read only through real directories: ``runs/`` and
    ``runs/<run_id>/`` must ``lstat`` as directories. When ``runs/`` is a
    symlink or not a directory it is reported and every declared run is
    ``not-checked`` (``ok: false``) without being read; a run directory that
    is a symlink or not a directory is reported the same way (contract
    section 15.2). Directories inside a nested bundle are walked by ``lstat``
    before ``report_bundle.verify`` reads anything.

    ``not_a_pretest_pack`` means ``manifest.json`` is absent or is a regular
    file that is not a pre-test manifest. A ``manifest.json`` that exists but
    is not a regular file (symlink, FIFO, socket, device node, directory) is
    ``report_integrity_failed`` naming it (contract section 15.1).

    Malformed data never escapes as a Python error: unreadable or non-object
    JSON in the pack or in a nested bundle, a catalogue that is not a list,
    and any OS error while reading become ``report_integrity_failed`` with a
    hint."""
    try:
        root = Path(pack).expanduser()
    except (RuntimeError, TypeError, ValueError):
        # "~user" without such a user, or not a path at all
        raise _not_a_pack(Path(str(pack))) from None
    try:
        is_dir = root.is_dir()
    except (OSError, ValueError):
        is_dir = False
    if not is_dir:
        raise _not_a_pack(root)
    manifest_kind = entry_kind(root / bundle_writer_mod.DESCRIPTOR_NAME)
    if manifest_kind is None:
        raise _not_a_pack(root)
    # contract 16: one lstat-only scan of the whole tree before anything is read
    try:
        refusal = _scan_refusal(root)
    except Exception as exc:  # noqa: BLE001 - a scan that cannot finish proves nothing
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED, "pre-test pack could not be verified",
            hint="The pack is damaged or unreadable (see failures). " + DAMAGED_HINT,
            pack=str(root), failures=[{"path": "", "reason": type(exc).__name__}],
            nested=[]) from None
    if refusal is not None:
        raise refusal
    first: list[dict[str, Any]] = []
    if manifest_kind == "file":
        if not is_pack(root):
            raise _not_a_pack(root)
    else:
        # present but not a regular file: a damaged pack, never opened
        first.append({"path": bundle_writer_mod.DESCRIPTOR_NAME,
                      "reason": "symlink" if manifest_kind == "symlink"
                      else "not-a-regular-file"})
    try:
        failures, nested, files = _verify_pack(root)
        failures = first + [item for item in failures if item not in first]
    except errors.AutonomError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed packs are refusals, not crashes
        raise errors.AutonomError(
            errors.REPORT_INTEGRITY_FAILED, "pre-test pack could not be verified",
            hint="The pack is damaged or unreadable (see failures). " + DAMAGED_HINT,
            pack=str(root), failures=[{"path": "", "reason": type(exc).__name__}],
            nested=[]) from None
    if failures:
        raise _integrity_refusal(root, failures, nested)
    return {"ok": True, "pack": str(root), "kind": PACK_KIND, "files": files,
            "nested": nested}


def _verify_nested(bundle: Path) -> list[dict[str, Any]]:
    """Failures of one nested Report Bundle v2 (empty when it verifies). A
    bundle holding a symlink, FIFO, socket or device node is reported entry by
    entry and never read (``report_bundle.verify`` opens files by name)."""
    unsafe = _unsafe_entries(bundle)
    if unsafe:
        return unsafe
    try:
        report_bundle.verify(bundle)
    except errors.AutonomError as exc:
        inner = exc.extra.get("failures")
        if not isinstance(inner, list) or not inner:
            inner = [{"path": "", "reason": exc.message}]
        return [{"path": str(item.get("path") or "") if isinstance(item, dict) else "",
                 "reason": str((item.get("reason") if isinstance(item, dict) else None)
                               or "invalid")} for item in inner]
    except Exception as exc:  # noqa: BLE001 - non-object JSON, unreadable files
        # report_bundle.verify reads the nested manifest, finalization record
        # and model without type checks: a list where an object belongs is an
        # AttributeError, broken bytes a ValueError; all are failures here.
        return [{"path": "", "reason": type(exc).__name__}]
    return []


def _verify_pack(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    failures: list[dict[str, Any]] = []
    unsafe: set[str] = set()
    for name in (bundle_writer_mod.INTEGRITY_NAME, bundle_writer_mod.FINALIZED_NAME):
        kind = entry_kind(root / name)
        if kind == "symlink":
            failures.append({"path": name, "reason": "symlink"})
        elif kind not in (None, "file"):
            failures.append({"path": name, "reason": "not-a-regular-file"})
        if kind not in (None, "file"):
            unsafe.add(name)
    integrity = _read_json(root / bundle_writer_mod.INTEGRITY_NAME)
    catalogue = integrity.get("files") if isinstance(integrity, dict) else None
    catalogue = catalogue if isinstance(catalogue, list) else []
    listed: set[str] = set()
    for item in catalogue:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue  # verify_tree reports it as malformed-entry
        normalized, reason = catalogue_path(item["path"])
        if normalized is None:
            # never handed to verify_tree, which would open it by name
            failures.append({"path": item["path"], "reason": reason})
            unsafe.add(item["path"])
        else:
            listed.add(normalized)
    for name in CORE_FILES:
        if name not in listed:
            failures.append({"path": name, "reason": "not-listed"})
    if isinstance(integrity, dict):
        found, present = _unlisted(root, listed)
        failures.extend(found)
        reach = listed | _listed_dirs(listed)
        unsafe |= {item["path"] for item in found
                   if item["reason"] in ("symlink", "not-a-regular-file")
                   and item["path"] in reach}
        # contract 17: a listed path needs an entry of exactly that name, also
        # where the volume would open it under another letter case
        reported = {item["path"] for item in found}
        failures.extend({"path": path, "reason": "missing"}
                        for path in sorted(listed - present - reported))
    if unsafe:
        # reading the digests would follow a link or block on a FIFO
        failures.append({"path": bundle_writer_mod.INTEGRITY_NAME,
                         "reason": "digests-not-checked"})
    else:
        try:
            failures.extend(bundle_writer_mod.verify_tree(root))
        except errors.AutonomError as exc:
            failures.append({"path": str(exc.extra.get("path") or "integrity.json"),
                             "reason": "path-forbidden"})
        except OSError as exc:
            failures.append({"path": bundle_writer_mod.INTEGRITY_NAME,
                             "reason": f"unreadable ({type(exc).__name__})"})
    manifest = _read_json(root / bundle_writer_mod.DESCRIPTOR_NAME)
    declared = manifest.get("runs") if isinstance(manifest, dict) else None
    runs = {run_id for run_id in (declared if isinstance(declared, list) else [])
            if isinstance(run_id, str)}
    runs_dir = root / "runs"
    # every directory between the pack root and a nested bundle must lstat as
    # a real directory before anything below it is read (contract 15.2)
    runs_kind = entry_kind(runs_dir)
    if runs_kind == "dir":
        try:
            runs |= {child.name for child in runs_dir.iterdir()
                     if entry_kind(child) == "dir"}
        except OSError:
            failures.append({"path": "runs/", "reason": "unreadable"})
    elif runs_kind is not None:
        failures.append({"path": "runs", "reason": "symlink" if runs_kind == "symlink"
                         else "not-a-directory"})
    nested: list[dict[str, Any]] = []
    for run_id in sorted(runs):
        bundle = runs_dir / run_id
        if not _RUN_ID.match(run_id) or runs_kind is None:
            reason = "missing"
        elif runs_kind != "dir":
            reason = "not-checked"
        else:
            kind = entry_kind(bundle)
            reason = (None if kind == "dir" else "missing" if kind is None
                      else "symlink" if kind == "symlink" else "not-a-directory")
        if reason is not None:
            nested.append({"run_id": run_id, "ok": False})
            failures.append({"path": f"runs/{run_id}/", "reason": reason})
            continue
        inner = _verify_nested(bundle)
        nested.append({"run_id": run_id, "ok": not inner})
        for item in inner:
            failures.append({"path": f"runs/{run_id}/{item['path']}",
                             "reason": item["reason"]})
    unique: list[dict[str, Any]] = []
    for item in failures:
        if item not in unique:
            unique.append(item)
    return unique, nested, len(catalogue)


def default_pack_dir(session: dict[str, Any], pretest_id: str) -> Path:
    return store.pretest_dir(session, pretest_id) / PACK_DIR

