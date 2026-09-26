"""Teach recorder state, marker ranges, review, validation, and approval."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import errors, journal
from .contracts import canonical_json, fresh_id, utc_now
from .flow import compiler, validator

STATE_FILE = "teach.json"


def _path(session: dict[str, Any]) -> Path:
    return Path(session["artifacts_dir"]) / STATE_FILE


def _write(session: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    path = _path(session)
    path.write_bytes(canonical_json(state) + b"\n")
    os.chmod(path, 0o600)
    return state


def load(session: dict[str, Any]) -> dict[str, Any]:
    path = _path(session)
    if not path.is_file():
        return {"schema": "autonom.teach/v1", "recordings": []}
    return json.loads(path.read_text(encoding="utf-8"))


def start(session: dict[str, Any], name: str) -> dict[str, Any]:
    state = load(session)
    if any(item.get("status") == "recording" for item in state["recordings"]):
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "a Teach recording is already active")
    before, _ = journal.read(session, max_entries=100_000)
    recording = {
        "recording_id": fresh_id("teach"), "name": name,
        "status": "recording", "started_at": utc_now(),
        "start_seq": (before[-1]["seq"] + 1) if before else 1,
        "markers": [],
    }
    state["recordings"].append(recording)
    journal.append(session, {"kind": "teach", "event": "start",
                             "recording_id": recording["recording_id"],
                             "name": name, "origin": "human"})
    # The marker itself is metadata, not part of the compilable range.
    recording["start_seq"] += 1
    _write(session, state)
    return recording


def _active(state: dict[str, Any]) -> dict[str, Any]:
    item = next((item for item in reversed(state.get("recordings") or [])
                 if item.get("status") == "recording"), None)
    if not item:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "there is no active Teach recording",
                                  hint="Start one with 'autonom teach start <name>'.")
    return item


def mark(session: dict[str, Any], name: str) -> dict[str, Any]:
    state = load(session)
    recording = _active(state)
    journal.append(session, {"kind": "teach", "event": "marker",
                             "recording_id": recording["recording_id"],
                             "name": name, "origin": "human"})
    entries, _ = journal.read(session, max_entries=100_000)
    marker = {"name": name, "seq": entries[-1]["seq"], "at": utc_now()}
    recording["markers"].append(marker)
    _write(session, state)
    return marker


def stop(session: dict[str, Any]) -> dict[str, Any]:
    state = load(session)
    recording = _active(state)
    entries, _ = journal.read(session, max_entries=100_000)
    recording["end_seq"] = entries[-1]["seq"] if entries else recording["start_seq"]
    recording["stopped_at"] = utc_now()
    recording["status"] = "recorded"
    journal.append(session, {"kind": "teach", "event": "stop",
                             "recording_id": recording["recording_id"],
                             "origin": "human"})
    _write(session, state)
    return recording


def resolve_range(session: dict[str, Any], recording_id: str | None = None,
                  from_marker: str | None = None,
                  to_marker: str | None = None) -> tuple[dict[str, Any], int, int]:
    state = load(session)
    candidates = [item for item in state.get("recordings") or []
                  if item.get("status") != "recording"]
    if recording_id:
        candidates = [item for item in candidates
                      if item.get("recording_id") == recording_id]
    if not candidates:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "no completed Teach recording matches")
    recording = candidates[-1]
    markers = {item["name"]: item["seq"] for item in recording.get("markers") or []}
    start_seq = markers.get(from_marker, recording["start_seq"])
    end_seq = markers.get(to_marker, recording["end_seq"])
    if from_marker and from_marker not in markers:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  f"unknown start marker {from_marker!r}")
    if to_marker and to_marker not in markers:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  f"unknown end marker {to_marker!r}")
    if start_seq > end_seq:
        raise errors.AutonomError(errors.TEACH_RANGE_INVALID,
                                  "Teach range starts after it ends")
    return recording, start_seq, end_seq


def compile_recording(session: dict[str, Any], *, out: Path,
                      recording_id: str | None = None,
                      from_marker: str | None = None,
                      to_marker: str | None = None) -> dict[str, Any]:
    recording, start_seq, end_seq = resolve_range(
        session, recording_id, from_marker, to_marker)
    text, quality = compiler.compile_to_text(
        session, name=recording["name"], task=recording["name"],
        start_seq=start_seq, end_seq=end_seq)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    validator.validate_tree(out)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    state = load(session)
    target = next(item for item in state["recordings"]
                  if item["recording_id"] == recording["recording_id"])
    target.update({"status": "compiled", "flow": str(out),
                   "flow_sha256": digest, "quality": quality})
    _write(session, state)
    return {"recording_id": recording["recording_id"], "out": str(out),
            "flow_sha256": digest, **quality}


def file_sha256(path: Path) -> str:
    """Hash of the exact bytes on disk — what an approval is bound to."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hash_or_none(path: Path) -> str | None:
    try:
        return file_sha256(path)
    except OSError:
        return None


def subflow_hashes(flow_path: Path) -> dict[str, str | None]:
    """Current hash of every runFlow child of ``flow_path``, keyed by its
    resolved path — the shape a run manifest's ``subflow_sha256`` has."""
    return {str(path): _hash_or_none(path)
            for path in validator.subflow_files(flow_path)}


def changed_subflows(recorded: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Children in a recorded ``subflow_sha256`` map whose bytes differ now
    (a deleted child counts as changed)."""
    changed = []
    for path, digest in sorted((recorded or {}).items()):
        now = _hash_or_none(Path(path))
        if now != digest:
            changed.append({"path": path, "recorded_sha256": digest,
                            "current_sha256": now})
    return changed


def verify_subflows(receipt: dict[str, Any], *, flow: str | None = None) -> None:
    """Refuse (``FLOW_SOURCE_CHANGED``) when a runFlow child an approval
    receipt covers has changed since. A receipt without the map (it predates
    child binding) binds the root file only and passes."""
    changed = changed_subflows(receipt.get("subflow_sha256"))
    if changed:
        raise errors.AutonomError(
            errors.FLOW_SOURCE_CHANGED,
            f"{len(changed)} runFlow subflow(s) changed after the flow was approved",
            hint="Replay and re-approve the flow with 'autonom teach approve "
                 "<flow> --run'; an approval covers every file the flow runs.",
            flow=flow or receipt.get("flow"), changed_subflows=changed)


def _ledger(session: dict[str, Any]) -> dict[str, Any]:
    return load(session).get("replays") or {}


def record_replay(session: dict[str, Any], run_id: str, flow_path: Path,
                  flow_sha256: str | None) -> None:
    """Bind a replay's run id to the source hash it actually executed.

    Run manifests carry ``flow_sha256`` themselves now and always win; the
    ledger in ``teach.json`` only still decides for a legacy manifest that
    was written without the key.
    """
    state = load(session)
    replays = state.setdefault("replays", {})
    replays[run_id] = {"flow": str(flow_path), "flow_sha256": flow_sha256,
                       "recorded_at": utc_now()}
    _write(session, state)


def replay(session: dict[str, Any], target: Any, flow_path: Path, *,
           runs: int, env: dict[str, str] | None = None,
           secrets: dict[str, str] | None = None,
           runner_factory: Any = None) -> list[dict[str, Any]]:
    """Run the flow ``runs`` times for approval, recording each source hash.

    Stops at the first failed replay (an approval is a claim about
    consecutive passes) with ``TEACH_APPROVAL_BLOCKED``. ``env``/``secrets``
    reach the executor exactly as in ``flow run --env/--secret``.
    ``runner_factory(target, session, config)`` defaults to the flow
    executor; tests pass a fake.
    """
    _require_minimum_runs(runs)
    from .flow import executor as flow_executor  # heavy; only when replaying
    factory = runner_factory or flow_executor.Executor
    config = flow_executor.RunConfig(env=dict(env or {}),
                                     secrets=dict(secrets or {}))
    replays: list[dict[str, Any]] = []
    for _ in range(runs):
        before = file_sha256(flow_path)
        flow = validator.validate_tree(flow_path)
        result = factory(target, session, config).run(flow)
        # an edit during the run leaves the replay unbound, never counted
        digest = before if file_sha256(flow_path) == before else None
        record_replay(session, result.run_id, flow_path, digest)
        replays.append({"run_id": result.run_id, "status": result.status,
                        "flow_sha256": digest})
        if result.status not in ("passed", "replayed"):
            raise errors.AutonomError(
                errors.TEACH_APPROVAL_BLOCKED,
                f"replay {len(replays)} of {runs} failed; approval "
                "needs consecutive clean passes",
                hint="Read the failure in the run's events, fix the flow, and "
                     "re-run 'teach approve --run'.",
                flow_id=flow.flow_id, replays=replays,
                failure=getattr(result, "failure", None),
            )
    return replays


def _require_minimum_runs(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise errors.AutonomError(
            errors.USAGE_ERROR,
            f"--minimum-runs must be an integer >= 1, got {value!r}",
            hint="An approval with zero replays proves nothing.",
            minimum_runs=value,
        )
    return value


def _run_time(manifest: dict[str, Any], manifest_path: Path) -> float:
    started = manifest.get("started_at_ms")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return started / 1000
    return manifest_path.stat().st_mtime


def approve(session: dict[str, Any], flow_path: Path, *, minimum_runs: int = 3) -> dict[str, Any]:
    """Approve a flow after ``minimum_runs`` consecutive clean replays of
    **these bytes**.

    A replay counts only when its source is bound to the current file:

    - a manifest with ``flow_sha256`` (every run since the executor records
      it) counts when that hash equals the file's hash now and every child in
      its ``subflow_sha256`` is unchanged — it never falls back to mtime;
    - a legacy manifest without the key counts by the Teach ledger's hash,
      else only when the file has not been modified since that run started.
      Those mtime-bound replays are listed in the receipt as
      ``legacy_unhashed`` with a warning: mtimes can be preserved by a copy.

    An edit after the replays always resets the count; a changed runFlow
    child refuses with ``FLOW_SOURCE_CHANGED``. The receipt stores
    ``flow_sha256`` and ``subflow_sha256`` so ``app-skill promote`` can
    refuse an edited flow later.
    """
    _require_minimum_runs(minimum_runs)
    flow = validator.validate_tree(flow_path)
    current = file_sha256(flow_path)
    modified_at = flow_path.stat().st_mtime
    ledger = _ledger(session)
    manifests = []
    for path in sorted((Path(session["artifacts_dir"]) / "flows").glob("*/manifest.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("flow_id") == flow.flow_id:
            manifests.append((value, path))
    consecutive = []
    bindings = {"sha256": 0, "unmodified_since_run": 0}
    legacy_unhashed: list[str] = []
    changed: list[dict[str, Any]] = []
    stale = 0
    for manifest, path in manifests:
        if manifest.get("status") != "passed":
            break
        if "flow_sha256" in manifest:
            # a hashed manifest decides alone; no hash (the file could not be
            # read for the run) leaves the replay unbound, never mtime-bound
            recorded = manifest.get("flow_sha256")
            if not recorded or recorded != current:
                stale += 1
                break
            changed = changed_subflows(manifest.get("subflow_sha256"))
            if changed:
                stale += 1
                break
            bindings["sha256"] += 1
        else:
            recorded = (ledger.get(manifest.get("run_id")) or {}).get("flow_sha256")
            if recorded:
                if recorded != current:
                    stale += 1
                    break
                bindings["sha256"] += 1
            elif (manifest.get("run_id") in ledger
                  or modified_at > _run_time(manifest, path)):
                # unbound ledger entry (edited mid-run) or edited after the run
                stale += 1
                break
            else:
                bindings["unmodified_since_run"] += 1
                legacy_unhashed.append(manifest.get("run_id"))
        consecutive.append(manifest)
        if len(consecutive) >= minimum_runs:
            break
    if len(consecutive) < minimum_runs:
        if changed:
            raise errors.AutonomError(
                errors.FLOW_SOURCE_CHANGED,
                f"{len(changed)} runFlow subflow(s) changed after the last "
                "replays; replays of other content do not count",
                hint=(f"Run the flow until it has {minimum_runs} consecutive "
                      "passes of the current files, then approve again."),
                flow_id=flow.flow_id, clean_replays=len(consecutive),
                required=minimum_runs, changed_subflows=changed,
                flow_sha256=current,
            )
        raise errors.AutonomError(
            errors.TEACH_APPROVAL_BLOCKED,
            f"Teach approval requires {minimum_runs} consecutive clean replays "
            f"of the current flow file; {len(consecutive)} match",
            hint=(f"Run the compiled flow until it has {minimum_runs} consecutive "
                  "passes." + (" The flow file changed after its last replays; "
                               "replays of other content do not count."
                               if stale else "")),
            flow_id=flow.flow_id, clean_replays=len(consecutive),
            required=minimum_runs, stale_replays=stale, flow_sha256=current,
        )
    warnings = []
    if legacy_unhashed:
        warnings.append({
            "code": "legacy_unhashed_replays",
            "message": (f"{len(legacy_unhashed)} replay(s) predate source hashing "
                        "and were bound by modification time only"),
            "hint": "Re-run 'autonom teach approve <flow> --run' to bind every "
                    "replay to the flow's bytes.",
            "run_ids": list(legacy_unhashed),
        })
    receipt = {
        "schema": "autonom.teach-approval/v1", "approved_at": utc_now(),
        "flow": str(flow_path), "flow_id": flow.flow_id,
        "flow_sha256": current,
        "subflow_sha256": subflow_hashes(flow_path),
        "clean_replays": [item["run_id"] for item in consecutive[:minimum_runs]],
        "replay_binding": bindings,
        "legacy_unhashed": legacy_unhashed,
    }
    if warnings:
        receipt["warnings"] = warnings
    receipt_path = flow_path.with_suffix(flow_path.suffix + ".approved.json")
    receipt_path.write_bytes(canonical_json(receipt) + b"\n")
    return {**receipt, "receipt": str(receipt_path)}
