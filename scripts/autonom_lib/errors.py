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

# --- Session -----------------------------------------------------------------
NO_ACTIVE_SESSION = "no_active_session"
SESSION_NOT_FOUND = "session_not_found"
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

# --- Device state ------------------------------------------------------------
INVALID_URL = "invalid_url"
INVALID_COORDINATES = "invalid_coordinates"
UNKNOWN_PRIVACY_SERVICE = "unknown_privacy_service"
PATH_OUTSIDE_CONTAINER = "path_outside_container"
RECORDING_ALREADY_ACTIVE = "recording_already_active"

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
UNSUPPORTED_FLOW_COMMAND = "unsupported_flow_command"  # Maestro import/export


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
