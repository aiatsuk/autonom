"""Stable machine-readable failures for the Autonom CLI (CAP-PLAT-005).

Every expected failure travels as ``AutonomError`` and is rendered as one JSON
object on stderr with exit code 2:

    {"ok": false, "error_code": "idb_required", "error": "...", "hint": "..."}

``error_code`` values are part of the agent-facing contract: they may be added
but never repurposed, because a host agent branches on them. ``hint`` names the
next action a human or agent can take, so a failure is never a dead end.
"""
from __future__ import annotations

from typing import Any

# --- Target resolution -------------------------------------------------------
AMBIGUOUS_TARGET = "ambiguous_target"
CONFLICTING_TARGET_FLAGS = "conflicting_target_flags"
NO_TARGET = "no_target"
UNKNOWN_PLATFORM = "unknown_platform"

# --- Tooling -----------------------------------------------------------------
ADB_NOT_FOUND = "adb_not_found"
SIMCTL_NOT_FOUND = "simctl_not_found"
IDB_REQUIRED = "idb_required"
IDB_COMPANION_UNAVAILABLE = "idb_companion_unavailable"
MITMDUMP_REQUIRED = "mitmdump_required"
BACKEND_FAILED = "backend_failed"
IOS_HID_FRAMEWORK_MISSING = "ios_hid_framework_missing"  # simulator HID input framework absent

# --- Session -----------------------------------------------------------------
NO_ACTIVE_SESSION = "no_active_session"
SESSION_NOT_FOUND = "session_not_found"
SESSION_ALREADY_ACTIVE = "session_already_active"  # a session already owns the target
INSTALL_PATH_NOT_FOUND = "install_path_not_found"
APP_NOT_INSTALLED = "app_not_installed"
IOS_BOOT_FAILED = "ios_boot_failed"
IOS_CLEAR_REQUIRES_INSTALL_PATH = "ios_clear_requires_install_path"
EMULATOR_NOT_FOUND = "emulator_not_found"
AVD_NOT_FOUND = "avd_not_found"
AVD_REQUIRED = "avd_required"
EMULATOR_ONLY = "emulator_only"
BOOT_TIMEOUT = "boot_timeout"

# --- UI ----------------------------------------------------------------------
AMBIGUOUS_SELECTOR = "ambiguous_selector"
NO_MATCHING_NODE = "no_matching_node"
SELECTOR_INDEX_OUT_OF_RANGE = "selector_index_out_of_range"
COORDINATE_SPACE_MISMATCH = "coordinate_space_mismatch"
UNSUPPORTED_KEY_FOR_PLATFORM = "unsupported_key_for_platform"
UNSUPPORTED_ON_PLATFORM = "unsupported_on_platform"
STALE_REF = "stale_ref"  # a node ref from an older tree snapshot
ELEMENT_OFFSCREEN = "element_offscreen"  # the matched node is not on screen (0x0 / outside it)
UI_NOT_IDLE = "ui_not_idle"  # Android: the screen never went idle, so uiautomator could not read it

# --- Device state ------------------------------------------------------------
INVALID_URL = "invalid_url"
INVALID_COORDINATES = "invalid_coordinates"
UNKNOWN_PRIVACY_SERVICE = "unknown_privacy_service"
PATH_OUTSIDE_CONTAINER = "path_outside_container"
RECORDING_ALREADY_ACTIVE = "recording_already_active"
OUTPUT_NOT_WRITABLE = "output_not_writable"  # the requested output path cannot be written
INVALID_SIMULATOR_ACTION = "invalid_simulator_action"  # an unknown simulator action name
CLIPBOARD_UNAVAILABLE = "clipboard_unavailable"  # the platform does not let a shell read the clipboard

# --- Network -----------------------------------------------------------------
PORT_UNAVAILABLE = "port_unavailable"
PROXY_NOT_RUNNING = "proxy_not_running"
FLOW_NOT_FOUND = "flow_not_found"
MOCK_NOT_FOUND = "mock_not_found"
BODY_FILE_NOT_FOUND = "body_file_not_found"
BODIES_NOT_CAPTURED = "bodies_not_captured"
UNSAFE_ARTIFACTS_PERMISSIONS = "unsafe_artifacts_permissions"

# --- Consent (C-05) ----------------------------------------------------------
CONSENT_REQUIRED = "consent_required"
CONSENT_DECLINED = "consent_declined"
PHYSICAL_DEVICE_ATTACH_UNSUPPORTED = "physical_device_attach_unsupported"

# --- Live observation & metrics (Phase 4) ------------------------------------
STREAM_NOT_FOUND = "stream_not_found"
PATH_FORBIDDEN = "path_forbidden"
APP_NOT_RUNNING = "app_not_running"
TOOL_MISSING = "tool_missing"  # generic, with a `tool` extra; per-tool codes above stay
PRESET_UNAVAILABLE = "preset_unavailable"
TRACE_FAILED = "trace_failed"
SIGNAL_UNSTABLE = "signal_unstable"  # a measurement never settled within its window

# --- Flow DSL ----------------------------------------------------------------
# The DSL's code family is deliberately distinct from network capture's
# FLOW_NOT_FOUND above ("flow" there is a recorded HTTP request, mitmproxy's
# vocabulary). That code stays network-owned forever; the DSL's file-missing
# code is FLOW_FILE_NOT_FOUND (docs/COMPATIBILITY.md).
FLOW_PARSE_ERROR = "flow_parse_error"
FLOW_SCHEMA_UNSUPPORTED = "flow_schema_unsupported"
FLOW_HEADER_INVALID = "flow_header_invalid"
FLOW_UNKNOWN_COMMAND = "flow_unknown_command"
FLOW_COMMAND_INVALID = "flow_command_invalid"
FLOW_SELECTOR_INVALID = "flow_selector_invalid"
FLOW_OPTIONAL_ASSERTION_FORBIDDEN = "flow_optional_assertion_forbidden"
FLOW_VAR_UNDEFINED = "flow_var_undefined"
FLOW_VAR_CONFLICT = "flow_var_conflict"
FLOW_COPY_EMPTY = "flow_copy_empty"
FLOW_REPEAT_INVALID = "flow_repeat_invalid"
FLOW_SECRET_UNDEFINED = "flow_secret_undefined"
FLOW_FILE_NOT_FOUND = "flow_file_not_found"
FLOW_PATH_ESCAPES_WORKSPACE = "flow_path_escapes_workspace"
FLOW_CYCLE_DETECTED = "flow_cycle_detected"
FLOW_REQUIREMENTS_UNMET = "flow_requirements_unmet"
FLOW_ASSERTION_TIMEOUT = "flow_assertion_timeout"
FLOW_CHECK_FAILED = "flow_check_failed"
FLOW_NO_FLOWS_FOUND = "flow_no_flows_found"
FLOW_REPLAY_STEP_NOT_REACHED = "flow_replay_step_not_reached"
UNSUPPORTED_FLOW_COMMAND = "unsupported_flow_command"  # Maestro import/export
FLOW_SOURCE_CHANGED = "flow_source_changed"  # the flow file changed since the run
RUN_NOT_FOUND = "run_not_found"  # no recorded flow run with that id
STEP_NOT_FOUND = "step_not_found"  # no step with that id in the run
COMMENTS_WOULD_BE_LOST = "comments_would_be_lost"  # a rewrite would drop YAML comments

# --- Blueprint contracts ----------------------------------------------------
UNSUPPORTED_CAPABILITY = "unsupported_capability"
REPORT_MODEL_INVALID = "report_model_invalid"
REPORT_BUNDLE_FINALIZED = "report_bundle_finalized"
REPORT_INTEGRITY_FAILED = "report_integrity_failed"
REPORT_MERGE_CONFLICT = "report_merge_conflict"
GATE_FAILED = "gate_failed"
TEACH_RANGE_INVALID = "teach_range_invalid"
TEACH_APPROVAL_BLOCKED = "teach_approval_blocked"
APP_SKILL_INVALID = "app_skill_invalid"
REPLAY_MANIFEST_INVALID = "replay_manifest_invalid"
CI_DESTINATION_FAILED = "ci_destination_failed"

# --- Found by the 2026-09-02 device sweep -----------------------------------
USAGE_ERROR = "usage_error"                  # argparse rejected the argv
SELECTOR_REQUIRED = "selector_required"      # ui tap with neither selector nor x/y
APP_NOT_DEBUGGABLE = "app_not_debuggable"    # run-as refused the package
FLOW_NO_FOCUSED_FIELD = "flow_no_focused_field"  # inputText with nothing focused
INVALID_VALUE = "invalid_value"              # a value the verb could not use (was a bare ValueError)

# --- Deterministic capture state --------------------------------------------
# Keyboard/locale pinning edits the simulator's on-disk preference store, which
# cfprefsd only reads at boot; a booted device must be shut down first.
SIMULATOR_MUST_BE_SHUTDOWN = "simulator_must_be_shutdown"
SIMULATOR_DATA_NOT_FOUND = "simulator_data_not_found"


# --- UI recovery (optional XCUITest backend) ----------------------------------
SESSION_STOPPED = "session_stopped"  # --session-id named a stopped session for a mutating verb
SESSION_TARGET_MISMATCH = "session_target_mismatch"  # --session-id with another target's flags
DISPLAY_GEOMETRY_UNAVAILABLE = "display_geometry_unavailable"  # no screen size; no input sent
UI_ACTION_UNCERTAIN = "ui_action_uncertain"  # an input was sent and not acknowledged; never repeated
APP_ID_REQUIRED = "app_id_required"  # the XCUITest backend needs a session with --app-id
UI_BRIDGE_IN_USE = "ui_bridge_in_use"  # another session owns this simulator's XCUITest runner
XCUITEST_UNAVAILABLE = "xcuitest_unavailable"  # no full Xcode, or the runner sources are absent
XCUITEST_BUILD_FAILED = "xcuitest_build_failed"
XCUITEST_START_FAILED = "xcuitest_start_failed"
XCUITEST_TIMEOUT = "xcuitest_timeout"  # a read (tree, geometry) was not answered
XCUITEST_FAILED = "xcuitest_failed"  # the runner reported an unexpected failure
STALE_UI_ELEMENT = "stale_ui_element"  # the observed element changed; refresh the tree
NO_FOCUSED_FIELD = "no_focused_field"  # text typed through the runner with no focused field

# --- Canvas actions (captures, recording, install roots) ----------------------
RECORDING_NOT_ACTIVE = "recording_not_active"  # record.stop with no recording on this device
CAPTURE_NOT_FOUND = "capture_not_found"  # a capture name the captures index does not hold
CAPTURE_TOO_LARGE = "capture_too_large"  # a screenshot or video above its size cap; nothing saved
INSTALL_NOT_CONFIGURED = "install_not_configured"  # the Canvas was started with no --install-root
INSTALL_PATH_NOT_ALLOWED = "install_path_not_allowed"  # the path resolves outside every install root
INSTALL_FAILED = "install_failed"  # the device refused the install (reason in the `reason` extra)

# --- Canvas workspace (autonom canvas stop|attach|detach|list) ------------------
CANVAS_NOT_FOUND = "canvas_not_found"  # no live Canvas matches --port/--workspace (or none runs)
CANVAS_AMBIGUOUS = "canvas_ambiguous"  # several live Canvases and no --port or --workspace
WORKSPACE_IN_USE = "workspace_in_use"  # another live Canvas already serves this workspace

# --- Ticket pre-test (autonom pretest) -------------------------------------------
# A pre-test is one ticket checked on a device inside a session. Report
# integrity reuses REPORT_INTEGRITY_FAILED; a missing session reuses
# NO_ACTIVE_SESSION; git or device failures reuse BACKEND_FAILED.
NO_ACTIVE_PRETEST = "no_active_pretest"  # a pretest verb with no active pre-test in the session
PRETEST_ALREADY_ACTIVE = "pretest_already_active"  # start while another pre-test is active (extra: pretest_id)
PRETEST_FINALIZED = "pretest_finalized"  # a change to a packed pre-test; packs are immutable
CHECK_ALREADY_OPEN = "check_already_open"  # check begin while another check is open (extra: open_check_id)
NO_OPEN_CHECK = "no_open_check"  # check end for a check that is not the open one
UNKNOWN_CHECK = "unknown_check"  # a check id the pre-test does not hold (extra: check_id)
INVALID_VERDICT = "invalid_verdict"  # a check verdict outside pass|fail|blocked|not_covered|inconclusive
UNKNOWN_FINDING = "unknown_finding"  # a finding id the pre-test or pack does not hold
INVALID_SEVERITY = "invalid_severity"  # a finding severity outside blocker|major|minor|info
EVIDENCE_REF_UNKNOWN = "evidence_ref_unknown"  # an evidence ref that names no session artifact (extra: ref)
BUDGET_EXHAUSTED = "budget_exhausted"  # the pre-test's action or minute budget is used up; no input sent
FORBIDDEN_ACTION = "forbidden_action"  # the target matches a forbidden text pattern, or cannot be read; no input sent
APP_NOT_ALLOWED = "app_not_allowed"  # open or launch of an app outside the pre-test's allow list
REVIEW_NEEDS_FINDING = "review_needs_finding"  # return-to-dev without a finding id
NOT_AUTOMATIC = "not_automatic"  # confirm or dismiss of a finding that is not automatic
PROFILE_INVALID = "profile_invalid"  # an app profile that does not parse or holds credential-shaped values
NOT_A_PRETEST_PACK = "not_a_pretest_pack"  # a path that is not a finalized pre-test pack
DRAFT_UNAVAILABLE = "draft_unavailable"  # no draft flow can be built or found for the finding
PRETEST_DAMAGED = "pretest_damaged"  # the active pre-test state file is unreadable; no input sent
RACE_NEEDS_COUNT = "race_needs_count"  # a race edge case needs --observed-count N
CHECK_NOT_ENDED = "check_not_ended"  # a change to a check that has not been ended yet
GIT_MISSING = "git_missing"  # git is not on PATH
# --- Control lease (P2) -------------------------------------------------------
CONTROL_HELD = "control_held"                          # another live holder controls the target
CONTROL_GENERATION_STALE = "control_generation_stale"  # the claim's generation is not the lease's
CONTROL_LEASE_EXPIRED = "control_lease_expired"        # the claimed lease expired; acquire again
CONTROL_NOT_HELD = "control_not_held"                  # a claim, release or renew without that lease

# --- iOS device daemon (speed plan 2b) -------------------------------------------
# An input sent once that the app did not confirm, and the cause when one is
# known, are warnings on an `ok: true` reply; the other three are failures (or,
# for the daemon, the reason a call went to the idb process instead). An input
# idb_companion itself never confirmed (cancelled at the daemon's deadline, or
# lost with the companion) fails with `input_unconfirmed`: sent once, not again.
INPUT_UNCONFIRMED = "input_unconfirmed"  # sent once; unconfirmed by the app or the companion
IOS_INPUT_BLOCKED = "ios_input_blocked"  # warning: dtuhidd.active set and no Device Hub; reboot it
IOS_DISPLAY_NOT_LIT = "ios_display_not_lit"  # a read hit a display that is not lit yet (foldable)
COMPANION_TOO_OLD = "companion_too_old"  # idb_companion older than 1.6.4 (no quiet stream)
DAEMON_UNREACHABLE = "daemon_unreachable"  # the device daemon did not answer before the request

# --- Interrupt ------------------------------------------------------------------
# The exit-130 envelope of a command cut by Ctrl-C or SIGINT. Only the CLI's
# interrupt handler builds it; observe's `observe_error.code` for an
# interrupt after the input was sent is the same word.
INTERRUPTED = "interrupted"


class AutonomError(RuntimeError):
    """An expected failure with a stable code and an actionable hint."""

    def __init__(self, code: str, message: str, hint: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": self.code,
            "error": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.extra)
        return payload


def tool_missing(tool: str) -> AutonomError:
    """Uniform 'this backend is not installed' failure with an install hint."""
    table = {
        "adb": (
            ADB_NOT_FOUND,
            "adb not found on PATH; install Android platform-tools",
            "Install Android platform-tools, or pass --adb /path/to/adb. Run 'autonom doctor'.",
        ),
        "simctl": (
            SIMCTL_NOT_FOUND,
            "xcrun not found on PATH; iOS support needs macOS with Xcode",
            "Install Xcode and run 'xcode-select --install'. Run 'autonom doctor'.",
        ),
        "idb": (
            IDB_REQUIRED,
            "idb not found on PATH; iOS UI verbs need the iOS Development Bridge",
            "Install idb-companion and the idb client, or pass --idb /path/to/idb. "
            "Run 'autonom doctor'.",
        ),
        "mitmdump": (
            MITMDUMP_REQUIRED,
            "mitmdump not found on PATH; network capture needs mitmproxy",
            "Install mitmproxy (brew install mitmproxy, or pipx install mitmproxy), "
            "or pass --mitmdump /path/to/mitmdump. Run 'autonom doctor'.",
        ),
    }
    code, message, hint = table[tool]
    return AutonomError(code, message, hint)
