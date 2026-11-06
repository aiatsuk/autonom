"""The only way the server runs the Autonom CLI (contract 2.6).

``Runner.run(key, params, operation_id=..., claim=..., timeout_s=...)`` builds
the argv from the allowlist below (an unknown key or a missing parameter is a
programming error, ``ValueError``), runs ``[cli_python, autonom.py, *argv]`` in
its own process group with a bounded timeout, and returns a ``CliResult``.
A mutating key carries ``--operation-id OP`` directly after the verb path, so
it always stands before a ``--`` terminator. Nothing here ever retries.

``REJECTED_CLI_CODES`` are the CLI codes raised before any device input;
``UNKNOWN_CLI_CODES`` are all the others of ``scripts/autonom_lib/errors.py``
(the plugin never imports that file; a test checks both sets against it).
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence

from mcp_common import PathScrubber, ToolError, redact
from mcp_env import Config, Resolver, child_env, ensure_private_dir, path_scrubber

STDOUT_CAP = 8 * 1024 * 1024
STDERR_CAP = 256 * 1024
LOG_TAIL = 4096
FIELD_CAP = 500
READER_GRACE_S = 2.0
READER_POLL_S = 0.05
READER_STOP_S = 1.0
READER_DRAIN_READS = 256

REJECTED_CLI_CODES = frozenset({
    "usage_error", "conflicting_target_flags", "ambiguous_target", "no_target", "unknown_platform",
    "no_active_session", "session_not_found", "session_stopped", "session_target_mismatch",
    "session_already_active", "control_held", "control_generation_stale", "control_lease_expired",
    "control_not_held", "ambiguous_selector", "no_matching_node", "selector_index_out_of_range",
    "selector_required", "unsupported_key_for_platform", "unsupported_on_platform",
    "unsupported_capability", "adb_not_found", "simctl_not_found", "tool_missing", "canvas_not_found",
    "canvas_ambiguous", "workspace_in_use", "flow_parse_error", "flow_schema_unsupported",
    "flow_header_invalid", "flow_unknown_command", "flow_command_invalid", "flow_selector_invalid",
    "flow_var_undefined", "flow_secret_undefined", "flow_file_not_found", "flow_path_escapes_workspace",
    "flow_requirements_unmet", "host_identity_invalid",
    # The pre-test guard refuses these before any input is sent.
    "budget_exhausted", "forbidden_action", "app_not_allowed", "pretest_damaged",
    # Android helper and typing refusals: nothing was sent or typed.
    "android_helper_unavailable", "text_not_typable", "fill_target_mismatch",
})

UNKNOWN_CLI_CODES = frozenset({
    # iOS device daemon: an input sent once and not confirmed, or a call whose
    # reply did not come back; never repeated.
    "input_unconfirmed", "ios_input_blocked", "ios_display_not_lit", "companion_too_old",
    "daemon_unreachable",
    "idb_required", "idb_companion_unavailable", "mitmdump_required", "backend_failed",
    "ios_hid_framework_missing", "install_path_not_found", "app_not_installed", "ios_boot_failed",
    "ios_clear_requires_install_path", "emulator_not_found", "avd_not_found", "avd_required",
    "emulator_only", "boot_timeout", "coordinate_space_mismatch", "stale_ref", "element_offscreen",
    "invalid_url", "invalid_coordinates", "unknown_privacy_service", "path_outside_container",
    "recording_already_active", "output_not_writable", "invalid_simulator_action",
    "clipboard_unavailable", "port_unavailable", "proxy_not_running", "flow_not_found",
    "mock_not_found", "body_file_not_found", "bodies_not_captured", "unsafe_artifacts_permissions",
    "consent_required", "consent_declined", "physical_device_attach_unsupported", "stream_not_found",
    "path_forbidden", "app_not_running", "preset_unavailable", "trace_failed", "signal_unstable",
    "flow_optional_assertion_forbidden", "flow_var_conflict", "flow_copy_empty", "flow_repeat_invalid",
    "flow_cycle_detected", "flow_assertion_timeout", "flow_check_failed", "flow_no_flows_found",
    "flow_replay_step_not_reached", "unsupported_flow_command", "flow_source_changed", "run_not_found",
    "step_not_found", "comments_would_be_lost", "report_model_invalid", "report_bundle_finalized",
    "report_integrity_failed", "report_merge_conflict", "gate_failed", "teach_range_invalid",
    "teach_approval_blocked", "app_skill_invalid", "replay_manifest_invalid", "ci_destination_failed",
    "app_not_debuggable", "flow_no_focused_field", "invalid_value", "simulator_must_be_shutdown",
    "simulator_data_not_found", "display_geometry_unavailable", "ui_action_uncertain",
    "app_id_required", "ui_bridge_in_use", "xcuitest_unavailable", "xcuitest_build_failed",
    "xcuitest_start_failed", "xcuitest_timeout", "xcuitest_failed", "stale_ui_element",
    "no_focused_field", "recording_not_active", "capture_not_found", "capture_too_large",
    "install_not_configured", "install_path_not_allowed", "install_failed", "no_active_pretest",
    "pretest_already_active", "pretest_finalized", "check_already_open", "no_open_check",
    "unknown_check", "invalid_verdict", "unknown_finding", "invalid_severity", "evidence_ref_unknown",
    "review_needs_finding", "not_automatic", "profile_invalid", "not_a_pretest_pack",
    "draft_unavailable", "race_needs_count", "check_not_ended", "git_missing",
    # Android screen never idle, and the exit-130 KeyboardInterrupt answer (DEC-028).
    "ui_not_idle", "interrupted",
})

CONTROL_CODES = frozenset({"control_held", "control_generation_stale", "control_lease_expired",
                           "control_not_held"})


def classify(code: Optional[str]) -> str:
    """``rejected`` for a code raised before any device input, else ``unknown``."""
    return "rejected" if code in REJECTED_CLI_CODES else "unknown"


# ---------------------------------------------------------------- allowlist

def _need(params: Mapping[str, Any], key: str) -> Any:
    if key not in params or params[key] is None:
        raise ValueError("parameter %r is required" % key)
    return params[key]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("parameter %r must be a non-empty string" % name)
    return value


def _int(value: Any, name: str) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("parameter %r must be an integer" % name)
    return str(value)


def _opt(flag: str, value: str) -> List[str]:
    """``--flag value``; a value that looks like an option uses ``--flag=value``."""
    return ["%s=%s" % (flag, value)] if value.startswith("-") else [flag, value]


def _sid(params: Mapping[str, Any]) -> List[str]:
    return ["--session-id", _text(_need(params, "session_id"), "session_id")]


def _target(params: Mapping[str, Any]) -> List[str]:
    platform = _need(params, "platform")
    if platform not in ("android", "ios"):
        raise ValueError("platform must be android or ios")
    return ["--platform", platform] + _opt("--target", _text(_need(params, "target_id"), "target_id"))


def _workspace(params: Mapping[str, Any]) -> List[str]:
    return _opt("--workspace", _text(_need(params, "workspace"), "workspace"))


def _b_none(_params: Mapping[str, Any]) -> List[str]:
    return []


def _b_sessions(params: Mapping[str, Any]) -> List[str]:
    return ["--all"] if params.get("all") else []


def _b_session_start(params: Mapping[str, Any]) -> List[str]:
    out = _target(params) + ["--alongside"]
    if params.get("label") is not None:
        out += _opt("--label", _text(params["label"], "label"))
    return out


def _b_screenshot(params: Mapping[str, Any]) -> List[str]:
    # `--no-agent`: the full-size PNG get_screen inlines and get_artifact serves,
    # even in an agent-profile session, whose default is the Android helper's
    # small JPEG (a no-op on iOS)
    out = _sid(params) + ["--no-agent"]
    if params.get("label") is not None:
        out += _opt("--label", _text(params["label"], "label"))
    return out


def _b_ui_tree(params: Mapping[str, Any]) -> List[str]:
    # --format json pins the reply shape: an agent-profile session defaults to outline.
    out = _sid(params) + ["--format", "json",
                          "--max-nodes", _int(_need(params, "max_nodes"), "max_nodes"),
                          "--no-accessibility-recovery"]
    if params.get("interactable"):
        out.append("--interactable")
    return out


SELECTOR_FLAGS = (("text", "--text"), ("desc", "--desc"), ("resource_id", "--resource-id"),
                  ("class_name", "--class-name"), ("role", "--role"))


def _no_observe(params: Mapping[str, Any]) -> List[str]:
    """``--session-id S --no-observe``: an agent-profile session observes by default."""
    return _sid(params) + ["--no-observe"]


def _b_ui_tap(params: Mapping[str, Any]) -> List[str]:
    out = _no_observe(params)
    selector = params.get("selector") or {}
    used = False
    for name, flag in SELECTOR_FLAGS:
        if selector.get(name) is not None:
            out += _opt(flag, _text(selector[name], name))
            used = True
    if not used:
        raise ValueError("ui_tap needs at least one selector field")
    if params.get("mode") is not None:
        if params["mode"] not in ("exact", "contains", "regex"):
            raise ValueError("mode must be exact, contains or regex")
        out += ["--mode", params["mode"]]
    if params.get("index") is not None:
        out += ["--index", _int(params["index"], "index")]
    return out


def _b_ui_type(params: Mapping[str, Any]) -> List[str]:
    out = _no_observe(params)
    if params.get("sensitive"):
        out.append("--sensitive")
    return out + ["--", _text(_need(params, "text"), "text")]


def _b_ui_key(params: Mapping[str, Any]) -> List[str]:
    key = _text(_need(params, "key"), "key")
    if key.startswith("-"):
        raise ValueError("key must not start with '-'")
    return _no_observe(params) + [key]


def _b_control_acquire(params: Mapping[str, Any]) -> List[str]:
    return (_sid(params) + _opt("--holder", _text(_need(params, "holder"), "holder"))
            + ["--kind", "agent"] + _opt("--label", _text(_need(params, "label"), "label"))
            + ["--ttl", _int(_need(params, "ttl"), "ttl")])


def _b_control_release(params: Mapping[str, Any]) -> List[str]:
    return (_sid(params) + _opt("--holder", _text(_need(params, "holder"), "holder"))
            + ["--generation", _int(_need(params, "generation"), "generation")])


def _b_control_renew(params: Mapping[str, Any]) -> List[str]:
    return _b_control_release(params) + ["--ttl", _int(_need(params, "ttl"), "ttl")]


def _b_journal_find(params: Mapping[str, Any]) -> List[str]:
    return _sid(params) + _opt("--grep", _text(_need(params, "grep"), "grep")) + ["--max", "5"]


def _b_flow_run(params: Mapping[str, Any]) -> List[str]:
    path = _text(_need(params, "path"), "path")
    if not os.path.isabs(path):
        raise ValueError("flow path must be absolute")
    out = [path] + _sid(params) + ["--events"]
    for key, value in sorted((params.get("env") or {}).items()):
        out += ["--env", "%s=%s" % (_text(key, "env key"), value)]
    return out


def _b_canvas_serve(params: Mapping[str, Any]) -> List[str]:
    return _workspace(params) + ["--ephemeral", "--bootable-only", "--port", "0", "--control-idle",
                                 _int(_need(params, "control_idle"), "control_idle")]


def _b_canvas_device(params: Mapping[str, Any]) -> List[str]:
    return _workspace(params) + _target(params)


class Verb(NamedTuple):
    key: str
    path: tuple
    timeout_s: Optional[float]
    kind: str  # read | mutating | lifecycle | job | long
    build: Callable[[Mapping[str, Any]], List[str]]

    @property
    def mutating(self) -> bool:
        return self.kind in ("mutating", "job")


ALLOWLIST: Dict[str, Verb] = {verb.key: verb for verb in (
    Verb("host_info", ("host", "info"), 15, "read", _b_none),
    Verb("devices", ("devices", "list"), 30, "read", _b_none),
    Verb("sessions", ("session", "list"), 15, "read", _b_sessions),
    Verb("session_show", ("session", "show"), 15, "read", _sid),
    Verb("session_start", ("session", "start"), 120, "mutating", _b_session_start),
    Verb("session_stop", ("session", "stop"), 60, "mutating", _sid),
    Verb("screenshot", ("screenshot",), 30, "read", _b_screenshot),
    Verb("ui_tree", ("ui", "tree"), 45, "read", _b_ui_tree),
    Verb("ui_tap", ("ui", "tap"), 30, "mutating", _b_ui_tap),
    Verb("ui_type", ("ui", "type"), 30, "mutating", _b_ui_type),
    Verb("ui_key", ("ui", "key"), 30, "mutating", _b_ui_key),
    Verb("control_status", ("control", "status"), 15, "read", _sid),
    Verb("control_acquire", ("control", "acquire"), 15, "mutating", _b_control_acquire),
    Verb("control_release", ("control", "release"), 15, "mutating", _b_control_release),
    Verb("control_renew", ("control", "renew"), 15, "lifecycle", _b_control_renew),
    Verb("journal_find", ("journal",), 15, "read", _b_journal_find),
    Verb("flow_run", ("flow", "run"), None, "job", _b_flow_run),
    Verb("canvas_serve", ("canvas", "serve"), None, "long", _b_canvas_serve),
    Verb("canvas_attach", ("canvas", "attach"), 150, "lifecycle", _b_canvas_device),
    Verb("canvas_detach", ("canvas", "detach"), 30, "lifecycle", _b_canvas_device),
    Verb("canvas_stop", ("canvas", "stop"), 30, "lifecycle", _workspace),
)}

QUIET_KEYS = frozenset({"canvas_serve", "canvas_attach", "canvas_detach", "canvas_stop"})


def build_argv(key: str, params: Optional[Mapping[str, Any]] = None,
               operation_id: Optional[str] = None) -> List[str]:
    """The argv after the CLI path for ``key`` (contract 2.6 table)."""
    verb = ALLOWLIST[key]
    params = params or {}
    if verb.mutating:
        if not operation_id:
            raise ValueError("mutating key %s needs an operation id" % key)
        head = list(verb.path) + ["--operation-id", operation_id]
    else:
        if operation_id is not None:
            raise ValueError("key %s takes no operation id" % key)
        head = list(verb.path)
    return head + verb.build(params)


# ---------------------------------------------------------------- results

def parse_cli_json(text: str) -> Optional[Dict[str, Any]]:
    """The whole text as one JSON object, else the last line that parses as one."""
    trimmed = (text or "").strip()
    if not trimmed:
        return None
    try:
        value = json.loads(trimmed)
        if isinstance(value, dict):
            return value
    except (ValueError, RecursionError):
        pass
    for line in reversed(trimmed.splitlines()):
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _cap(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:FIELD_CAP]


class CliResult:
    """What one CLI invocation produced.

    ``status`` is ``ok``, ``cli_error`` (the CLI answered an error object),
    ``timeout``, ``malformed``, ``interrupted`` (exit 130) or ``unavailable``
    (nothing was spawned).
    """

    def __init__(self, key: str, status: str, *, exit_code: Optional[int] = None,
                 data: Optional[Dict[str, Any]] = None, error: Optional[Dict[str, Any]] = None,
                 duration_s: float = 0.0, pid: Optional[int] = None,
                 reason: Optional[str] = None) -> None:
        self.key = key
        self.status = status
        self.exit_code = exit_code
        self.data = data
        self.error = error or {}
        self.duration_s = duration_s
        self.pid = pid
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def cli_error_code(self) -> Optional[str]:
        code = self.error.get("error_code")
        return code[:80] if isinstance(code, str) and code else None

    @property
    def error_code(self) -> Optional[str]:
        """The adapter's code for this result (None when ok)."""
        if self.status == "ok":
            return None
        if self.status == "cli_error":
            return self.cli_error_code or "cli_failed"
        return {"timeout": "cli_timeout", "malformed": "cli_output_invalid",
                "interrupted": "cli_interrupted", "unavailable": "cli_unavailable"}[self.status]

    @property
    def rejected(self) -> bool:
        """True when the invocation certainly had no device or session effect."""
        if self.status == "unavailable":
            return True
        return self.status == "cli_error" and classify(self.cli_error_code) == "rejected"

    def to_error(self, hint: Optional[str] = None, **extras: Any) -> ToolError:
        code = self.error_code or "cli_failed"
        if self.status == "cli_error":
            message = _cap(self.error.get("error")) or "The Autonom CLI failed"
        elif self.status == "timeout":
            message = "The Autonom CLI did not answer in time (%s)" % self.key
        elif self.status == "malformed":
            message = "The Autonom CLI printed no valid JSON answer (%s)" % self.key
        elif self.status == "interrupted":
            message = "The Autonom CLI was interrupted (%s)" % self.key
        else:
            message = self.reason or "The Autonom CLI could not be started"
        fields: Dict[str, Any] = {}
        if self.cli_error_code:
            fields["cli_error_code"] = self.cli_error_code
            fields["cli_error"] = _cap(self.error.get("error")) or ""
            if isinstance(self.error.get("hint"), str):
                fields["cli_hint"] = _cap(self.error["hint"])
            if isinstance(self.error.get("capability"), str):
                fields["capability"] = self.error["capability"][:80]
        fields.update(extras)
        if hint is None and self.status == "cli_error":
            hint = _cap(self.error.get("hint"))
        return ToolError(code, message, hint, rejected=self.rejected, **fields)


# ---------------------------------------------------------------- runner

def _reader(stream: Any, cap: int, sink: Dict[str, Any], stop: threading.Event) -> None:
    """Read ``stream`` until EOF or until ``stop`` is set, keeping at most ``cap`` bytes.

    The reader owns the stream and closes it. It reads the raw descriptor with a
    short poll, so a helper process that keeps the pipe open after the CLI
    exited cannot hold the call: once ``stop`` is set the reader drains what is
    already buffered in the pipe and returns.
    """
    chunks: List[bytes] = []
    size = 0
    selector = None
    try:
        fd = stream.fileno()
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        drained = 0
        while True:
            draining = stop.is_set()
            if draining:
                drained += 1
                if drained > READER_DRAIN_READS:
                    break
            if not selector.select(0 if draining else READER_POLL_S):
                if draining:
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            if size < cap:
                chunks.append(chunk[:cap - size])
            size += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        sink["data"] = b"".join(chunks)
        sink["overflow"] = size > cap
        if selector is not None:
            try:
                selector.close()
            except (OSError, ValueError):
                pass
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def kill_group(pid: int, group_only: bool = False) -> None:
    """SIGKILL the process group ``pid``; without ``group_only`` fall back to the
    process itself. Pass ``group_only`` once ``pid`` is reaped: the pid may then
    belong to an unrelated process, while a live group id cannot be reused."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if group_only:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class Runner:
    """Runs allowlisted CLI verbs as bounded subprocesses."""

    def __init__(self, env: Mapping[str, str], home: Path, config: Config, resolver: Resolver,
                 state_dir: Path, log: Callable[[str], None]) -> None:
        self.env = dict(env)
        self.home = Path(home)
        self.config = config
        self.resolver = resolver
        self.state_dir = Path(state_dir)
        self.log = log
        # CLI error text names local files (``<state base>/host.json is invalid``);
        # it reaches tool results only through ``scrubbed`` (p3-020).
        self.scrub = path_scrubber(self.env, self.home)

    def scale(self) -> float:
        return float(self.config.get("cli_timeout_scale") or 1.0)

    def timeout_for(self, key: str) -> float:
        verb = ALLOWLIST[key]
        return float(verb.timeout_s or 0) * self.scale()

    def command(self, key: str, params: Optional[Mapping[str, Any]] = None,
                operation_id: Optional[str] = None) -> List[str]:
        """``[cli_python, autonom.py, *argv]``; ``cli_unavailable`` (rejected) when unresolved."""
        argv = build_argv(key, params, operation_id)
        python = self.resolver.resolve_cli_python()
        cli = self.resolver.resolve_cli()
        return [python, cli] + argv

    def child_env(self, claim: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
        return child_env(self.env, self.home, claim)

    def _redact(self, text: str, extra: Sequence[str] = ()) -> str:
        values = [value for value in self.env.values() if isinstance(value, str) and len(value) >= 6]
        return redact(self.scrub.text(text), list(extra) + values)

    def scrubbed(self, result: CliResult, redact_values: Sequence[str] = ()) -> CliResult:
        """``result`` with local paths and secrets removed from its error object and reason.

        Every string of the CLI's error object (``error``, ``hint``, a lease
        holder label, ...) and the unavailable reason may be copied into a tool
        result by ``to_error``, ``Refs.local_host`` or a lease refusal view, so
        they are cleaned once here. ``data`` of a successful answer is left as
        is: tool modules read local files through it and choose what to show.
        ``redact_values`` (sensitive text of the call) are removed as well.
        """
        scrub = PathScrubber(self.scrub.labels, redact_values) if redact_values else self.scrub
        if result.error:
            result.error = scrub.value(result.error)
        if result.reason is not None:
            result.reason = scrub.text(result.reason)
        return result

    def spawn(self, key: str, params: Optional[Mapping[str, Any]] = None, *,
              operation_id: Optional[str] = None, claim: Optional[Mapping[str, Any]] = None,
              stdout: Any = subprocess.DEVNULL, stderr: Any = subprocess.DEVNULL) -> subprocess.Popen:
        """Start a long-lived or job verb in its own process group (no timeout)."""
        argv = self.command(key, params, operation_id)
        ensure_private_dir(self.state_dir)
        try:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                    close_fds=True, start_new_session=True, cwd=str(self.state_dir),
                                    env=self.child_env(claim))
        except (OSError, ValueError) as exc:
            raise ToolError("cli_unavailable", "The Autonom CLI could not be started: %s"
                            % type(exc).__name__, rejected=True)

    def run(self, key: str, params: Optional[Mapping[str, Any]] = None, *,
            operation_id: Optional[str] = None, claim: Optional[Mapping[str, Any]] = None,
            timeout_s: Optional[float] = None, redact_values: Sequence[str] = ()) -> CliResult:
        verb = ALLOWLIST[key]
        if verb.kind in ("job", "long"):
            raise ValueError("key %s is started with spawn(), not run()" % key)
        started = time.monotonic()
        try:
            argv = self.command(key, params, operation_id)
        except ToolError as exc:
            return self.scrubbed(CliResult(key, "unavailable", reason=exc.error))
        timeout = (float(timeout_s) if timeout_s is not None else float(verb.timeout_s or 0)) * self.scale()
        try:
            ensure_private_dir(self.state_dir)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                                    cwd=str(self.state_dir), env=self.child_env(claim))
        except (OSError, ValueError) as exc:
            return CliResult(key, "unavailable",
                             reason="The Autonom CLI could not be started: %s" % type(exc).__name__)
        out_sink: Dict[str, Any] = {}
        err_sink: Dict[str, Any] = {}
        stop_readers = threading.Event()
        readers = [threading.Thread(target=_reader, args=(proc.stdout, STDOUT_CAP, out_sink, stop_readers),
                                    daemon=True),
                   threading.Thread(target=_reader, args=(proc.stderr, STDERR_CAP, err_sink, stop_readers),
                                    daemon=True)]
        for thread in readers:
            thread.start()
        timed_out = False
        try:
            proc.wait(timeout=max(0.05, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if timed_out:
            # Whatever the group left behind is gone too. The CLI is reaped by
            # now, so only the group is signalled, never the bare pid.
            kill_group(proc.pid, group_only=True)
        grace_end = time.monotonic() + READER_GRACE_S
        for thread in readers:
            thread.join(timeout=max(0.0, grace_end - time.monotonic()))
        if any(thread.is_alive() for thread in readers):
            # A helper outside the group still holds a pipe: keep what was
            # written, stop waiting for EOF.
            stop_readers.set()
            for thread in readers:
                thread.join(timeout=READER_STOP_S)
        duration = time.monotonic() - started
        stdout = out_sink.get("data", b"").decode("utf-8", "replace")
        stderr = err_sink.get("data", b"").decode("utf-8", "replace")
        code = proc.returncode
        if timed_out:
            result = CliResult(key, "timeout", exit_code=code, duration_s=duration, pid=proc.pid)
        else:
            result = self._classify(key, code, stdout, stderr, bool(out_sink.get("overflow")),
                                    duration, proc.pid)
        if not result.ok:
            line = "cli %s exit %s status %s %.2fs" % (key, code, result.status, duration)
            if result.cli_error_code:
                line += " code %s" % result.cli_error_code
            if key not in QUIET_KEYS and stderr.strip():
                line += " stderr: " + stderr[-LOG_TAIL:]
            self.log(self._redact(line, redact_values))
        return self.scrubbed(result, redact_values)

    @staticmethod
    def _classify(key: str, code: Optional[int], stdout: str, stderr: str, overflow: bool,
                  duration: float, pid: int) -> CliResult:
        if code == 0:
            data = None if overflow else parse_cli_json(stdout)
            if data is None:
                return CliResult(key, "malformed", exit_code=code, duration_s=duration, pid=pid)
            if data.get("ok") is False:
                if isinstance(data.get("error_code"), str) and data["error_code"]:
                    return CliResult(key, "cli_error", exit_code=code, error=data,
                                     duration_s=duration, pid=pid)
                return CliResult(key, "malformed", exit_code=code, duration_s=duration, pid=pid)
            return CliResult(key, "ok", exit_code=code, data=data, duration_s=duration, pid=pid)
        error = parse_cli_json(stderr) or parse_cli_json(stdout)
        if code == 130 and error is not None and error.get("error") == "interrupted":
            return CliResult(key, "interrupted", exit_code=code, error=error, duration_s=duration, pid=pid)
        if error is None or not isinstance(error.get("error_code"), str) or not error["error_code"]:
            return CliResult(key, "malformed", exit_code=code, duration_s=duration, pid=pid)
        return CliResult(key, "cli_error", exit_code=code, error=error, duration_s=duration, pid=pid)
