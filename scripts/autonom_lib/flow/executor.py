"""Flow v1 executor: run a validated flow against a resolved target.

Semantics fixed by the language (docs/FLOW.md, decisions in
docs/plans/PHASE_5_FLOW_DSL.md):

- **Pre-flight before any mutation.** The typed model — including every
  ``runFlow`` child and both hooks — is walked against the target:
  platform-impossible commands, missing appId, and unresolvable ``${VAR}``
  references fail with zero device side effects. ``--dry-run`` stops here.
- **Assertions poll; mutations fire exactly once.** The polling engine runs
  on ``time.monotonic`` with an injectable clock. A ``tapOn`` polls only
  while *zero* nodes match — the moment matches exist, the shared selection
  rule applies once (ambiguity refuses, reusing ``selector.select``) and the
  tap is dispatched exactly once, never retried.
- **Failure classes.** An assertion timeout is a *test failure* (the app did
  not do what the flow asserts): the run returns a failed summary and the
  CLI exits 1. A malformed-for-this-target flow is a *definition* error and
  a dead backend is *infrastructure*: both raise and exit 2.
- **Hooks.** ``onFlowStart`` runs before the steps and aborts the run when
  it fails. ``onFlowComplete`` runs after pass *and* fail, each command
  isolated: a cleanup failure is recorded as its own event and never
  overwrites the primary outcome. Failure evidence is captured at the
  failing step, before cleanup runs.
- **Subflows.** ``runFlow`` children (already statically contained and
  cycle-checked) execute inline with their own env frame
  (child header env < runFlow env < ``--env`` < secrets); the root ``appId``
  is inherited; child hooks do not run. A false ``when:`` skips the step
  with the failed condition as the reason.
- **Secrets never persist.** Interpolated values are resolved per use;
  event payloads carry no raw values; selector text in events is the
  uninterpolated source.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import device_state, errors, ios_simctl
from .. import journal as journal_mod
from .. import screenshot as screenshot_mod
from .. import selector as selector_engine
from .. import session as session_mod
from .. import ui as ui_mod
from ..platform import ANDROID, IOS, Target
from ..atlas import fingerprint as atlas_fingerprint
from . import conditions as flow_conditions
from . import selectors as flow_selectors
from . import validator as flow_validator
from .events import EventWriter
from .schema import (
    Evidence,
    Flow,
    FlowSelector,
    MATCH_MODES,
    Step,
    TEST_FAILURE,
    WhenClause,
    failure_class,
)

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class RunConfig:
    env: dict = field(default_factory=dict)          # --env overrides
    secrets: dict = field(default_factory=dict)      # name -> value
    default_timeout_ms: int = 10_000
    interval_ms: int = 500
    dry_run: bool = False


@dataclass
class StepOutcome:
    index: int
    command: str
    label: str | None
    line: int
    status: str            # passed | failed | skipped
    flow: str | None = None
    hook: str | None = None
    duration_ms: int = 0
    attempts: int = 1
    error_code: str | None = None
    failure_class: str | None = None
    error: str | None = None
    skip_reason: str | None = None


@dataclass
class RunResult:
    run_id: str
    status: str            # passed | failed
    steps: list = field(default_factory=list)
    failure: dict | None = None
    hook_failures: list = field(default_factory=list)
    sensitive: bool = False
    events_path: str | None = None


def _iter_var_names(text: str):
    index = 0
    while True:
        position = text.find("${", index)
        if position < 0:
            return
        if position > 0 and text[position - 1] == "$":
            index = position + 2
            continue
        match = _VAR_RE.match(text, position)
        if match:  # parser guarantees this, but stay safe
            yield match.group(1)
            index = match.end()
        else:
            index = position + 2


class _Stop(Exception):
    """Internal: a step failed as a test failure; unwind to the run loop."""


class Executor:
    def __init__(self, target: Target, session_record: dict, config: RunConfig,
                 *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 stdout_stream: Any | None = None,
                 screen: tuple[int, int] | None = None) -> None:
        self.target = target
        self.session = session_record
        self.config = config
        self.clock = clock
        self.sleep = sleep
        self.stdout_stream = stdout_stream
        self._screen = screen
        self._screen_fetched = screen is not None
        self.values: dict[str, str] = {}
        self.secret_values: set[str] = set()
        self._children: dict[str, Flow] = {}
        self._counter = 0
        self._used_secret_anywhere = False
        self._retry_attempt: int | None = None
        self._writer_run_id: str | None = None
        self._last_nodes: list | None = None

    # -- public ---------------------------------------------------------------

    def run(self, flow: Flow) -> RunResult:
        root_values = {**flow.env, **self.config.env, **self.config.secrets}
        self.values = root_values
        self.secret_values = set(self.config.secrets)
        self._children = {}
        self._collect_children(flow, flow.app_id)
        self._preflight_flow(flow, root_values, is_root=True)

        run_id = f"fr_{uuid.uuid4().hex[:10]}"
        writer = EventWriter(
            self.session, run_id, flow.flow_id or flow.name,
            self.target.platform, self.target.target_id,
            serial=self.target.serial, stdout_stream=self.stdout_stream,
        )
        self._writer_run_id = run_id
        result = RunResult(run_id=run_id, status="passed",
                           events_path=str(writer.path))
        evidence = flow.evidence or Evidence()
        unsupported = [k for k in evidence.collect if k in ("logs", "crashes", "network")]
        writer.emit("flow.run.started", {
            "flow": flow.path, "name": flow.name, "app_id": flow.app_id,
            "tags": flow.tags, "steps": len(flow.steps),
            "dry_run": self.config.dry_run,
            **({"converted_from": flow.converted_from}
               if flow.converted_from else {}),
            **({"warnings": [{"code": "flow_evidence_kind_unsupported",
                              "kinds": unsupported}]} if unsupported else {}),
        })
        if self.config.dry_run:
            writer.emit("flow.run.finished",
                        {"status": "passed", "dry_run": True, "steps": 0})
            return result

        try:
            if flow.on_flow_start:
                self._execute_steps(flow.on_flow_start, flow, root_values,
                                    writer, result, evidence, hook="onFlowStart")
            self._execute_steps(flow.steps, flow, root_values,
                                writer, result, evidence)
        except _Stop:
            result.status = "failed"
        finally:
            if flow.on_flow_complete and not self.config.dry_run:
                self._run_complete_hooks(flow, root_values, writer, result,
                                         evidence)

        if result.status == "failed" and result.steps:
            failed = next((s for s in result.steps if s.status == "failed"), None)
            if failed is not None:
                result.failure = {
                    "step_index": failed.index,
                    "command": failed.command,
                    "line": failed.line,
                    "flow": failed.flow,
                    "error_code": failed.error_code,
                    "failure_class": failed.failure_class,
                    "error": failed.error,
                }
        result.sensitive = self._used_secret_anywhere or any(
            step.args.get("sensitive") for step in flow.steps)
        writer.emit("flow.run.finished", {
            "status": result.status,
            "steps": len(result.steps),
            "failure": result.failure,
        }, sensitive=result.sensitive)
        self._write_manifest(flow, writer, result)
        return result

    def _write_manifest(self, flow: Flow, writer: EventWriter,
                        result: RunResult) -> None:
        """One evidence manifest per run (§9.2) — the report's only input."""
        try:
            run_dir = writer.run_dir()
            shots_dir = Path(self.session.get("artifacts_dir", "")) / "shots" / writer.run_id
            artifacts = sorted(
                str(p.relative_to(self.session["artifacts_dir"]))
                for base in (run_dir, shots_dir) if base.is_dir()
                for p in base.rglob("*") if p.is_file())
            manifest = {
                "schema_version": 1,
                "session_id": self.session.get("session_id"),
                "run_id": writer.run_id,
                "flow_id": flow.flow_id,
                "flow_name": flow.name,
                "flow_path": flow.path,
                "app_id": flow.app_id,
                "platform": self.target.platform,
                "target_id": self.target.target_id,
                "status": result.status,
                "sensitive": result.sensitive,
                "primary_error": result.failure,
                "hook_failures": result.hook_failures,
                "steps": [
                    {k: v for k, v in vars(step).items() if v is not None}
                    for step in result.steps
                ],
                "artifacts": artifacts,
                "reproduction": self._reproduction_command(flow),
            }
            path = run_dir / "manifest.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            import os as os_mod
            os_mod.chmod(path, 0o600)
        except Exception:  # noqa: BLE001 — evidence loss never fails the run
            pass

    def _reproduction_command(self, flow: Flow) -> str:
        parts = [f"autonom flow run {flow.path}"]
        parts.extend(f"--secret {name}" for name in sorted(self.secret_values))
        for key, value in self.config.env.items():
            parts.append(f"--env {key}={value}")
        return " ".join(parts)

    # -- pre-flight -----------------------------------------------------------

    def _collect_children(self, flow: Flow, inherited_app_id: str | None) -> None:
        for step in (*flow.on_flow_start, *flow.steps, *flow.on_flow_complete):
            if step.command != "runFlow":
                continue
            target_path = (Path(flow.path).resolve().parent / step.args["file"]).resolve()
            key = str(target_path)
            if key in self._children:
                continue
            child = flow_validator.load_flow(target_path)
            child.app_id = child.app_id or inherited_app_id
            self._children[key] = child
            self._collect_children(child, child.app_id)

    def _child_of(self, flow: Flow, step: Step) -> Flow:
        target_path = (Path(flow.path).resolve().parent / step.args["file"]).resolve()
        return self._children[str(target_path)]

    def _preflight_flow(self, flow: Flow, values: dict, *,
                        is_root: bool, _seen: set | None = None) -> None:
        seen = _seen if _seen is not None else set()
        # Keyed on (path, available names): the same subflow can be included
        # twice with different env frames, and only checking the first would
        # let the second inclusion's missing variables escape to run time.
        key = (flow.path, tuple(sorted(values)))
        if key in seen:
            return
        seen.add(key)
        if is_root and flow.requires_platforms and \
                self.target.platform not in flow.requires_platforms:
            raise errors.AutonomError(
                errors.FLOW_REQUIREMENTS_UNMET,
                f"flow requires platform {'/'.join(flow.requires_platforms)}, "
                f"target is {self.target.platform}",
                hint="Pick a matching target with --platform/--target.",
                required=flow.requires_platforms, target=self.target.platform,
            )
        steps = (*flow.on_flow_start, *flow.steps, *flow.on_flow_complete) \
            if is_root else tuple(flow.steps)
        for step in steps:
            self._preflight_step(step, flow, values, seen)

    def _preflight_step(self, step: Step, flow: Flow, values: dict,
                        seen: set) -> None:
        if step.command == "runFlow":
            # The step's own env: values and when: clause resolve in the
            # PARENT frame — check them here before descending.
            for name in self._step_var_names(step):
                if name not in values:
                    self._definition(step, f"${{{name}}} is not defined",
                                     code=errors.FLOW_VAR_UNDEFINED)
            child = self._child_of(flow, step)
            child_values = {**child.env,
                            **{k: v for k, v in step.args.get("env", {}).items()},
                            **self.config.env, **self.config.secrets}
            self._preflight_flow(child, child_values, is_root=False, _seen=seen)
            return
        if step.command in ("retry", "group"):
            for sub in step.args["commands"]:
                self._preflight_step(sub, flow, values, seen)
            return
        if step.command in ("launchApp", "stopApp", "clearState") and not flow.app_id:
            self._definition(step, f"{step.command} needs an appId, and neither "
                                   "this flow nor the root flow provides one")
        if self.target.platform == IOS:
            if step.command in ("clearState", "back", "setOrientation"):
                self._definition(step, f"{step.command} is not supported on iOS",
                                 code=errors.UNSUPPORTED_ON_PLATFORM)
            if step.command == "launchApp" and step.args.get("clearState"):
                self._definition(step, "launchApp.clearState is not supported "
                                       "on iOS (no 'pm clear' equivalent)",
                                 code=errors.UNSUPPORTED_ON_PLATFORM)
        for name in self._step_var_names(step):
            if name not in values:
                self._definition(step, f"${{{name}}} is not defined",
                                 code=errors.FLOW_VAR_UNDEFINED)

    def _step_var_names(self, step: Step):
        def from_selector(selector: FlowSelector):
            for text in selector.fields.values():
                if isinstance(text, str):
                    yield from _iter_var_names(text)
            for spec in selector.relations.values():
                for text in spec["fields"].values():
                    if isinstance(text, str):
                        yield from _iter_var_names(text)

        for value in step.args.values():
            if isinstance(value, str):
                yield from _iter_var_names(value)
            elif isinstance(value, FlowSelector):
                yield from from_selector(value)
            elif isinstance(value, dict):
                for text in value.values():
                    if isinstance(text, str):
                        yield from _iter_var_names(text)
            elif isinstance(value, WhenClause):
                for selector in (value.visible, value.not_visible):
                    if selector is not None:
                        yield from from_selector(selector)
                for text in value.env_equals.values():
                    if isinstance(text, str):
                        yield from _iter_var_names(text)

    def _definition(self, step: Step, message: str,
                    code: str = errors.FLOW_COMMAND_INVALID) -> None:
        raise errors.AutonomError(
            code, f"step {step.command} (line {step.line}): {message}",
            line=step.line, column=step.col, command=step.command,
        )

    # -- interpolation --------------------------------------------------------

    def _resolve(self, text: str) -> tuple[str, bool]:
        used_secret = False

        def substitute(match: re.Match) -> str:
            nonlocal used_secret
            name = match.group(1)
            if name not in self.values:
                # pre-flight checks every reference, so reaching this means a
                # frame bug — still fail as a positioned envelope, never KeyError
                raise errors.AutonomError(
                    errors.FLOW_VAR_UNDEFINED, f"${{{name}}} is not defined",
                    hint="Define it in the flow env, --env, or --secret.",
                )
            if name in self.secret_values:
                used_secret = True
            return self.values[name]

        out: list[str] = []
        index = 0
        while index < len(text):
            if text.startswith("$${", index):
                out.append("${")
                index += 3
                continue
            match = _VAR_RE.match(text, index)
            if match:
                out.append(substitute(match))
                index = match.end()
                continue
            out.append(text[index])
            index += 1
        if used_secret:
            self._used_secret_anywhere = True
        return "".join(out), used_secret

    def _resolve_when(self, when: WhenClause) -> WhenClause:
        """Interpolate ${VAR} in a when: clause before evaluating it."""
        visible = self._resolve_selector(when.visible)[0] if when.visible else None
        not_visible = (self._resolve_selector(when.not_visible)[0]
                       if when.not_visible else None)
        env_equals = {key: self._resolve(value)[0]
                      for key, value in when.env_equals.items()}
        return WhenClause(platform=when.platform, visible=visible,
                          not_visible=not_visible, env_equals=env_equals,
                          line=when.line, col=when.col)

    def _resolve_selector(self, selector: FlowSelector) -> tuple[FlowSelector, bool]:
        used = False

        def resolve_fields(fields: dict) -> dict:
            nonlocal used
            out = {}
            for key, value in fields.items():
                if isinstance(value, str):
                    resolved, secret = self._resolve(value)
                    used = used or secret
                    out[key] = resolved
                else:
                    out[key] = value
            return out

        relations = {
            name: {**spec, "fields": resolve_fields(spec["fields"])}
            for name, spec in selector.relations.items()
        }
        clone = FlowSelector(fields=resolve_fields(selector.fields),
                             match=selector.match,
                             index=selector.index, line=selector.line,
                             col=selector.col,
                             source_fields=selector.source_fields,
                             relations=relations,
                             source_relations=selector.source_relations)
        return clone, used

    # -- execution ------------------------------------------------------------

    def _execute_steps(self, steps: list, flow: Flow, values: dict,
                       writer: EventWriter, result: RunResult,
                       evidence: Evidence, hook: str | None = None) -> None:
        previous_values = self.values
        self.values = values
        try:
            for step in steps:
                if step.command == "runFlow":
                    self._execute_runflow(step, flow, writer, result,
                                          evidence, hook)
                    continue
                if step.command == "retry":
                    self._execute_retry(step, flow, writer, result,
                                        evidence, hook)
                    continue
                if step.command == "group":
                    self._execute_group(step, flow, writer, result,
                                        evidence, hook)
                    continue
                outcome = self._execute_step(step, flow, writer, evidence,
                                             hook=hook)
                result.steps.append(outcome)
                if outcome.status == "failed":
                    raise _Stop()
        finally:
            self.values = previous_values

    def _execute_runflow(self, step: Step, flow: Flow, writer: EventWriter,
                         result: RunResult, evidence: Evidence,
                         hook: str | None) -> None:
        child = self._child_of(flow, step)
        self._counter += 1
        outcome = StepOutcome(index=self._counter, command="runFlow",
                              label=step.label, line=step.line,
                              status="passed", flow=flow.path, hook=hook)
        payload = {"step_index": outcome.index, "command": "runFlow",
                   "label": step.label, "file": flow.path, "line": step.line,
                   "child": child.path}
        writer.emit("flow.step.started", payload)

        when = step.args.get("when")
        if when is not None:
            met, reason = flow_conditions.evaluate(
                self._resolve_when(when), self.target.platform, self.values,
                lambda: ui_mod.snapshot(self.target))
            if not met:
                outcome.status = "skipped"
                outcome.skip_reason = reason
                finished = dict(payload)
                finished.update({"status": "skipped", "skip_reason": reason,
                                 "duration_ms": 0, "attempts": 1})
                event = writer.emit("flow.step.finished", finished)
                writer.journal_step(event)
                result.steps.append(outcome)
                return

        child_env_arg = {}
        for key, raw in step.args.get("env", {}).items():
            resolved, _secret = self._resolve(raw)
            child_env_arg[key] = resolved
        child_values = {**child.env, **child_env_arg,
                        **self.config.env, **self.config.secrets}

        started = self.clock()
        before = len(result.steps)
        try:
            self._execute_steps(child.steps, child, child_values,
                                writer, result, evidence, hook=hook)
        except _Stop:
            failed = result.steps[-1] if len(result.steps) > before else None
            outcome.status = "failed"
            if failed is not None:
                outcome.error_code = failed.error_code
                outcome.failure_class = failed.failure_class
                outcome.error = failed.error
            self._finish_runflow(outcome, payload, started, writer, result)
            raise
        self._finish_runflow(outcome, payload, started, writer, result)

    def _finish_runflow(self, outcome: StepOutcome, payload: dict,
                        started: float, writer: EventWriter,
                        result: RunResult) -> None:
        outcome.duration_ms = int((self.clock() - started) * 1000)
        finished = dict(payload)
        finished.update({"status": outcome.status,
                         "duration_ms": outcome.duration_ms, "attempts": 1})
        if outcome.error_code:
            finished["error_code"] = outcome.error_code
            finished["failure_class"] = outcome.failure_class
        event = writer.emit("flow.step.finished", finished)
        writer.journal_step(event)
        result.steps.append(outcome)

    def _execute_group(self, step: Step, flow: Flow, writer: EventWriter,
                       result: RunResult, evidence: Evidence,
                       hook: str | None) -> None:
        self._counter += 1
        writer.emit("flow.step.started", {
            "step_index": self._counter, "command": "group",
            "label": step.label, "file": flow.path, "line": step.line,
            "steps": len(step.args["commands"]),
        })
        index = self._counter
        started = self.clock()
        status = "passed"
        try:
            self._execute_steps(step.args["commands"], flow, self.values,
                                writer, result, evidence, hook=hook)
        except (_Stop, errors.AutonomError):
            status = "failed"
            raise
        finally:
            writer.emit("flow.step.finished", {
                "step_index": index, "command": "group", "label": step.label,
                "file": flow.path, "line": step.line, "status": status,
                "duration_ms": int((self.clock() - started) * 1000),
                "attempts": 1,
            })

    def _execute_retry(self, step: Step, flow: Flow, writer: EventWriter,
                       result: RunResult, evidence: Evidence,
                       hook: str | None) -> None:
        """Explicit, bounded retry of a small non-composed block (§7.11).

        Only test-failure-class outcomes are retried (and only those in
        onlyOn, when given); infrastructure and definition errors always
        abort. Every attempt's steps land in the journal and events.
        """
        max_attempts = step.args["maxAttempts"]
        only_on = step.args.get("onlyOn") or []
        self._counter += 1
        block_index = self._counter
        writer.emit("flow.step.started", {
            "step_index": block_index, "command": "retry", "label": step.label,
            "file": flow.path, "line": step.line,
            "max_attempts": max_attempts,
        })
        if step.args.get("allowMutations"):
            writer.emit("flow.step.finished", {
                "step_index": block_index, "command": "retry",
                "label": step.label, "file": flow.path, "line": step.line,
                "status": "warning", "duration_ms": 0, "attempts": 0,
                "warning": "allowMutations: mutating commands inside this "
                           "block may act on the app more than once",
            })
        started = self.clock()
        for attempt in range(1, max_attempts + 1):
            before = len(result.steps)
            previous_attempt = self._retry_attempt
            self._retry_attempt = attempt
            try:
                self._execute_steps(step.args["commands"], flow, self.values,
                                    writer, result, evidence, hook=hook)
                self._emit_retry_finished(step, flow, writer, block_index,
                                          started, "passed", attempt)
                return
            except _Stop:
                failed = next((s for s in result.steps[before:]
                               if s.status == "failed"), None)
                retryable = (failed is not None
                             and (not only_on or failed.error_code in only_on)
                             and attempt < max_attempts)
                if not retryable:
                    self._emit_retry_finished(step, flow, writer, block_index,
                                              started, "failed", attempt)
                    raise
            finally:
                self._retry_attempt = previous_attempt

    def _emit_retry_finished(self, step: Step, flow: Flow, writer: EventWriter,
                             block_index: int, started: float, status: str,
                             attempts: int) -> None:
        writer.emit("flow.step.finished", {
            "step_index": block_index, "command": "retry", "label": step.label,
            "file": flow.path, "line": step.line, "status": status,
            "duration_ms": int((self.clock() - started) * 1000),
            "attempts": attempts,
        })

    def _run_complete_hooks(self, flow: Flow, values: dict,
                            writer: EventWriter, result: RunResult,
                            evidence: Evidence) -> None:
        """Isolated cleanup: every failure is recorded, none masks the run.

        Each hook step goes through the same machinery as regular steps
        (so a hook runFlow composes, its env: frame applies, and its when:
        clause is honored), but failures are contained per step — the run's
        primary status never changes here.
        """
        for step in flow.on_flow_complete:
            before = len(result.steps)
            hook_payload = {"command": step.command, "label": step.label,
                            "file": flow.path, "line": step.line,
                            "hook": "onFlowComplete", "status": "passed"}
            try:
                self._execute_steps([step], flow, values, writer, result,
                                    evidence, hook="onFlowComplete")
            except (_Stop, errors.AutonomError) as exc:
                failed = next((s for s in result.steps[before:]
                               if s.status == "failed"), None)
                code = (failed.error_code if failed is not None
                        else getattr(exc, "code", errors.BACKEND_FAILED))
                message = (failed.error if failed is not None else str(exc))
                hook_payload.update({
                    "status": "failed", "error_code": code,
                    "failure_class": failure_class(code), "error": message,
                })
                result.hook_failures.append({
                    "command": step.command, "line": step.line,
                    "error_code": code, "error": message,
                })
            writer.emit("flow.hook.finished", hook_payload)

    def _execute_step(self, step: Step, flow: Flow, writer: EventWriter,
                      evidence: Evidence, hook: str | None = None) -> StepOutcome:
        self._counter += 1
        index = self._counter
        outcome = StepOutcome(index=index, command=step.command,
                              label=step.label, line=step.line,
                              status="passed", flow=flow.path, hook=hook)
        payload: dict[str, Any] = {
            "step_index": index, "command": step.command, "label": step.label,
            "file": flow.path, "line": step.line,
        }
        if hook:
            payload["hook"] = hook
        if self._retry_attempt is not None:
            payload["retry_attempt"] = self._retry_attempt
        if step.selector is not None:
            payload["selector"] = flow_selectors.describe(step.selector)
        writer.emit("flow.step.started", payload)

        self._auto_evidence(evidence, step, "before", index, writer)
        sensitive = bool(step.args.get("sensitive"))
        started = self.clock()
        self._last_nodes = None
        attempts = [0]
        try:
            secret_used = self._dispatch(step, flow, attempts)
            sensitive = sensitive or secret_used
        except errors.AutonomError as exc:
            outcome.error_code = exc.code
            outcome.failure_class = failure_class(exc.code)
            outcome.error = exc.message
            if step.args.get("optional") and outcome.failure_class == TEST_FAILURE:
                outcome.status = "skipped"
                outcome.skip_reason = step.args.get("reason")
            else:
                outcome.status = "failed"
                if evidence.mode != "minimal":
                    self._capture_failure_evidence(index, writer)
        else:
            self._auto_evidence(evidence, step, "after", index, writer)
        outcome.duration_ms = int((self.clock() - started) * 1000)
        outcome.attempts = max(attempts[0], 1)

        finished = dict(payload)
        finished.update({
            "status": outcome.status,
            "duration_ms": outcome.duration_ms,
            "attempts": outcome.attempts,
        })
        if outcome.error_code:
            finished["error_code"] = outcome.error_code
            finished["failure_class"] = outcome.failure_class
        if outcome.skip_reason:
            finished["skip_reason"] = outcome.skip_reason
        if self._last_nodes:
            # nearly free: the snapshot is already in memory, and the Atlas
            # ingests these fingerprints into the observed graph
            finished["screen"] = atlas_fingerprint.fingerprint(self._last_nodes)
        event = writer.emit("flow.step.finished", finished, sensitive=sensitive)
        writer.journal_step(event)

        if outcome.status == "failed" and outcome.failure_class != TEST_FAILURE:
            # definition/infrastructure failures abort the whole verb: re-raise
            # with step context so the stderr envelope names the step.
            raise errors.AutonomError(
                outcome.error_code, outcome.error or outcome.command,
                step_index=index, command=step.command, line=step.line,
                failure_class=outcome.failure_class, run_id=writer.run_id,
            )
        return outcome

    def _auto_evidence(self, evidence: Evidence, step: Step, phase: str,
                       index: int, writer: EventWriter) -> None:
        if evidence.mode == "minimal" or "screenshot" not in evidence.collect:
            return
        wanted = (
            (phase == "after" and evidence.mode == "always")
            or (phase == "before" and evidence.before_mutation
                and step.spec.mutating and evidence.mode != "on-failure")
            or (phase == "after" and evidence.after_assertion
                and step.spec.assertion and evidence.mode != "on-failure")
        )
        if not wanted:
            return
        try:
            detail = screenshot_mod.capture_evidence(
                self.target, self.session, label=f"step-{index}-{phase}",
                task=writer.run_id)
            writer.emit("flow.evidence.captured",
                        {"step_index": index, "phase": phase,
                         "screenshot": detail.get("path", "")})
        except Exception:  # noqa: BLE001 — evidence is best-effort
            pass

    def _dispatch(self, step: Step, flow: Flow, attempts: list) -> bool:
        command = step.command
        target = self.target
        if command == "launchApp":
            if step.args.get("clearState"):
                session_mod.clear_data(target.tool, target.target_id, flow.app_id)
            if target.platform == IOS:
                ios_simctl.launch(target.tool, target.target_id, flow.app_id,
                                  env=self._ios_launch_env())
            else:
                session_mod.launch_app(target.tool, target.target_id, flow.app_id)
            return False
        if command == "stopApp":
            if target.platform == IOS:
                ios_simctl.terminate(target.tool, target.target_id, flow.app_id)
            else:
                session_mod.force_stop(target.tool, target.target_id, flow.app_id)
            return False
        if command == "clearState":
            session_mod.clear_data(target.tool, target.target_id, flow.app_id)
            return False
        if command == "openLink":
            url, secret = self._resolve(step.args["url"])
            device_state.open_url(target, url)
            return secret
        if command == "tapOn":
            selector, secret = self._resolve_selector(step.selector)
            node = self._poll_for_one(selector, step, attempts)
            x, y = ui_mod.center_of(node)
            self._tap(x, y)
            return secret
        if command == "longPressOn":
            selector, secret = self._resolve_selector(step.selector)
            node = self._poll_for_one(selector, step, attempts)
            x, y = ui_mod.center_of(node)
            ui_mod.long_press(target, x, y, step.args.get("durationMs", 600),
                              screen=self._screen_size())
            return secret
        if command == "doubleTapOn":
            selector, secret = self._resolve_selector(step.selector)
            node = self._poll_for_one(selector, step, attempts)
            x, y = ui_mod.center_of(node)
            ui_mod.double_tap(target, x, y, screen=self._screen_size())
            return secret
        if command == "setOrientation":
            device_state.set_orientation(target, step.args["orientation"])
            self._screen_fetched = False  # dimensions just swapped
            return False
        if command == "inputText":
            value, secret = self._resolve(step.args["value"])
            ui_mod.type_text(target, value)
            return secret
        if command == "eraseText":
            key = "KEYCODE_DEL" if target.platform == ANDROID else "42"
            for _ in range(step.args.get("chars", 50)):
                ui_mod.press_key(target, key)
            return False
        if command == "pressKey":
            key, secret = self._resolve(step.args["key"])
            ui_mod.press_key(target, key)
            return secret
        if command == "back":
            ui_mod.press_key(target, "KEYCODE_BACK")
            return False
        if command == "swipe":
            self._swipe(step.args["direction"], step.args.get("durationMs", 300))
            return False
        if command == "scrollUntilVisible":
            return self._scroll_until_visible(step, attempts)
        if command in ("assertVisible", "assertNotVisible"):
            selector, secret = self._resolve_selector(step.selector)
            want_visible = command == "assertVisible"
            self._poll_condition(selector, step.args.get("timeoutMs"),
                                 attempts,
                                 lambda m: bool(m) == want_visible,
                                 "visible" if want_visible else "gone")
            return secret
        if command in ("assertEnabled", "assertChecked"):
            selector, secret = self._resolve_selector(step.selector)
            state_field = "enabled" if command == "assertEnabled" else "checked"
            self._poll_condition(
                selector, step.args.get("timeoutMs"), attempts,
                lambda m: bool(m) and all(node.get(state_field) for node in m),
                state_field)
            return secret
        if command == "waitUntil":
            want_visible = "visible" in step.args
            raw = step.args["visible" if want_visible else "notVisible"]
            selector, secret = self._resolve_selector(raw)
            self._poll_condition(selector, step.args["timeoutMs"], attempts,
                                 lambda m: bool(m) == want_visible,
                                 "visible" if want_visible else "gone")
            return secret
        if command == "setLocation":
            device_state.set_location(
                target, f"{step.args['latitude']},{step.args['longitude']}")
            return False
        if command == "setPermissions":
            device_state.permissions(target, step.args["action"],
                                     step.args["service"],
                                     step.args.get("appId") or flow.app_id)
            return False
        if command == "addMedia":
            path, secret = self._resolve(step.args["path"])
            device_state.add_media(target, Path(path))
            return secret
        if command == "takeScreenshot":
            label = step.args.get("label") or "flow"
            screenshot_mod.capture_evidence(target, self.session, label=label,
                                             task=self._writer_run_id)
            return False
        if command == "checkpoint":
            return False  # the step event itself is the checkpoint record
        if command == "note":
            text, secret = self._resolve(step.args["text"])
            if not secret:  # a secret-bearing note must not reach the journal
                journal_mod.note(self.session, text, author="flow")
            return secret
        raise AssertionError(f"unhandled command {command}")  # pragma: no cover

    # -- polling --------------------------------------------------------------

    def _matches(self, selector: FlowSelector) -> list:
        mode, case_sensitive = MATCH_MODES[selector.match]
        nodes = ui_mod.snapshot(self.target)
        self._last_nodes = nodes
        matches = selector_engine.select(
            nodes, dict(selector.fields),
            mode=mode, case_sensitive=case_sensitive, all_matches=True,
        )
        if selector.index is not None:
            # An assertion with index asserts about THAT occurrence; a missing
            # occurrence is simply "not there", not an error.
            try:
                matches = [matches[selector.index]]
            except IndexError:
                matches = []
        return matches

    def _poll_for_one(self, selector: FlowSelector, step: Step, attempts: list):
        """Poll while ZERO nodes match; the moment any match, select once."""
        timeout_ms = step.args.get("timeoutMs") or self.config.default_timeout_ms
        deadline = self.clock() + timeout_ms / 1000
        while True:
            attempts[0] += 1
            nodes = ui_mod.snapshot(self.target)
            self._last_nodes = nodes
            candidates = flow_selectors.select(nodes, selector)
            if candidates:
                return candidates[0]  # >1 already raised ambiguous_selector
            if self.clock() >= deadline:
                raise errors.AutonomError(
                    errors.FLOW_ASSERTION_TIMEOUT,
                    f"no node matched {flow_selectors.describe(selector)} "
                    f"within {timeout_ms} ms",
                    hint="The element never appeared. If it renders late, "
                         "raise timeoutMs; if the selector is wrong, fix it.",
                    timeout_ms=timeout_ms, attempts=attempts[0],
                )
            self.sleep(self.config.interval_ms / 1000)

    def _poll_condition(self, selector: FlowSelector, timeout_ms: int | None,
                        attempts: list, satisfied: Callable[[list], bool],
                        expectation: str) -> None:
        timeout_ms = timeout_ms or self.config.default_timeout_ms
        deadline = self.clock() + timeout_ms / 1000
        while True:
            attempts[0] += 1
            if satisfied(self._matches(selector)):
                return
            if self.clock() >= deadline:
                raise errors.AutonomError(
                    errors.FLOW_ASSERTION_TIMEOUT,
                    f"{flow_selectors.describe(selector)} was not {expectation} "
                    f"within {timeout_ms} ms",
                    timeout_ms=timeout_ms, attempts=attempts[0],
                )
            self.sleep(self.config.interval_ms / 1000)

    def _scroll_until_visible(self, step: Step, attempts: list) -> bool:
        selector, secret = self._resolve_selector(step.selector)
        max_swipes = step.args.get("maxSwipes", 5)
        direction = step.args.get("direction", "down")
        for swipes_used in range(max_swipes + 1):
            attempts[0] += 1
            if self._matches(selector):
                return secret
            if swipes_used == max_swipes:
                break
            # scrolling down moves content up: swipe opposite the direction
            gesture = {"down": "up", "up": "down", "left": "right",
                       "right": "left"}[direction]
            self._swipe(gesture, 300)
        raise errors.AutonomError(
            errors.FLOW_ASSERTION_TIMEOUT,
            f"{flow_selectors.describe(selector)} not visible after "
            f"{max_swipes} {direction} swipes",
            attempts=attempts[0], max_swipes=max_swipes,
        )

    # -- helpers --------------------------------------------------------------

    def _screen_size(self) -> tuple[int, int] | None:
        if not self._screen_fetched:
            self._screen = ui_mod.screen_size(self.target)
            self._screen_fetched = True
        return self._screen

    def _refresh_screen(self) -> tuple[int, int] | None:
        self._screen = ui_mod.screen_size(self.target)
        self._screen_fetched = True
        return self._screen

    def _tap(self, x: int, y: int) -> None:
        """Tap with the cached screen size; on a coordinate guard refusal,
        refresh the cache once (the app may have rotated mid-flow) and retry
        the guard. The guard raises *before* dispatch, so this can never
        double-tap."""
        try:
            ui_mod.tap(self.target, x, y, screen=self._screen_size())
        except errors.AutonomError as exc:
            if exc.code != errors.COORDINATE_SPACE_MISMATCH:
                raise
            ui_mod.tap(self.target, x, y, screen=self._refresh_screen())

    def _swipe(self, direction: str, duration_ms: int) -> None:
        def geometry(screen):
            width, height = screen
            boxes = {
                "up": ((width // 2, int(height * 0.7)), (width // 2, int(height * 0.3))),
                "down": ((width // 2, int(height * 0.3)), (width // 2, int(height * 0.7))),
                "left": ((int(width * 0.7), height // 2), (int(width * 0.3), height // 2)),
                "right": ((int(width * 0.3), height // 2), (int(width * 0.7), height // 2)),
            }
            return boxes[direction]

        screen = self._screen_size()
        if not screen:
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                "cannot determine the screen size for a directional swipe",
                hint="The backend did not report a screen size.",
            )
        try:
            (x1, y1), (x2, y2) = geometry(screen)
            ui_mod.swipe(self.target, x1, y1, x2, y2, duration_ms / 1000,
                         screen=screen)
        except errors.AutonomError as exc:
            if exc.code != errors.COORDINATE_SPACE_MISMATCH:
                raise
            screen = self._refresh_screen()  # rotated mid-flow; guard fired pre-dispatch
            if not screen:
                raise
            (x1, y1), (x2, y2) = geometry(screen)
            ui_mod.swipe(self.target, x1, y1, x2, y2, duration_ms / 1000,
                         screen=screen)

    def _ios_launch_env(self) -> dict:
        from ..network import device_proxy_ios
        env: dict = {}
        for key, value in device_proxy_ios.launch_environment(self.session).items():
            env.setdefault(key, value)
        return env

    def _capture_failure_evidence(self, index: int, writer: EventWriter) -> None:
        captured: dict[str, str] = {}
        try:
            detail = screenshot_mod.capture_evidence(
                self.target, self.session, label=f"failure-step-{index}",
                task=writer.run_id)
            captured["screenshot"] = detail.get("path", "")
        except Exception:  # noqa: BLE001 — evidence is best-effort on a failing run
            pass
        try:
            nodes, _warnings = ui_mod.tree(self.target)
            path = writer.run_dir() / f"failure-step-{index}-hierarchy.json"
            path.write_text(json.dumps({"nodes": nodes}, indent=2),
                            encoding="utf-8")
            captured["hierarchy"] = str(path)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .. import logs as logs_mod
            entries, _warnings = logs_mod.tail(
                self.target, package=self.session.get("app_id"),
                since_seconds=120, max_lines=100)
            if entries:
                path = writer.run_dir() / f"failure-step-{index}-logs.txt"
                path.write_text(
                    "\n".join(e.get("line", "") for e in entries) + "\n",
                    encoding="utf-8")
                captured["logs"] = str(path)
        except Exception:  # noqa: BLE001
            pass
        if captured:
            writer.emit("flow.evidence.captured",
                        {"step_index": index, **captured})
