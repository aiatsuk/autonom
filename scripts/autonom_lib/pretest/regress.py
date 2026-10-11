"""Regression drafts, reproducibility and reruns (tp-020..tp-022).

``flow_draft(session, record_or_pack, finding_id, out=None)`` compiles the
journal range of the finding's check (``seq_from``..``seq_to``) with the
Session→Flow compiler (``flow.compiler.compile_session``) into a draft flow,
by default ``<repo>/.autonom/flows/drafts/<ticket-slug>-<finding-id>.yaml``.
The draft carries the tag ``draft``, ``properties.covers`` with the
pre-test's changed files (each path escaped as a literal glob; a path with a
comma cannot be written into the comma-separated list and is left out with a
warning), and ``properties.pretest`` / ``properties.finding``. The compiler's
closing assertion is bound to the check: it comes from the last successful
``ui find`` made while that check was open, never from another check; a
check without one gets no closing assertion and a ``no_final_assertion``
warning. The file is written owner-only and must pass ``flow check``
(``flow.validator.validate_tree``); a draft that does not is removed again.
An existing file with other content is never overwritten
(``output_not_writable``); the same content is reported ``unchanged``.
A finding without a check, a check that never began, or a check whose range
holds no replayable action (a tap, typed text, a key, a launch, an open...)
is ``draft_unavailable``. On the active pre-test the draft's path is recorded
on the finding (an ``update`` event with ``draft_flow``). On a pack
(``--pack``), the pack is verified first, its own records are read, the
journal of the session that made it is compiled when that session still
exists on this machine, and nothing is recorded anywhere: a pack is final.

The draft states what the agent saw: review its closing assertion so it
checks the EXPECTED behaviour. A run of the draft that fails with a test
failure then reproduces the finding, and a passing run means the finding is
gone.

``repro(session, finding_id, times, target)`` (the active pre-test only)
replays the finding's draft (its recorded ``draft_flow``, else the default
draft path) ``times`` times (1..10) through the flow executor. Each run is
``failed`` (a test failure: the finding reproduced), ``passed`` (not
reproduced), ``broken`` (infrastructure or definition failure) or
``skipped`` (every step skipped); only failed and passed runs are
conclusive, and a run refused by a guard (budget, forbidden action, app) never
is. The finding gets ``reproduced: "k/N"`` (k failed runs out of the N runs
asked for), ``stability`` (``stable`` k=N, ``intermittent`` 0<k<N,
``not_reproduced`` k=0) and ``repro_runs`` (the N run ids) as one ``update``
event; the finding always stays a finding. N is always ``times``: the first
run that reaches no verdict (broken, skipped, aborted, refused) stops the
loop, nothing is recorded, and its cause is raised with every run so far, so
a smaller denominator never reaches the finding. Repro refuses
while a check is open (its runs would land in that check's window). A pack
cannot be changed, so ``repro --pack`` creates nothing and answers
``pretest_finalized`` (``rerun`` is the way to replay a pack's drafts).

``rerun(session, pack, check_ids, target)`` verifies the pack, starts a child
pre-test in the session (``kind: "rerun"``, ``parent_pack`` and
``rerun_of: {"pack", "checks"}``) with the pack's ticket and diff context but
this session's app and the default safety rules (never the pack's), and runs
one flow per chosen check: the check's own flow file, else the first draft of
its findings that exists on this machine. Each becomes a child check with the
parent's title, kind and case (so the child's pack pairs it with the parent
check and shows ``before``/``after``), begun and ended with the run's verdict
(passed→pass, failed→fail, broken→blocked, all skipped→inconclusive). Without
``--check`` every parent check with a flow is chosen. A chosen check without
a flow, or a pack with no runnable check, is ``draft_unavailable`` and
nothing is created. The child stays active so findings can be added; pack it
with ``autonom pretest pack``. The parent pack is only read, never written.

Flow steps run by ``repro`` and ``rerun`` count against the pre-test budget
like any flow step; the verbs themselves do not.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .. import errors
from .. import actions as actions_mod
from .. import journal as journal_mod
from .. import session as session_mod
from ..flow import compiler as flow_compiler
from ..flow import executor as flow_executor
from ..flow import schema as flow_schema
from ..flow import validator as flow_validator
from ..flow.canonical import emit_flow
from ..flow.parser import parse_document
from . import checks as checks_mod
from . import findings as findings_mod
from . import pack as pack_mod
from . import store

DRAFT_DIR = (".autonom", "flows", "drafts")
DRAFT_TAG = "draft"
MIN_TIMES = 1
MAX_TIMES = 10
SLUG_LIMIT = 48
# flow run status -> check verdict (tp-008, the same mapping `pretest flows` uses)
VERDICTS = {"passed": "pass", "failed": "fail", "broken": "blocked",
            "skipped": "inconclusive"}
# Steps the compiler emits that act on the device or the app. A draft holding
# none of them (only notes, screenshots or an assertion) replays nothing.
REPLAYABLE_COMMANDS = frozenset({
    "launchApp", "clearState", "stopApp", "openLink", "tapOn", "longPressOn",
    "inputText", "back", "pressKey", "setLocation", "setPermissions",
})
# guard refusals repeat the same way on every run: repro stops at the first
_GUARD_REFUSALS = frozenset({errors.BUDGET_EXHAUSTED, errors.FORBIDDEN_ACTION,
                             errors.APP_NOT_ALLOWED})
_SLUG = re.compile(r"[^a-z0-9]+")
_GLOB_SPECIAL = re.compile(r"([*?\[])")


# --- small helpers ---------------------------------------------------------------------


def execution(status: str, failure: dict[str, Any] | None, steps: Iterable[Any]) -> str:
    """passed|failed|broken|skipped for one finished flow run (a key of
    ``VERDICTS``): a failure that is not the app's (infrastructure,
    definition) is broken, and a run whose every step was skipped proved
    nothing."""
    steps = list(steps or [])
    if status in ("passed", "replayed"):
        if steps and all(getattr(step, "status", None) == "skipped" for step in steps):
            return "skipped"
        return "passed"
    if (failure or {}).get("failure_class") in (None, "test_failure"):
        return "failed"
    return "broken"


def slug(record: dict[str, Any]) -> str:
    """The ticket slug of a draft's file name: the ticket key, else the title."""
    for value in (record.get("ticket_ref"), record.get("title")):
        text = _SLUG.sub("-", str(value or "").lower()).strip("-")[:SLUG_LIMIT].strip("-")
        if text:
            return text
    return "pretest"


def default_draft_path(record: dict[str, Any], finding_id: str) -> Path:
    """``<repo>/.autonom/flows/drafts/<ticket-slug>-<finding-id>.yaml`` (the
    working directory when the record names no repository)."""
    base = Path(record["repo"]) if record.get("repo") else Path.cwd()
    return base.joinpath(*DRAFT_DIR) / f"{slug(record)}-{finding_id}.yaml"


def _unavailable(message: str, hint: str, **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(errors.DRAFT_UNAVAILABLE, message, hint=hint, **extra)


def _unknown_finding(finding_id: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.UNKNOWN_FINDING, f"no finding {finding_id!r} in this pre-test",
        hint="List findings with 'autonom pretest finding list' (or read the pack's "
             "summary.json).", finding_id=finding_id)


def _find(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get(key) == value), None)


def _is_file(value: Any) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        return Path(value).expanduser().is_file()
    except (OSError, ValueError):
        return False


def _covers(context: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Changed files as literal globs for ``properties.covers``."""
    patterns: list[str] = []
    warnings: list[dict[str, Any]] = []
    for item in context.get("changed_files") or []:
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path.strip():
            continue
        if "," in path or path != path.strip():
            warnings.append({
                "code": "covers_path_skipped",
                "error": f"{path!r} cannot be written into the comma-separated covers list",
                "hint": "Add a glob that matches it to properties.covers by hand."})
            continue
        pattern = _GLOB_SPECIAL.sub(r"[\1]", path)
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns, warnings


# --- reading a pack ------------------------------------------------------------------


def _pack_json(pack: Path, name: str) -> Any:
    try:
        return json.loads(pack_mod.read_regular_bytes(pack / name).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"cannot read the pack's {name}: {exc}",
            hint="Verify the pack with 'autonom report verify <pack>'.",
            path=str(pack)) from None


def _pack_lines(pack: Path, name: str) -> list[dict[str, Any]]:
    try:
        text = pack_mod.read_regular_bytes(pack / name).decode("utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"cannot read the pack's {name}: {exc}",
            hint="Verify the pack with 'autonom report verify <pack>'.",
            path=str(pack)) from None
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def read_pack(pack: str | Path) -> dict[str, Any]:
    """A verified pack's record, folded checks and findings, ticket and
    context. ``not_a_pretest_pack`` / ``report_integrity_failed`` otherwise."""
    try:
        path = Path(pack).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"{pack!r} cannot be resolved ({type(exc).__name__})",
            hint="Pass the directory 'autonom pretest pack' printed.",
            path=str(pack)) from None
    if not path.is_dir():
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"{pack} is not a directory",
            hint="Pass the directory 'autonom pretest pack' printed.", path=str(pack))
    pack_mod.verify(path)
    record = store.find_pack_record(path)
    checks = checks_mod.fold_events([event for event in _pack_lines(path, store.CHECKS_FILE)
                                     if not checks_mod.event_failures(event)])
    findings = findings_mod.fold_events([
        event for event in _pack_lines(path, store.FINDINGS_FILE)
        if not findings_mod.event_failures(event)])
    context = _pack_json(path, store.CONTEXT_FILE)
    if not isinstance(context, dict):
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, "the pack's context.json is not an object",
            hint="Verify the pack with 'autonom report verify <pack>'.", path=str(path))
    try:
        ticket = pack_mod.read_regular_bytes(path / store.TICKET_FILE).decode(
            "utf-8", errors="replace")
    except OSError as exc:
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"cannot read the pack's ticket: {exc}",
            hint="Verify the pack with 'autonom report verify <pack>'.",
            path=str(path)) from None
    return {"path": path, "record": record, "checks": checks, "findings": findings,
            "context": context, "ticket": ticket}


def _source_session(record: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    """The session whose journal holds a pack's checks; its journal must
    still be on this machine."""
    session_id = str(record.get("session_id") or "")
    hint = ("A draft is compiled from the journal of the session that ran the check; "
            "run flow-draft on this machine while that session's directory exists, "
            "or write the flow by hand.")
    if current and current.get("session_id") == session_id:
        found: Any = current
    else:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", session_id) \
                or session_id in (".", ".."):
            raise _unavailable("the pack names no session", hint,
                               session_id=session_id or None)
        path = session_mod.sessions_home() / session_id / "session.json"
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise _unavailable(f"session {session_id} is not on this machine", hint,
                               session_id=session_id) from None
        if not isinstance(found, dict) or not found.get("artifacts_dir"):
            raise _unavailable(f"session {session_id} cannot be read", hint,
                               session_id=session_id)
        found = session_mod.upgrade(found)
    try:
        present = journal_mod.journal_path(found).is_file()
    except (OSError, TypeError, ValueError, KeyError):
        present = False
    if not present:
        raise _unavailable(f"the journal of session {session_id} is not on this machine",
                           hint, session_id=session_id)
    return found


# --- flow-draft --------------------------------------------------------------------------


def _bind_closing_assertion(session: dict[str, Any], flow: Any, report: dict[str, Any],
                            pretest_id: str, check_id: str) -> None:
    """The compiler closes a flow with the session's LAST successful find;
    a draft must close with its own check's (see the module text)."""
    warnings = report.setdefault("warnings", [])
    compiled = (flow.steps and flow.steps[-1].command == "assertVisible"
                and not any(item.get("code") == "no_final_assertion" for item in warnings))
    own = None
    for detail in reversed(list(actions_mod.read_details(session).values())):
        if (isinstance(detail, dict) and detail.get("kind") == "find"
                and (detail.get("count") or 0) >= 1
                and detail.get("pretest_id") == pretest_id
                and detail.get("check_id") == check_id):
            own = flow_compiler._selector_from_detail(detail)  # noqa: SLF001
            if own is not None:
                break
    if compiled:
        flow.steps.pop()
    if own is not None:
        flow.steps.append(flow_schema.Step("assertVisible", {"selector": own}))
        warnings[:] = [item for item in warnings if item.get("code") != "no_final_assertion"]
    elif not any(item.get("code") == "no_final_assertion" for item in warnings):
        warnings.append({
            "code": "no_final_assertion",
            "error": f"check {check_id} made no successful 'ui find'; the draft has no "
                     "closing assertion",
            "hint": "Add the assertion that states the expected result before relying "
                    "on the draft."})
    report.setdefault("quality", {})["steps"] = len(flow.steps)


def _write_draft(path: Path, text: str) -> bool:
    """Write the draft owner-only and atomically; False when the file already
    holds exactly this text. Never overwrites other content."""
    flag = "--out"
    if path.is_dir():
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE, f"{path} is a directory",
            hint="Pass a .yaml file path with --out.", path=str(path), flag=flag)
    if path.exists() or path.is_symlink():
        try:
            same = (not path.is_symlink() and path.is_file()
                    and path.read_text(encoding="utf-8") == text)
        except (OSError, UnicodeDecodeError):
            same = False
        if same:
            return False
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE, f"{path} already exists with other content",
            hint="A draft is never overwritten (it may hold your review); delete it or "
                 "pass another --out.", path=str(path), flag=flag)
    try:
        if not path.parent.is_dir():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp makes a fresh, unpredictable name with O_EXCL (0o600): a file or
        # link planted beside the draft is never followed or truncated
        fd, scratch_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                            dir=path.parent)
        scratch = Path(scratch_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(scratch, 0o600)
            if path.exists() or path.is_symlink():  # appeared meanwhile: never clobber
                raise FileExistsError(str(path))
            os.replace(scratch, path)
        finally:
            with contextlib.suppress(OSError):
                scratch.unlink()
    except FileExistsError:
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE, f"{path} appeared while the draft was written",
            hint="Run flow-draft again, or pass another --out.", path=str(path),
            flag=flag) from None
    except OSError as exc:
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE, f"cannot write {path}: {exc.strerror or exc}",
            hint="Pass a writable --out path.", path=str(path), flag=flag) from None
    return True


def flow_draft(session: dict[str, Any] | None, record_or_pack: dict[str, Any] | str | Path,
               finding_id: str, *, out: str | Path | None = None) -> dict[str, Any]:
    """Compile a finding's check into a draft flow (see the module text)."""
    if isinstance(record_or_pack, dict):
        record = record_or_pack
        store.ensure_mutable(record)
        if not session:
            raise errors.AutonomError(
                errors.NO_ACTIVE_SESSION, "no active session",
                hint="Start one with 'autonom session start --serial <id> --app-id <app>'.")
        checks = checks_mod.fold(session, record)
        found = findings_mod.fold(session, record)
        try:
            context = store.read_context(session, record["pretest_id"])
        except (OSError, ValueError):
            context = {}
        source, pack = session, None
    else:
        state = read_pack(record_or_pack)
        record, checks, found, context = (state["record"], state["checks"],
                                          state["findings"], state["context"])
        pack = state["path"]
        source = None
    finding = _find(found, "finding_id", finding_id)
    if finding is None:
        raise _unknown_finding(finding_id)
    check_id = finding.get("check_id")
    if not check_id:
        raise _unavailable(
            f"finding {finding_id} belongs to no check", "A draft replays the actions of "
            "the finding's check; write the flow by hand for a finding outside a check.",
            finding_id=finding_id)
    check = _find(checks, "check_id", check_id)
    if check is None or not isinstance(check.get("seq_from"), int) \
            or isinstance(check.get("seq_from"), bool):
        raise _unavailable(
            f"check {check_id} of finding {finding_id} never began",
            "Only a check that was begun has a journal range to compile.",
            finding_id=finding_id, check_id=check_id)
    if source is None:
        source = _source_session(record, session)
    seq_to = check.get("seq_to")
    seq_to = seq_to if isinstance(seq_to, int) and not isinstance(seq_to, bool) else None
    title = str(finding.get("title") or finding_id)
    name = store.clip(f"Draft {finding_id}: {title}", 200) or f"Draft {finding_id}"
    try:
        flow, report = flow_compiler.compile_session(
            source, name=name, start_seq=check["seq_from"], end_seq=seq_to)
    except errors.AutonomError as exc:
        if exc.code != errors.FLOW_CHECK_FAILED:
            raise
        raise _unavailable(
            f"check {check_id} has no replayable actions", "Drive the check with "
            "selector-based 'ui tap', 'ui type' or 'ui key' (coordinate taps and swipes "
            "do not compile), or write the flow by hand.", finding_id=finding_id,
            check_id=check_id, warnings=exc.extra.get("warnings") or []) from None
    _bind_closing_assertion(source, flow, report, str(record["pretest_id"]), check_id)
    if not any(step.command in REPLAYABLE_COMMANDS for step in flow.steps):
        raise _unavailable(
            f"check {check_id} has no replayable actions", "Drive the check with "
            "selector-based 'ui tap', 'ui type' or 'ui key' (coordinate taps and swipes "
            "do not compile), or write the flow by hand.", finding_id=finding_id,
            check_id=check_id, warnings=report.get("warnings") or [])
    patterns, cover_warnings = _covers(context if isinstance(context, dict) else {})
    warnings = list(report.get("warnings") or []) + cover_warnings
    if not patterns:
        warnings.append({"code": "no_changed_files",
                         "error": "the pre-test lists no changed file; the draft has no covers",
                         "hint": "Add properties.covers globs by hand so 'proof' selects it."})
    flow.tags = [DRAFT_TAG] + [tag for tag in flow.tags if tag != DRAFT_TAG]
    properties = {"pretest": str(record["pretest_id"]), "finding": finding_id}
    if patterns:
        properties["covers"] = ",".join(patterns)
    flow.properties = {**flow.properties, **properties}
    text = emit_flow(flow)
    flow_schema.build_flow(parse_document(text, "<generated>"))  # never write the unparseable
    try:
        path = Path(out).expanduser() if out else default_draft_path(record, finding_id)
    except RuntimeError as exc:  # an unknown ~user
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE, f"--out {out!r} cannot be resolved ({exc})",
            hint="Pass an absolute path or one relative to the current directory.",
            path=str(out), flag="--out") from None
    path = path if path.is_absolute() else Path.cwd() / path
    written = _write_draft(path, text)
    try:
        checked = flow_validator.validate_tree(path)
    except errors.AutonomError as exc:
        if written:
            with contextlib.suppress(OSError):
                path.unlink()
        raise _unavailable(
            f"the draft does not pass flow check: {exc.message}",
            "Report this; the compiled flow should always validate.",
            finding_id=finding_id, check=exc.as_dict()) from None
    recorded = False
    if pack is None and finding.get("draft_flow") != str(path):
        findings_mod.update(session, record, finding_id, {"draft_flow": str(path)})
        recorded = True
    secrets = list(report.get("secrets_required") or [])
    return {
        "pretest_id": record["pretest_id"], "finding_id": finding_id, "check_id": check_id,
        "draft_flow": str(path), "written": written, "recorded": recorded,
        "pack": str(pack) if pack else None,
        "covers": patterns, "tags": list(checked.tags),
        "steps": len(checked.steps), "flow_check": {"ok": True, "name": checked.name},
        "secrets_required": secrets, "confidence": report.get("confidence"),
        "review_required": True, "warnings": warnings,
        "repro": f"autonom pretest repro {finding_id} --times 3"
                 + "".join(f" --secret {name}" for name in secrets),
    }


# --- running flows -----------------------------------------------------------------------


def _failure(exc: errors.AutonomError) -> dict[str, Any]:
    return {"error_code": exc.code, "failure_class": flow_schema.failure_class(exc.code),
            "error": exc.message}


def _run_once(target: Any, session: dict[str, Any], config: Any,
              path: Path) -> dict[str, Any]:
    """One executor run of a flow file: ``{"run_id", "status", "failure",
    "aborted"}``; an aborted run (an AutonomError from the run) is broken."""
    run: dict[str, Any] = {"run_id": None, "status": "broken", "failure": None,
                           "aborted": False}
    try:
        flow = flow_validator.validate_tree(path)
    except errors.AutonomError as exc:
        run["failure"] = _failure(exc)
        return run
    runner = flow_executor.Executor(target, session, config)
    try:
        result = runner.run(flow)
    except errors.AutonomError as exc:
        run.update(run_id=getattr(runner, "_writer_run_id", None), failure=_failure(exc),
                   aborted=True)
        return run
    run.update(run_id=result.run_id, failure=result.failure,
               status=execution(result.status, result.failure, result.steps))
    return run


def _config(config: Any) -> Any:
    return config if config is not None else flow_executor.RunConfig()


# --- repro -------------------------------------------------------------------------------


def check_times(times: Any) -> int:
    if isinstance(times, bool) or not isinstance(times, int) \
            or not MIN_TIMES <= times <= MAX_TIMES:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--times must be a whole number from {MIN_TIMES} to "
                                  f"{MAX_TIMES}, got {times!r}",
            hint="For example --times 3.", times=times)
    return times


def stability(reproduced: int, times: int) -> str:
    """tp-021 over the N runs asked for: stable k=N, intermittent 0<k<N,
    not_reproduced k=0."""
    if reproduced == 0:
        return "not_reproduced"
    return "stable" if reproduced == times else "intermittent"


def _conclusive(run: dict[str, Any]) -> bool:
    """A run with a verdict about the app: failed or passed, never a guard
    refusal (whatever status the executor gave it)."""
    refused = (run["failure"] or {}).get("error_code") in _GUARD_REFUSALS
    return run["status"] in ("failed", "passed") and not refused and not run["aborted"]


def repro(session: dict[str, Any] | None, finding_id: str, *, times: int, target: Any,
          config: Any = None) -> dict[str, Any]:
    """Replay a finding's draft ``times`` times (see the module text)."""
    times = check_times(times)
    record = store.require_active(session)
    store.ensure_mutable(record)
    finding = _find(findings_mod.fold(session, record), "finding_id", finding_id)
    if finding is None:
        raise _unknown_finding(finding_id)
    opened = checks_mod.open_check(session, record)
    if opened is not None:
        raise errors.AutonomError(
            errors.CHECK_ALREADY_OPEN, f"check {opened['check_id']} is still open",
            hint=f"End it first with 'autonom pretest check end {opened['check_id']} "
                 "--verdict <verdict>'; repro runs never land inside a check.",
            open_check_id=opened["check_id"])
    draft = next((str(Path(candidate).expanduser()) for candidate in (
        finding.get("draft_flow"), str(default_draft_path(record, finding_id)))
        if _is_file(candidate)), None)
    if draft is None:
        raise _unavailable(
            f"finding {finding_id} has no draft flow",
            f"Write one first with 'autonom pretest flow-draft {finding_id}', review its "
            "closing assertion, then run repro again.", finding_id=finding_id)
    config = _config(config)
    runs: list[dict[str, Any]] = []
    for _ in range(times):
        run = _run_once(target, session, config, Path(draft))
        runs.append(run)
        if not _conclusive(run):
            # k/N needs a verdict from every one of the N runs (tp-021); a spent
            # budget, a dead backend or a broken draft cannot give one
            break
    shown = [{key: run[key] for key in ("run_id", "status", "failure")} for run in runs]
    if len(runs) < times or not all(_conclusive(run) for run in runs):
        stopped = runs[-1]
        failure = stopped["failure"] or {}
        raise errors.AutonomError(
            failure.get("error_code") or errors.BACKEND_FAILED,
            f"repro of {finding_id} stopped at run {len(runs)} of {times}: it reached no "
            f"verdict ({stopped['status']})"
            + (f": {failure.get('error')}" if failure.get("error") else ""),
            hint=f"Nothing was recorded on the finding: reproduced k/{times} needs a verdict "
                 "from every run. Fix the cause (budget, device, secrets, the draft) and "
                 "run repro again.",
            finding_id=finding_id, draft_flow=draft, times=times, runs=shown)
    reproduced = sum(1 for run in runs if run["status"] == "failed")
    run_ids = [str(run["run_id"]) for run in runs if run["run_id"]]
    fields: dict[str, Any] = {"reproduced": f"{reproduced}/{times}",
                              "stability": stability(reproduced, times),
                              "repro_runs": run_ids}
    if finding.get("draft_flow") != draft:
        fields["draft_flow"] = draft
    updated = findings_mod.update(session, record, finding_id, fields)
    payload: dict[str, Any] = {
        "pretest_id": record["pretest_id"], "finding_id": finding_id, "draft_flow": draft,
        "times": times, "runs": shown, "reproduced": updated["reproduced"],
        "stability": updated["stability"], "repro_runs": updated["repro_runs"],
        "severity": updated["severity"], "confirmed": updated["confirmed"]}
    return payload


# --- rerun -------------------------------------------------------------------------------


def _check_flow(check: dict[str, Any], found: list[dict[str, Any]],
                record: dict[str, Any]) -> tuple[str | None, str | None]:
    """(flow path, where it came from) for one parent check, or (None, None)."""
    if _is_file(check.get("flow")):
        return str(Path(check["flow"]).expanduser()), "flow"
    for finding in found:
        if finding.get("check_id") != check.get("check_id"):
            continue
        for candidate in (finding.get("draft_flow"),
                          str(default_draft_path(record, finding["finding_id"]))):
            if _is_file(candidate):
                return str(Path(candidate).expanduser()), f"draft:{finding['finding_id']}"
    return None, None


def plan_rerun(pack: str | Path, check_ids: Iterable[str] = ()) -> dict[str, Any]:
    """The verified pack and the flows each chosen check runs; refuses
    (creating nothing) when a chosen check has none."""
    state = read_pack(pack)
    parent, checks, found = state["record"], state["checks"], state["findings"]
    wanted: list[str] = []
    for check_id in check_ids or ():
        if check_id not in wanted:
            wanted.append(str(check_id))
    for check_id in wanted:
        if _find(checks, "check_id", check_id) is None:
            raise errors.AutonomError(
                errors.UNKNOWN_CHECK, f"no check {check_id!r} in the pack",
                hint="The pack's checks are in its summary.json (or summary.md).",
                check_id=check_id, pack=str(state["path"]))
    plan: list[dict[str, Any]] = []
    missing: list[str] = []
    for check in checks:
        if wanted and check["check_id"] not in wanted:
            continue
        path, origin = _check_flow(check, found, parent)
        if path is None:
            if wanted:
                missing.append(check["check_id"])
            continue
        plan.append({"check": check, "flow": path, "source": origin})
    if wanted:
        plan.sort(key=lambda item: wanted.index(item["check"]["check_id"]))
    if missing:
        raise _unavailable(
            f"{', '.join(missing)} {'has' if len(missing) == 1 else 'have'} no flow or draft "
            "on this machine",
            "Write a draft for the check's finding with 'autonom pretest flow-draft "
            "<finding-id> --pack <pack>', or rerun only checks that have a flow.",
            checks=missing, pack=str(state["path"]))
    if not plan:
        raise _unavailable(
            "no check of the pack has a flow or draft on this machine",
            "Write drafts with 'autonom pretest flow-draft <finding-id> --pack <pack>' "
            "first, or check by hand in a new pre-test.", pack=str(state["path"]))
    return {**state, "plan": plan}


def rerun(session: dict[str, Any] | None, pack: str | Path, *, check_ids: Iterable[str],
          target: Any, config: Any = None) -> dict[str, Any]:
    """Start a rerun child of ``pack`` and run the chosen checks' flows."""
    if not session:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION, "no active session",
            hint="Start one with 'autonom session start --serial <id> --app-id <app>'.")
    existing = store.active(session)
    if existing is not None:
        raise errors.AutonomError(
            errors.PRETEST_ALREADY_ACTIVE,
            f"pre-test {existing.get('pretest_id')} is still active in this session",
            hint="The rerun child starts in this session; pack or stop the active "
                 "pre-test first.", pretest_id=existing.get("pretest_id"))
    state = plan_rerun(pack, check_ids)
    parent, path = state["record"], state["path"]
    chosen = [item["check"]["check_id"] for item in state["plan"]]
    child = store.create(
        session, target=target, ticket_text=state["ticket"],
        ticket_ref=parent.get("ticket_ref"), title=parent.get("title"),
        repo=parent.get("repo"), base=parent.get("base"), head=parent.get("head"),
        flows_dir=parent.get("flows_dir"), app_id=None, budget=None,
        parent_pack=path, kind="rerun",
        rerun_of={"pack": str(path), "checks": chosen}, context=state["context"])
    config = _config(config)
    runs: list[dict[str, Any]] = []
    for item in state["plan"]:
        before = item["check"]
        added = checks_mod.add(session, child, title=before["title"], kind=before["kind"],
                               case=before.get("case"),
                               why=f"rerun of {before['check_id']}", flow=item["flow"])
        checks_mod.begin(session, child, added["check_id"])
        try:
            run = _run_once(target, session, config, Path(item["flow"]))
        except BaseException as exc:
            # an unexpected stop never leaves the check open; the error still wins
            with contextlib.suppress(Exception):
                checks_mod.end(session, child, added["check_id"], verdict="blocked",
                               note=f"run stopped: {type(exc).__name__}")
            raise
        failure = run["failure"] or {}
        note = (f"{failure.get('error_code')}: {failure.get('error')}"
                if failure.get("error_code") else None)
        ended = checks_mod.end(session, child, added["check_id"],
                               verdict=VERDICTS[run["status"]], note=note,
                               run_id=run["run_id"])
        runs.append({"check_id": added["check_id"], "parent_check_id": before["check_id"],
                     "title": before["title"], "flow": item["flow"], "source": item["source"],
                     "run_id": run["run_id"], "status": run["status"],
                     "before": before.get("verdict"), "after": ended["verdict"],
                     "failure": run["failure"]})
    return {"pretest_id": child["pretest_id"], "kind": "rerun", "status": "active",
            "parent_pack": str(path), "rerun_of": {"pack": str(path), "checks": chosen},
            "app_id": child.get("app_id"), "budget": child.get("budget"),
            "count": len(runs), "runs": runs,
            "next": "Add findings if needed, then 'autonom pretest pack'; the child's "
                    "pack shows each check before and after."}
