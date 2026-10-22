#!/usr/bin/env python3
"""Autonom CLI — portable control plane for AI agents (Android + iOS Simulator)."""
from __future__ import annotations

import argparse
import difflib
import errno
import json
import math
import os
import shutil
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# docs/INSTALL.md states 3.11 (CI tests 3.11 on pull requests and 3.14 on
# main). An older interpreter must not die in an import traceback before any
# envelope could be printed: checked before the library is imported, with the
# same stderr shape as every other failure.
MIN_PYTHON = (3, 11)


def _python_too_old(version: tuple[int, ...]) -> dict[str, Any] | None:
    if tuple(version[:2]) >= MIN_PYTHON:
        return None
    found = ".".join(str(part) for part in version[:3])
    wanted = ".".join(str(part) for part in MIN_PYTHON)
    return {
        "ok": False,
        "error_code": "tool_missing",  # errors.TOOL_MISSING; not importable yet
        "tool": "python3",
        "error": f"Autonom needs Python >= {wanted}; this is Python {found}",
        "hint": f"Run it with a newer interpreter, e.g. 'python3.{MIN_PYTHON[1]} "
                "scripts/autonom.py ...' (see docs/INSTALL.md).",
    }


_TOO_OLD = _python_too_old(tuple(sys.version_info))
if _TOO_OLD is not None:
    print(json.dumps(_TOO_OLD), file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonom_lib import __version__  # noqa: E402
from autonom_lib import actions as actions_mod  # noqa: E402
from autonom_lib import accessibility as accessibility_mod  # noqa: E402
from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib import consent as consent_mod  # noqa: E402
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import device_state  # noqa: E402
from autonom_lib import doctor as doctor_mod  # noqa: E402
from autonom_lib import emulator as emulator_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import follow as follow_mod  # noqa: E402
from autonom_lib import ios_idb  # noqa: E402
from autonom_lib.flow import canonical as flow_canonical  # noqa: E402
from autonom_lib.flow import compiler as flow_compiler  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import maestro as flow_maestro  # noqa: E402
from autonom_lib.flow import repair as flow_repair  # noqa: E402
from autonom_lib.flow import validator as flow_validator  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import observe as observe_mod  # noqa: E402
from autonom_lib import providers as providers_mod  # noqa: E402
from autonom_lib import teach as teach_mod  # noqa: E402
from autonom_lib import app_skills as app_skills_mod  # noqa: E402
from autonom_lib import simulator as simulator_mod  # noqa: E402
from autonom_lib import ios_simctl  # noqa: E402
from autonom_lib import logs as logs_mod  # noqa: E402
from autonom_lib import platform as platform_mod  # noqa: E402
from autonom_lib import proof as proof_mod  # noqa: E402
from autonom_lib import processes as processes_mod  # noqa: E402
from autonom_lib import screenshot as shot_mod  # noqa: E402
from autonom_lib import selector as selector_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui as ui_mod  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

# Report, campaign, metrics, network and atlas modules are imported inside the
# commands that use them: a quick verb such as `ui tree` must not pay for
# urllib/ssl, zipfile or statistics on every call. Their old module-level names
# still resolve (module __getattr__) to the very module object the commands
# import, so patching an attribute through them, as in
# `patch.object(cli.proxy_mod, "stop")`, still reaches the commands; rebinding
# the name itself on this module does not.
_LAZY_MODULES = {
    "atlas_fingerprint": "autonom_lib.atlas.fingerprint",
    "atlas_graph": "autonom_lib.atlas.graph",
    "campaign_mod": "autonom_lib.campaign",
    "device_proxy_android": "autonom_lib.network.device_proxy_android",
    "flow_report": "autonom_lib.flow.report",
    "gates_mod": "autonom_lib.gates",
    "har_mod": "autonom_lib.network.har",
    "metrics_artifacts": "autonom_lib.metrics.artifacts",
    "metrics_frames": "autonom_lib.metrics.frames",
    "metrics_memory": "autonom_lib.metrics.android_memory",
    "metrics_presets": "autonom_lib.metrics.presets",
    "metrics_series": "autonom_lib.metrics.series",
    "metrics_snapshot": "autonom_lib.metrics.snapshot",
    "metrics_trace": "autonom_lib.metrics.trace",
    "mocks_mod": "autonom_lib.network.mocks",
    "proxy_mod": "autonom_lib.network.proxy",
    "redact_mod": "autonom_lib.network.redact",
    "report_bundle_mod": "autonom_lib.report_bundle",
    "report_export_mod": "autonom_lib.report_export",
    "report_model_mod": "autonom_lib.report_model",
    "store_mod": "autonom_lib.network.store",
    "tour_mod": "autonom_lib.tour",
}


def __getattr__(name: str) -> Any:
    module = _LAZY_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return importlib.import_module(module)


_LAST_EMIT: dict[str, Any] | None = None
# The exit status of an interrupt (SIGINT, Ctrl-C), the shell's convention.
EXIT_INTERRUPTED = 130
# Set when an interrupt ended the observe step of an action whose input was
# already sent (AL-11): the reply is printed and journaled ok as usual, then
# `main` exits EXIT_INTERRUPTED so `&&` chains and `set -e` scripts stop.
_OBSERVE_INTERRUPTED = False
# The root parser of the running invocation: the journal resolves argv through
# it (abbreviations, --flag=value, value positions) before scrubbing (SEC-002).
_ROOT_PARSER: argparse.ArgumentParser | None = None


def _control_payload() -> dict[str, Any] | None:
    """The `control` object of an admitted MUTATING/FLOW verb (contract 5.4):
    only when a lease file existed for the target or a claim was given, so a
    verb on a target without a lease prints exactly what it printed before."""
    current = control_mod.invocation()
    if current.get("mode") not in control_mod.ADMITTED_MODES:
        return None
    for result in current.get("admitted", {}).values():
        if result.get("generation") is not None or current.get("claim") is not None:
            return {"generation": result.get("generation"),
                    "holder_id": result.get("holder_id"),
                    "operation_id": result.get("operation_id")}
    return None


def _with_control(data: Any) -> Any:
    if isinstance(data, dict):
        control = _control_payload()
        if control is not None:
            data.setdefault("control", control)
    return data


def _unique_warnings(warnings: list[Any]) -> list[Any]:
    """`warnings` with exact repeats dropped, first occurrence order kept."""
    seen: set[str] = set()
    out: list[Any] = []
    for item in warnings:
        try:
            key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            key = repr(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# How every JSON reply is printed on stdout (AL-10): one line, no spaces.
JSON_SEPARATORS = (",", ":")


def emit(data: Any, *, as_json: bool) -> int:
    global _LAST_EMIT
    _with_control(data)
    if isinstance(data, dict) and isinstance(data.get("warnings"), list):
        # one cause reached through two paths is still one warning
        data["warnings"] = _unique_warnings(data["warnings"])
    if isinstance(data, dict) and data.get("platform") == IOS:
        # UI provenance measured by this invocation (which backend served the
        # tree and the input, and why it fell back); never overrides a key
        # the verb set itself.
        from autonom_lib import ui_ios
        observed = ui_ios.observation(data.get("target_id", ""))
        for key in ui_ios.PROVENANCE_KEYS:
            if observed.get(key) is not None:
                data.setdefault(key, observed[key])
    _LAST_EMIT = data if isinstance(data, dict) else None
    if as_json or isinstance(data, (dict, list)):
        # One line for every caller and profile (AL-10): the same keys and
        # values the indented form had, compact, so an agent reads a reply
        # in one line and a script splits replies on newlines.
        print(json.dumps(data, ensure_ascii=False, separators=JSON_SEPARATORS))
    else:
        print(data)
    return 0


def _interrupted_error() -> errors.AutonomError:
    """The exit-130 envelope of an interrupt (Ctrl-C, SIGINT). `error` keeps
    the word it always carried; `error_code` and `hint` make it an envelope
    like every other failure (docs/COMPATIBILITY.md)."""
    return errors.AutonomError(
        errors.INTERRUPTED, "interrupted",
        "The command was cancelled (Ctrl-C or SIGINT) and may have stopped partway. "
        "If it was driving a device, read the screen (autonom ui tree) before running "
        "it again: an input it was sending may already have arrived.")


def fail_error(exc: errors.AutonomError, code: int = 2) -> int:
    print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
    return code


def _target(args: argparse.Namespace) -> Target:
    record = session_mod.load_current()
    target = platform_mod.resolve(args, session_record=record)
    if record and (record.get("target_id") != target.target_id or record.get("platform") != target.platform):
        if getattr(args, "session_id", None):
            raise errors.AutonomError(
                errors.SESSION_TARGET_MISMATCH,
                f"session {record.get('session_id')} belongs to {record.get('target_id')}, "
                f"not {target.target_id}",
                "Drop the target flags, or name the session that owns that target.",
                session_id=record.get("session_id"), target_id=record.get("target_id"))
    # Control lease admission (contract 5.4): MUTATING and FLOW verbs are
    # admitted once per target here, the first device-facing call of every
    # handler; every other class reads nothing. A refusal raises before any
    # device call.
    control_mod.admit_once(target.platform, target.target_id)
    return target


def _selectors(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "text": getattr(args, "text", None),
        "desc": getattr(args, "desc", None),
        "resource_id": getattr(args, "resource_id", None),
        "class_name": getattr(args, "class_name", None),
        "package": getattr(args, "package", None),
        "role": getattr(args, "role", None),
        "clickable": getattr(args, "clickable", None),
        "enabled": getattr(args, "enabled", None),
    }


def _check_selector_regex(args: argparse.Namespace) -> None:
    """`--mode regex` with a malformed pattern is the caller's mistake.

    The selector engine reports it as `no_matching_node` (flows pin that), and
    only once some node is compared at all; on the CLI a bad pattern is an
    invalid value, refused before the device is asked for anything.
    """
    if getattr(args, "mode", None) != "regex":
        return
    for name, value in _selectors(args).items():
        if not isinstance(value, str):
            continue
        try:
            re.compile(value)
        except re.error as exc:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"invalid regular expression for --{name.replace('_', '-')}: {exc}",
                "Fix the pattern (escape ( [ * with a backslash), or use "
                "--mode contains.",
            ) from exc


def _not_writable(path: Path, flag: str, reason: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.OUTPUT_NOT_WRITABLE,
        f"{flag} {path}: {reason}",
        f"Pass a {flag} path in a directory you can write to.",
        path=str(path),
    )


def _output_path(raw: str | Path, flag: str) -> Path:
    """An output file the verb is about to write, checked before any effect.

    A path whose directory is read-only (or is itself a directory) used to
    surface as a PermissionError traceback after the device work was done.
    An existing writable file — `/dev/null` included — is fine as it is.
    """
    path = Path(raw).expanduser()
    if path.is_dir():
        raise _not_writable(path, flag, "is a directory")
    if path.exists():
        if not os.access(path, os.W_OK):
            raise _not_writable(path, flag, "permission denied")
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _not_writable(path, flag, exc.strerror or str(exc)) from exc
    if not os.access(path.parent, os.W_OK):
        raise _not_writable(path, flag, "directory is not writable")
    return path


def _output_dir(raw: str | Path, flag: str) -> Path:
    """An output directory: created when missing, refused when unwritable."""
    path = Path(raw).expanduser()
    if path.exists() and not path.is_dir():
        raise _not_writable(path, flag, "exists and is not a directory")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _not_writable(path, flag, exc.strerror or str(exc)) from exc
    if not os.access(path, os.W_OK):
        raise _not_writable(path, flag, "directory is not writable")
    return path


def _point(raw: str, flag: str) -> tuple[int, int]:
    """`X,Y` as two integers; anything else is one clean invalid_value."""
    parts = [part.strip() for part in str(raw).split(",")]
    try:
        if len(parts) != 2:
            raise ValueError
        return int(parts[0]), int(parts[1])
    except ValueError:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"{flag} takes X,Y (two integers), got {raw!r}",
            f"Example: {flag} 200,400",
        ) from None


def _ignored_flag_warning(flag: str, platform: str, why: str) -> dict[str, str]:
    """A flag that has no effect on this platform: said, never silently dropped."""
    return {
        "code": "flag_ignored_on_platform",
        "error": f"{flag} has no effect on {platform} and was ignored",
        "hint": why,
    }


def cmd_version(_: argparse.Namespace) -> int:
    return emit({"name": "autonom", "version": __version__}, as_json=True)


def cmd_devices(args: argparse.Namespace) -> int:
    devices, warnings = platform_mod.list_all(args, only=args.platform)
    payload: dict[str, Any] = {"ok": True, "devices": devices, "warnings": warnings}
    if args.platform in (None, ANDROID):
        # Bootable-but-not-running AVDs are part of the inventory too; a
        # missing emulator binary just means the key is absent.
        adb_path = None
        try:
            adb_path = adb_mod.find_adb(getattr(args, "adb", None))
            emulator_bin = emulator_mod.find_emulator(
                getattr(args, "emulator", None), adb_path=adb_path
            )
            payload["avds"] = emulator_mod.list_avds(emulator_bin)
            # Names alone do not say which AVD is the phone and which the
            # tablet; the hardware profile does (`avd_profiles`, additive).
            payload["avd_profiles"] = emulator_mod.describe_avds(payload["avds"])
        except errors.AutonomError:
            pass
        # A running emulator names the AVD it booted from, so a serial can be
        # matched to a profile instead of guessed from `product:`. Best-effort.
        if adb_path:
            for device in devices:
                if device.get("platform") == ANDROID and device.get("running"):
                    name = emulator_mod.running_avd_name(adb_path, device["target_id"])
                    if name:
                        device["avd"] = name
    if warnings and not devices:
        payload["next_action"] = "run 'autonom doctor' to see what is missing"
    return emit(payload, as_json=True)


def cmd_devices_boot(args: argparse.Namespace) -> int:
    avd = getattr(args, "avd", None)
    if getattr(args, "port", None) is not None and not avd:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "--port picks the console port of a new emulator; it needs --avd",
            "Pass --avd <name> --port <even port 5554-5682>.")
    explicit = any(getattr(args, flag, None) for flag in ("target", "serial", "udid"))
    if avd and explicit:
        raise errors.AutonomError(
            errors.CONFLICTING_TARGET_FLAGS,
            "--avd starts a new emulator; do not also pass --target/--serial/--udid",
            "Pick one: --avd <name> for a new Android emulator, --target for a simulator.",
        )
    if avd:
        adb_path = adb_mod.find_adb(getattr(args, "adb", None))
        emulator_bin = emulator_mod.find_emulator(
            getattr(args, "emulator", None), adb_path=adb_path
        )
        detail = emulator_mod.boot_avd(
            emulator_bin, adb_path, avd,
            wait=not args.no_wait, timeout=args.timeout,
            http_proxy=getattr(args, "http_proxy", None),
            port=getattr(args, "port", None),
        )
        return emit({"ok": True, "platform": ANDROID, **detail}, as_json=True)
    if not explicit:
        raise errors.AutonomError(
            errors.NO_TARGET,
            "devices boot needs --avd <name> (Android) or --target/--udid <simulator>",
            "See 'autonom devices' for simulators and the avds list.",
        )
    target = platform_mod.resolve(args)
    if target.platform == IOS:
        booted = ios_simctl.boot(target.tool, target.target_id, timeout=args.timeout)
        return emit(
            {"ok": True, "booted": booted, "already_running": not booted, **target.identity()},
            as_json=True,
        )
    running = any(
        device.serial == target.target_id and device.state == "device"
        for device in adb_mod.list_devices(target.tool)
    )
    if running:
        return emit(
            {"ok": True, "booted": False, "already_running": True, **target.identity()},
            as_json=True,
        )
    raise errors.AutonomError(
        errors.AVD_REQUIRED,
        f"'{target.target_id}' is not running, and an Android emulator boots from an AVD image, not a serial",
        "Pass --avd <name>; 'autonom devices' lists the available AVDs.",
    )


def cmd_devices_shutdown(args: argparse.Namespace) -> int:
    target = platform_mod.resolve(args)
    # Resolved without `_target` (no session binding): admitted here, before
    # the simulator lookup and the shutdown.
    control_mod.admit_once(target.platform, target.target_id)
    if target.platform == IOS:
        simulator = ios_simctl.find_simulator(target.tool, target.target_id)
        already = simulator is not None and simulator.state == "Shutdown"
        if not already:
            ios_simctl.shutdown(target.tool, target.target_id)
        return emit({"ok": True, "stopped": True, "already_stopped": already,
                     **target.identity()}, as_json=True)
    detail = emulator_mod.kill_emulator(target.tool, target.target_id)
    return emit({"ok": True, "platform": ANDROID, **detail}, as_json=True)


# --- session -----------------------------------------------------------------


def _session_target_gone(args: argparse.Namespace) -> bool:
    """True only when the current session's target is confirmed absent: no
    such adb device, no such simulator. Anything that cannot be checked (a
    missing tool, no session) is not "gone"."""
    tools = argparse.Namespace(adb=getattr(args, "adb", None),
                               simctl=getattr(args, "simctl", None))
    try:
        target = _target(tools)
        if target.platform == IOS:
            return ios_simctl.find_simulator(target.tool, target.target_id) is None
        return all(device.serial != target.target_id
                   for device in adb_mod.list_devices(target.tool))
    except errors.AutonomError:
        return False


_STARTED_BY = re.compile(r"^canvas:([0-9]{1,5}):([0-9]{1,10})$")


def _started_by(args: argparse.Namespace) -> dict[str, Any] | None:
    """`--started-by canvas:<port>:<pid>` as the record's `started_by`."""
    raw = getattr(args, "started_by", None)
    if raw is None:
        return None
    if not getattr(args, "alongside", False):
        raise errors.AutonomError(
            errors.USAGE_ERROR, "--started-by is only accepted with --alongside",
            "A Canvas starts its sessions with 'session start --alongside "
            "--started-by canvas:<port>:<pid>'.")
    match = _STARTED_BY.fullmatch(raw)
    if not match or not 0 <= int(match.group(1)) <= 65535 or int(match.group(2)) < 1:
        raise errors.AutonomError(
            errors.USAGE_ERROR, f"--started-by must look like canvas:<port>:<pid>, not {raw!r}",
            "For example --started-by canvas:3277:12345 (port 0-65535, pid above 0).")
    return {"kind": "canvas", "port": int(match.group(1)), "pid": int(match.group(2))}


def _explicit_target_flags(args: argparse.Namespace) -> bool:
    return any(getattr(args, flag, None) for flag in ("target", "serial", "udid"))


def _already_active(record: dict[str, Any], *, gone: bool = False,
                    with_started_by: bool = False) -> errors.AutonomError:
    target_id = record.get("target_id") or record.get("serial")
    extra: dict[str, Any] = {"stale_target": True} if gone else {}
    if with_started_by:
        extra["started_by"] = record.get("started_by")
    return errors.AutonomError(
        errors.SESSION_ALREADY_ACTIVE,
        f"session {record.get('session_id')} is still active on "
        f"{target_id}" + (" (no longer present)" if gone else ""),
        f"Stop it first with 'autonom session stop' (it clears the session on "
        f"{target_id} even if that target is gone), then start the new one.",
        session_id=record.get("session_id"),
        target_id=target_id,
        **extra,
    )


def cmd_session_start(args: argparse.Namespace) -> int:
    started_by = _started_by(args)
    label = session_mod.validate_label(getattr(args, "label", None))
    profile = session_mod.validate_profile(getattr(args, "profile", None))
    alongside_flag = bool(getattr(args, "alongside", False))
    explicit = _explicit_target_flags(args)
    # One session per target. `current.json` keeps naming the machine's
    # primary session; a start on another target named explicitly (or with
    # --alongside) runs beside it and never replaces that pointer.
    current = session_mod.load_current()
    if current and not alongside_flag:
        if not explicit:
            # Starting a second one used to silently replace the current
            # pointer and orphan the first one's proxy and log stream.
            raise _already_active(current)
        if _session_target_gone(args):
            raise _already_active(current, gone=True)
    alongside = alongside_flag or bool(current)
    target = platform_mod.resolve(args)
    live = session_mod.live_for_target(target.target_id, target.platform)
    if live is not None:
        # The refused attempt is journaled where it belongs: in the session
        # that holds this target, not in the primary on another device.
        session_mod.select(live["session_id"])
        raise _already_active(live, with_started_by=True)
    # Everything that can be refused is refused before the session exists.
    # With --install or --launch the start drives the device: admitted here
    # like a mutating verb (a plain start is lifecycle and reads nothing).
    control_mod.admit_once(target.platform, target.target_id)
    install_path = Path(args.install).expanduser() if args.install else None
    if install_path is not None and not install_path.exists():
        raise errors.AutonomError(
            errors.INSTALL_PATH_NOT_FOUND, f"package not found: {install_path}",
            "Pass the path of an .apk (Android) or a built .app bundle (iOS).",
        )
    launch_id = None
    if args.launch is not None:
        launch_id = args.app_id or args.launch or None
        if not launch_id:
            raise errors.AutonomError(
                errors.NO_TARGET,
                "--launch requires --app-id or an explicit package id",
                "Pass --app-id com.example.app, or --launch com.example.app.",
            )
    tooling: dict[str, Any] = {}
    booted = False

    if target.platform == IOS:
        tooling["simctl"] = target.tool
        booted = ios_simctl.boot(target.tool, target.target_id)
        tooling["idb"] = ios_idb.probe(
            getattr(args, "idb", None),
            ios_idb.companion_endpoint(getattr(args, "idb_host", None), getattr(args, "idb_port", None)),
        )
    else:
        tooling["adb"] = target.tool

    try:
        record = session_mod.start_session(
            target.tool,
            serial=target.serial,
            app_id=args.app_id,
            platform=target.platform,
            target_id=target.target_id,
            tooling=tooling,
            alongside=alongside,
            started_by=started_by,
            exclusive=True,
            label=label,
            profile=profile,
        )
    except errors.AutonomError as exc:
        # Lost a race on this target under the store lock: like the refusal
        # above, the attempt is journaled in the session that holds it.
        holder = exc.extra.get("session_id")
        if exc.code == errors.SESSION_ALREADY_ACTIVE and isinstance(holder, str):
            session_mod.select(holder)
        raise
    if alongside:
        # The rest of this command (its saves and its journal entry) belongs
        # to the new session, never to the one `current.json` names. If the
        # start is rolled back, the entry goes nowhere (as a rolled-back
        # primary start's always did), never to that other session.
        session_mod.select(record["session_id"])
        session_mod.keep_journal_in(record["session_id"])

    record["ui_backend_preference"] = getattr(args, "ui_backend", None) or "auto"
    warnings: list[dict[str, Any]] = []
    if target.platform == IOS and tooling.get("idb", {}).get("state") != "ready":
        warnings.append({
            "code": errors.IDB_REQUIRED,
            "error": "idb is not available; UI will try the XCUITest backend when Xcode is available",
            "hint": "simctl-backed verbs (screenshot, logs, open, permissions) still work.",
        })

    if getattr(args, "log_stream", False) and target.platform != IOS:
        warnings.append(_ignored_flag_warning(
            "--log-stream", target.platform,
            "The background stream is iOS-only; on Android read logcat with "
            "'autonom logs tail' or 'autonom logs follow --source device'."))
    if args.activity and target.platform == IOS:
        warnings.append(_ignored_flag_warning(
            "--activity", target.platform,
            "Activities are Android components; iOS launches by bundle id."))

    # What --install / --launch actually did, reported beside the session: the
    # record alone did not say whether a launch happened, or how.
    installed = False
    launched: dict[str, Any] | None = None
    try:
        if install_path is not None:
            if target.platform == IOS:
                ios_simctl.install(target.tool, target.target_id, install_path)
            else:
                session_mod.install_app(target.tool, target.target_id, install_path)
            record["install_path"] = str(install_path.resolve())
            installed = True

        if launch_id:
            if target.platform == IOS:
                pid = ios_simctl.launch(target.tool, target.target_id, launch_id)
                launched = {"app_id": launch_id, "pid": pid}
            else:
                # `am start -W`: mode, component and the launch report
                detail = session_mod.launch_app(target.tool, target.target_id, launch_id,
                                                activity=args.activity)
                launched = {"app_id": launch_id, **detail}
            record["app_id"] = launch_id

        if target.platform == IOS and getattr(args, "log_stream", False):
            # bounded on disk (AUTONOM_IOS_LOG_MAX_MB), filtered to the
            # installed app's bundle, and entered in the process registry
            # under this session (background pid, streams[] and registry row)
            logs_mod.start_session_log_stream(target, record)
    except BaseException as exc:
        # A failed install or launch must not leave a half-built session as
        # "current": the next verb would silently drive it.
        session_mod.terminate_pid((record.get("background") or {}).get("log_stream_pid"))
        session_mod.save(record)
        # Only the new session: a primary running beside it is never touched.
        session_mod.stop_session(session_id=record["session_id"])
        if isinstance(exc, errors.AutonomError):
            exc.extra.setdefault("session_rolled_back", record.get("session_id"))
        raise

    session_mod.save(record)
    primary = session_mod._current_id(None) == record.get("session_id")
    payload: dict[str, Any] = {"ok": True,
                               "session": {**record, "primary": primary,
                                           "started_by": record.get("started_by")},
                               "installed": installed, "launched": launched}
    if target.platform == IOS:
        payload["booted"] = booted
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_session_list(args: argparse.Namespace) -> int:
    """The sessions of the machine store: live ones, every one with --all.

    Needs no session and never resolves a device: optional target flags
    filter by comparing strings (`--serial` is an Android id, `--udid` an
    iOS one, `--platform` narrows either way)."""
    platform_mod._check_conflicts(args)  # noqa: SLF001 - pure flag check
    wanted_id = next((getattr(args, flag, None) for flag in ("target", "serial", "udid")
                      if getattr(args, flag, None)), None)
    wanted_platform = getattr(args, "platform", None) or (
        "android" if getattr(args, "serial", None) else
        "ios" if getattr(args, "udid", None) else None)
    entries, warnings = session_mod.list_sessions(include_stopped=bool(getattr(args, "all", False)))
    if wanted_id is not None:
        entries = [entry for entry in entries if entry["target_id"] == str(wanted_id)]
    if wanted_platform is not None:
        entries = [entry for entry in entries if entry["platform"] == wanted_platform]
    payload: dict[str, Any] = {"ok": True, "sessions": entries, "count": len(entries)}
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_session_stop(args: argparse.Namespace) -> int:
    record = session_mod.load_current()
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION,
            "no active session",
            "Start one with 'autonom session start'.",
        )

    # Marked before any teardown step (issue #31): a runner request racing
    # this stop is refused with session_stopped instead of starting a runner
    # the teardown below could no longer reach. A stop that aborts before the
    # session is stopped withdraws its own owner token again: the mark stays
    # while another `session stop` is still tearing down, and is gone (the
    # session usable again) once no stopper is left.
    stopper = session_mod.mark_stopping(record)
    try:
        return _session_stop_marked(args, record)
    except BaseException:
        if not (session_mod.on_disk(record).get("stopped_at")):
            session_mod.clear_stopping(record, stopper)
        raise


def _session_stop_marked(args: argparse.Namespace, record: dict[str, Any]) -> int:
    # Keep the session pointer and its saved snapshot if device-side restore
    # fails: a later `ui accessibility reset` can still finish the cleanup.
    accessibility_teardown = None
    if record.get("accessibility"):
        accessibility_teardown = accessibility_mod.restore(_target(args), record)

    background = record.get("background") or {}
    # A session whose emulator was killed or simulator deleted must still be
    # clearable, or every later `session start` is refused for good.
    gone = _session_target_gone(args)

    def _detach() -> Any:
        if not (record.get("network") or {}).get("attached"):
            return {"was_attached": False}
        target = _target(argparse.Namespace())
        if gone and target.platform == ANDROID:
            # The proxy setting lived on the device, which is gone with it.
            record.setdefault("network", {}).update(
                {"attached": False, "device_proxy": None, "previous_http_proxy": None,
                 "previous_proxy_settings": None, "capture_mode": None})
            return {"was_attached": True, "skipped": "stale_target"}
        if target.platform == ANDROID:
            from autonom_lib.network import device_proxy_android

            return device_proxy_android.detach(target, record)
        from autonom_lib.network import device_proxy_ios

        return device_proxy_ios.detach(target, record)

    # The pids in the record may be days old and belong to anything by now:
    # each is signalled only once its registry row or its command line shows
    # it is still this session's writer or recorder. The detail stays the
    # bool it always was (True: a process was stopped); how each pid was
    # judged is kept for the warnings below.
    verification: dict[str, dict[str, Any]] = {}

    def _log_stream() -> bool:
        outcome = logs_mod.stop_log_writer(record, background.get("log_stream_pid"))
        verification["log_stream"] = outcome
        return outcome["result"] == "terminated"

    def _recorder() -> bool:
        outcome = _stop_recorder(record)
        verification["recorder"] = outcome
        return outcome["result"] == "terminated"

    def _stop_ui_bridge() -> bool:
        # The optional XCUITest runner: asked to stop through its mailbox,
        # then its registry row; session_processes below is the backstop.
        if record.get("platform") != IOS:
            return False
        from autonom_lib import ios_xctest
        return ios_xctest.stop(_target(argparse.Namespace()))

    # Order matters: restore the device's network before killing the proxy, or the
    # device is briefly pointed at a dead listener (INV-07, INV-10).
    actions: list[tuple[str, Any]] = [
        ("xcuitest", _stop_ui_bridge),
        ("log_stream", _log_stream),
        ("recorder", _recorder),
        ("network_detach", _detach),
    ]
    teardown = session_mod.run_teardown(actions)
    failed_detach = next((step for step in teardown
                          if step["action"] == "network_detach" and not step["ok"]), None)
    session_mod.save(record)
    if failed_detach:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, "Could not restore the device proxy; session kept open.",
            "Resolve the detach error and retry session stop; the proxy was left running.",
            teardown=teardown)
    # Use the same route guard as CLI/Canvas Stop. No generic process reaping
    # may kill a proxy still needed by the target.
    from autonom_lib.network import attachment as attachment_mod
    from autonom_lib.network import proxy as proxy_mod

    if not gone and attachment_mod.transparent_route_live(record):
        network_stop_payload(record)  # raises the transparent-route refusal
    # The device is restored by now: a proxy that fails to stop is reported,
    # and the reaping below stays its best-effort backstop.
    teardown.extend(session_mod.run_teardown([
        ("network_stop", lambda: (proxy_mod.stop(record) if gone else
                                  network_stop_payload(record, lambda: _target(args)))),
    ]))
    teardown.extend(session_mod.run_teardown([
        ("session_processes", lambda: session_mod.reap_owned_processes(record)),
    ]))
    if accessibility_teardown is not None:
        teardown.insert(0, {"action": "accessibility_restore", "ok": True,
                            "detail": accessibility_teardown})
    reaped: dict[str, Any] = next(
        (item.get("detail") or {} for item in teardown
         if item["action"] == "session_processes" and item.get("ok")), {})
    if (reaped.get("terminated") or reaped.get("companion_left_running")
            or reaped.get("group_remnants")):
        # kept on the stopped record, as the library's own reap keeps it
        current = session_mod.load_current()
        if current:
            current["process_teardown"] = reaped
            session_mod.save(current)

    # the list above already reaped the session's processes; the session
    # stopped is the one this command is bound to (--session-id, a target
    # flag naming a live session, else current.json)
    stopped = session_mod.stop_session(reap=False, session_id=record.get("session_id"))
    payload: dict[str, Any] = {"ok": True, "session": stopped, "teardown": teardown,
                               "started_by": (stopped or record).get("started_by")}
    warnings: list[dict[str, Any]] = []
    if gone:
        target_id = record.get("target_id") or record.get("serial")
        payload["stale_target"] = True
        warnings.append({
            "code": "stale_target",
            "error": f"the session's target {target_id} is no longer present; "
                     "the session was cleared without device-side teardown",
            "hint": "Pick a running target with 'autonom devices', then "
                    "'autonom session start'.",
        })
    for name, outcome in verification.items():
        if outcome.get("result") == "unverified_skipped":
            what = "log-stream writer" if name == "log_stream" else "screen recorder"
            warnings.append({
                "code": "unverified_skipped",
                "error": f"the session's {what} (pid {outcome['pid']}) was not "
                         "signalled: ps could not confirm it is still that process",
                "hint": f"Inspect it with 'ps -p {outcome['pid']}' and stop it yourself "
                        "only if it is yours.",
            })
    if any(outcome.get("result") in ("pid_reused", "unverified_skipped")
           for outcome in verification.values()):
        payload["teardown_verification"] = verification
    companions = reaped.get("companion_left_running") or []
    if companions:
        udid = record.get("target_id")
        warnings.append({
            "code": "companion_left_running",
            "error": f"{len(companions)} idb_companion process(es) for {udid} were not "
                     "started by this session and were left running",
            "hint": "Stop them with kill <pid> if nothing else uses the simulator.",
            "companions": companions,
        })
    remnants = reaped.get("group_remnants") or []
    if remnants:
        warnings.append({
            "code": "group_remnant_left_running",
            "error": f"{len(remnants)} process group(s) recorded for this session lost "
                     "their leader and were left running; they may not be Autonom's",
            "hint": remnants[0].get("hint"),
            "group_remnants": remnants,
        })
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_session_show(_: argparse.Namespace) -> int:
    record = session_mod.load_current()
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION,
            "no active session",
            "Start one with 'autonom session start'.",
        )
    return emit({"ok": True, "session": record}, as_json=True)


def cmd_session_launch(args: argparse.Namespace) -> int:
    target = _target(args)
    warnings: list[dict[str, str]] = []
    if target.platform == IOS:
        if args.activity:
            warnings.append(_ignored_flag_warning(
                "--activity", IOS, "Activities are Android components; iOS "
                "launches by bundle id."))
        env = dict(pair.split("=", 1) for pair in (args.setenv or []) if "=" in pair)
        # When the session is attached to a proxy, launching without the proxy
        # environment would silently produce an uncaptured run. The session is
        # the one this command is bound to (`_select_session`): with --udid X
        # that is the live session on X, not necessarily current.json's.
        from autonom_lib.network import device_proxy_ios as _ios_proxy

        current = session_mod.load_current()
        if current:
            for key, value in _ios_proxy.launch_environment(current).items():
                env.setdefault(key, value)
        if getattr(args, "fresh", False):
            ios_simctl.terminate(target.tool, target.target_id, args.app_id)
        pid = ios_simctl.launch(
            target.tool, target.target_id, args.app_id, args=args.arg or [], env=env
        )
        payload: dict[str, Any] = {
            "ok": True, "launched": args.app_id, "pid": pid,
            "mode": "fresh" if getattr(args, "fresh", False) else "resume",
            **target.identity()}
        if warnings:
            payload["warnings"] = warnings
        return emit(payload, as_json=True)
    for flag, value in (("--arg", args.arg), ("--setenv", args.setenv)):
        if value:
            warnings.append(_ignored_flag_warning(
                flag, ANDROID, "Launch arguments and environment are iOS "
                "(simctl launch) only; Android starts the activity as declared."))
    if getattr(args, "fresh", False):
        # `--fresh --activity` used to fall back to a resume of that activity.
        detail = session_mod.launch_app_fresh(target.tool, target.target_id, args.app_id,
                                              activity=args.activity)
        payload = {"ok": True, "launched": args.app_id, **detail, **target.identity()}
    else:
        # `am start -W` on the launcher activity (monkey only as the
        # fallback): mode, component, and the launch report
        detail = session_mod.launch_app(target.tool, target.target_id, args.app_id,
                                        activity=args.activity)
        payload = {"ok": True, "launched": args.app_id, **detail, **target.identity()}
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_session_stop_app(args: argparse.Namespace) -> int:
    target = _target(args)
    if target.platform == IOS:
        was_running = ios_simctl.terminate(target.tool, target.target_id, args.app_id)
        return emit(
            {"ok": True, "stopped": args.app_id, "was_running": was_running, **target.identity()},
            as_json=True,
        )
    session_mod.force_stop(target.tool, target.target_id, args.app_id)
    return emit({"ok": True, "stopped": args.app_id, **target.identity()}, as_json=True)


def cmd_session_clear(args: argparse.Namespace) -> int:
    target = _target(args)
    if target.platform == ANDROID:
        try:
            session_mod.clear_data(target.tool, target.target_id, args.app_id)
        except errors.AutonomError as exc:
            if not exc.hint:
                exc.hint = (f"Check that {args.app_id} is installed ('adb shell pm list "
                            "packages') and that the device is online ('autonom devices').")
            raise
        payload: dict[str, Any] = {"ok": True, "cleared": args.app_id, **target.identity()}
        if args.strategy != "auto":
            payload["warnings"] = [_ignored_flag_warning(
                "--strategy", ANDROID, "Android clears app data with 'pm clear'; "
                "the reinstall/privacy strategies are iOS-only.")]
        return emit(payload, as_json=True)

    record = session_mod.load_current() or {}
    strategy = args.strategy
    install_path = record.get("install_path")
    if strategy == "auto":
        strategy = "reinstall" if install_path else "unavailable"
    if strategy == "reinstall":
        if not install_path:
            raise errors.AutonomError(
                errors.IOS_CLEAR_REQUIRES_INSTALL_PATH,
                "iOS has no 'pm clear'; a full reset needs the original .app path",
                "Re-run 'session start --install <path>.app', or use "
                "'session clear <bundle> --strategy privacy' to reset permissions only.",
            )
        ios_simctl.uninstall(target.tool, target.target_id, args.app_id, check=True)
        ios_simctl.install(target.tool, target.target_id, Path(install_path))
        payload = {"ok": True, "cleared": args.app_id, "strategy": "reinstall",
                   **target.identity()}
        # the reinstall may bring another build, whose binary UUIDs the
        # session has not recorded: stop the old writer (only once it is
        # shown to be this session's) and start a new one, which records them
        restart = simulator_mod.restart_session_log_stream(target, cause="reinstall",
                                                           stop_previous=True)
        if restart.get("warnings"):
            payload["warnings"] = restart.pop("warnings")
        payload.update(restart)
        return emit(payload, as_json=True)
    if strategy == "privacy":
        ios_simctl.privacy(target.tool, target.target_id, "reset", "all", args.app_id)
        return emit(
            {
                "ok": True,
                "cleared": args.app_id,
                "strategy": "privacy",
                "scope": "permissions_only",
                "note": "Application data was NOT cleared; only privacy permissions were reset.",
                **target.identity(),
            },
            as_json=True,
        )
    raise errors.AutonomError(
        errors.IOS_CLEAR_REQUIRES_INSTALL_PATH,
        "no install path recorded for this session and no strategy given",
        "Use --strategy privacy, or start the session with --install <path>.app.",
    )


def cmd_session_uninstall(args: argparse.Namespace) -> int:
    target = _target(args)
    if target.platform == IOS:
        # the simctl return code used to be dropped: every uninstall was ok
        ios_simctl.uninstall(target.tool, target.target_id, args.app_id, check=True)
    else:
        completed = adb_mod.run_adb(target.tool, ["uninstall", args.app_id],
                                    serial=target.target_id, check=False)
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        failure = next((line.strip() for line in output.splitlines()
                        if line.strip().startswith("Failure")), None)
        if completed.returncode != 0 or failure:
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                failure or f"adb uninstall {args.app_id} failed ({completed.returncode})",
                "DELETE_FAILED_INTERNAL_ERROR usually means the package is not "
                "installed; check with 'adb shell pm list packages'.",
                app_id=args.app_id,
            )
    return emit({"ok": True, "uninstalled": args.app_id, **target.identity()}, as_json=True)


# --- ui ----------------------------------------------------------------------


def _read_dump(dump: str) -> str:
    """An offline UI dump; a missing or unreadable file is one clean envelope."""
    path = Path(dump).expanduser()
    if not path.is_file():
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--dump {dump}: no such file",
            "Pass a uiautomator XML or an idb describe-all JSON file.",
            path=str(path),
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--dump {dump}: {exc}",
            "Pass a readable UTF-8 uiautomator XML or idb describe-all JSON file.",
            path=str(path),
        ) from exc


def _dump_platform(text: str) -> str:
    return IOS if text.lstrip()[:1] in "{[" else ANDROID


def _snapshot(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str, str, Target | None]:
    """All on-screen nodes plus provenance: (nodes, source, target label, target)."""
    dump = getattr(args, "dump", None)
    if dump:
        text = _read_dump(dump)
        # marked like a live snapshot: a node that cannot be on screen
        # carries visible: false, whatever file it came from
        if _dump_platform(text) == IOS:
            from autonom_lib import ui_ios

            return selector_mod.mark_visibility(ui_ios.parse_all(text)), "file", dump, None
        from autonom_lib import ui_android

        return selector_mod.mark_visibility(ui_android.parse_all(text)), "file", dump, None
    target = _target(args)
    return ui_mod.snapshot(target), "device", target.target_id, target


def _recovery_admitted(target: Target) -> bool:
    """The control gate of the implicit accessibility recovery of `ui tree`
    (contract 5.4): a read-only verb that would write secure settings,
    force-stop and relaunch the app. Under a lease this invocation may not
    use, the recovery is skipped (the tree and its sparse warning stay);
    without a lease file and without a claim it runs as before. A lease file
    that cannot be read never counts as free."""
    try:
        lease = control_mod.read_lease(target.platform, target.target_id)
    except errors.AutonomError:
        return False
    claim = control_mod.invocation().get("claim")
    return control_mod.admission(lease, claim, control_mod._now_ms()) is None  # noqa: SLF001


def cmd_ui_tree(args: argparse.Namespace) -> int:
    dump = getattr(args, "dump", None)
    warnings: list[dict[str, Any]] = []
    recovery: dict[str, Any] | None = None
    if args.max_nodes is not None and args.max_nodes < 1:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--max-nodes must be >= 1, got {args.max_nodes}",
            "Omit it for the default of 200.")
    # one node past the limit is fetched, so `truncated` is exact: a screen of
    # exactly --max-nodes nodes is complete, not cut
    fetch = None if args.max_nodes is None else args.max_nodes + 1
    if dump:
        text = _read_dump(dump)
        if _dump_platform(text) == IOS:
            from autonom_lib import ui_ios

            nodes, warnings = ui_ios.parse_tree(
                text, meaningful_only=not args.all, max_depth=args.max_depth,
                max_nodes=fetch,
            )
            # as a live tree is marked: the meaningful filter may drop an
            # unlabelled application root, so the screen rectangle can come
            # from the whole dump instead
            view = (selector_mod.viewport(nodes)
                    or selector_mod.viewport(ui_ios.parse_all(text)))
            nodes = selector_mod.mark_visibility(nodes, view)
            identity = {"platform": IOS}
        else:
            nodes = ui_mod.parse_compact_tree(
                text, meaningful_only=not args.all, max_depth=args.max_depth,
                max_nodes=fetch,
            )
            identity = {"platform": ANDROID}
        nodes, truncated = ui_mod.clip(nodes, args.max_nodes)
        source, label, target = "file", dump, None
    else:
        target = _target(args)
        nodes, warnings, truncated = ui_mod.tree_clipped(
            target, meaningful_only=not args.all, max_depth=args.max_depth,
            max_nodes=args.max_nodes,
        )
        source, label = "device", target.target_id
        identity = target.identity()
        if target.platform == ANDROID and not truncated and accessibility_mod.sparse(nodes):
            warnings.append({
                "code": "sparse_accessibility_tree",
                "error": "Android exposed only unlabelled containers on this screen",
                "hint": "If the screenshot shows Flutter controls, the app may need an "
                        "accessibility service enabled before it starts. Enable a "
                        "service on the test emulator, force-stop and relaunch the app, "
                        "then read the tree again. Restore the emulator's original "
                        "accessibility settings when testing is done.",
            })
            current = session_mod.load_current()
            if (not getattr(args, "no_accessibility_recovery", False)
                    and args.max_depth is None and (args.max_nodes is None or args.max_nodes >= 8)
                    and accessibility_mod.eligible(target, current, nodes)
                    and not current.get("accessibility")):
                try:
                    # A depth/filter limit can hide labels. Confirm against an
                    # unfiltered second dump before touching device settings.
                    whole = ui_mod.snapshot(target)
                    if accessibility_mod.eligible(target, current, whole) and \
                            accessibility_mod.flutter_shell(whole) and \
                            accessibility_mod.foreground_app(target) == current["app_id"] and \
                            _recovery_admitted(target):
                        accessibility_mod.enable(target, current)
                        from autonom_lib.pretest import guard as pretest_guard

                        # The relaunch belongs to this read-only verb, not to
                        # the agent: a pre-test neither counts nor refuses it,
                        # so the app is never left force-stopped (contract §9).
                        with pretest_guard.exempt():
                            session_mod.force_stop(target.tool, target.target_id,
                                                   current["app_id"])
                            launch = session_mod.launch_app(target.tool, target.target_id,
                                                            current["app_id"])
                        for attempt in range(3):
                            nodes, new_warnings, truncated = ui_mod.tree_clipped(
                                target, meaningful_only=not args.all, max_depth=args.max_depth,
                                max_nodes=args.max_nodes)
                            if not accessibility_mod.sparse(nodes):
                                break
                            if attempt < 2:
                                time.sleep(0.6)
                        warnings.extend(new_warnings)
                        warnings = [item for item in warnings
                                    if item.get("code") != "sparse_accessibility_tree"]
                        if accessibility_mod.sparse(nodes):
                            accessibility_mod.restore(target, current)
                            warnings.append({"code": "sparse_accessibility_tree",
                                             "error": "tree remains sparse after accessibility recovery",
                                             "hint": "Original accessibility settings were restored. "
                                                     "Check the current screenshot and app semantics."})
                        recovery = {"attempted": True,
                                    "recovered": not accessibility_mod.sparse(nodes),
                                    "settings_restored": accessibility_mod.sparse(nodes),
                                    "service": accessibility_mod.SERVICE,
                                    "launch": launch}
                except errors.AutonomError as exc:
                    if current.get("accessibility"):
                        try:
                            accessibility_mod.restore(target, current)
                        except errors.AutonomError:
                            pass  # snapshot stays in the session for explicit reset
                    warnings.append({"code": "accessibility_recovery_failed",
                                     "error": exc.message,
                                     "hint": "Inspect the emulator and run "
                                             "'autonom ui accessibility reset' if enabled."})
    if getattr(args, "interactable", False):
        nodes = ui_mod.interactable(nodes)

    payload: dict[str, Any] = {
        "ok": True,
        "source": source,
        "target": label,
        "count": len(nodes),
        "nodes": nodes,
        **identity,
    }
    # `--max-nodes` used to cut the list silently; an agent reading 5 nodes
    # could not tell a 5-node screen from a 200-node one. Cheap and honest.
    if truncated:
        payload["truncated"] = True
    if not dump:
        # The coordinate space the bounds live in (pixels on Android, points
        # on iOS) — the same number the tap guard checks against. Android's
        # `wm size` is one cheap call; on iOS the tree's own root frame is
        # used, since the alternative is a second full accessibility dump.
        size = None
        if target.platform == ANDROID:
            try:
                size = ui_mod.screen_size(target)
            except errors.AutonomError:
                size = None
        else:
            size = ui_mod.screen_from_nodes(nodes)
        if size:
            payload["screen"] = list(size)
    if warnings:
        payload["warnings"] = warnings
    if recovery is not None:
        payload["accessibility_recovery"] = recovery
    current = session_mod.load_current()
    if current and not dump:
        # While a pre-test is active the saved tree names it, and an open
        # check doubles as the tree's task (tp-004). Without one: no new keys.
        attribution = shot_mod.pretest_attribution(current)
        if attribution:
            payload.update(attribution)
            if attribution.get("check_id"):
                payload["task"] = attribution["check_id"]
        # Keep the full history, not just the latest: a run's trees are evidence
        # of how the screen changed. `latest.json` stays as a stable pointer.
        # The saved tree records how it was read (the reply does not change):
        # observe compares only with a tree read the way it reads (AL-11).
        shape = observe_mod.read_shape(
            observe_mod.SHAPE_UI_TREE, all_nodes=bool(args.all),
            interactable=bool(getattr(args, "interactable", False)),
            max_depth=args.max_depth, max_nodes=args.max_nodes, truncated=truncated)
        payload["saved"] = observe_mod.save_tree(current, {**payload, "read_shape": shape})
    # the profile of the session this read belongs to (for a dump, the
    # invocation's session: there is no target to match)
    owner = current if dump or observe_mod.owns(current, target) else None
    if _tree_format(args, owner) == "outline":
        # One line per node for a token-cheap read; the saved history above
        # keeps the full nodes, the printed envelope carries the outline.
        payload["format"] = "outline"
        payload["outline"] = ui_mod.outline(nodes)
        payload.pop("nodes", None)
    return emit(payload, as_json=True)


def _tree_format(args: argparse.Namespace, record: dict[str, Any] | None) -> str:
    """`ui tree --format`: as given; without it `outline` in an agent-profile
    session (AL-09), `json` otherwise (and for every older session)."""
    chosen = getattr(args, "format", None)
    if chosen:
        return chosen
    return "outline" if session_mod.profile_of(record) == session_mod.AGENT_PROFILE else "json"


def cmd_ui_accessibility(args: argparse.Namespace) -> int:
    target = _target(args)
    record = session_mod.load_current()
    if args.action == "status":
        detail = accessibility_mod.status(target, record)
    elif not record:
        raise errors.AutonomError(errors.NO_ACTIVE_SESSION,
                                  "an active session is required")
    elif args.action == "enable":
        detail = accessibility_mod.enable(target, record)
    else:
        detail = accessibility_mod.restore(target, record)
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_ui_wait(args: argparse.Namespace) -> int:
    """`ui wait --settled`: wait until the screen stops changing, bounded.

    On the iOS idb route idb is asked whether the app is quiet, then the tree
    is read once (`method: idb-quiet`); elsewhere, or when idb cannot answer,
    the tree is polled until two consecutive reads match (`method: poll`).
    Mirrors the other bounded checks (`flow fmt --check`, `doctor --strict`):
    the report is on stdout either way, and a screen that never settled
    within --timeout-ms exits 1 with `settled: false`.
    """
    if not args.settled:
        raise errors.AutonomError(
            errors.USAGE_ERROR, "ui wait needs a condition",
            "Pass --settled (wait until the screen holds still for --quiet-ms).")
    for flag, value in (("--timeout-ms", args.timeout_ms), ("--quiet-ms", args.quiet_ms)):
        if value < 0:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"{flag} must be >= 0, got {value}",
                "Pass a duration in milliseconds.")
    target = _target(args)
    result = ui_mod.settle(target, timeout_ms=args.timeout_ms, quiet_ms=args.quiet_ms)
    payload: dict[str, Any] = {"ok": bool(result["settled"]), **result,
                               "timeout_ms": args.timeout_ms, "quiet_ms": args.quiet_ms,
                               **target.identity()}
    if not result["settled"]:
        # "still changing" only when a change was seen; one slow dump in the
        # budget (or identical dumps short of --quiet-ms) cannot confirm either
        error, hint = ui_mod.unsettled_message(result, args.timeout_ms, args.quiet_ms)
        payload["warnings"] = [{"code": "screen_not_settled", "error": error, "hint": hint}]
    emit(payload, as_json=True)
    return 0 if result["settled"] else 1


def cmd_note_add(args: argparse.Namespace) -> int:
    if not (args.text or "").strip():
        raise errors.AutonomError(
            errors.INVALID_VALUE, "an empty note records nothing",
            "Write what you observed or concluded: autonom note add \"...\".")
    session = session_mod.require_current()
    entry = journal_mod.note(
        session, args.text, task=getattr(args, "task", None),
        tags=getattr(args, "tag", None), author=args.author,
    )
    return emit({"ok": True, "note": entry, "session_id": session.get("session_id")}, as_json=True)


def cmd_note_list(args: argparse.Namespace) -> int:
    session = session_mod.require_current()
    entries, total = journal_mod.read(
        session, kind="note", task=getattr(args, "task", None),
        grep=getattr(args, "grep", None), max_entries=args.max,
    )
    return emit({"ok": True, "count": len(entries), "total_matched": total,
                 "truncated": total > len(entries), "notes": entries}, as_json=True)


def _stream_print(payload: Any) -> None:
    """One NDJSON line per event — the documented exception to the one-JSON-doc
    rule, shared with `flow run --events` (docs/CAPABILITIES.md)."""
    if isinstance(payload, str):
        print(payload, flush=True)
    else:
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def _finish_stream(eof: dict[str, Any]) -> int:
    """Every streaming verb ends here: the eof summary — not the line spam —
    becomes the journal payload."""
    global _LAST_EMIT
    _LAST_EMIT = {"ok": True, **eof}
    return 0


def _observed_session(args: argparse.Namespace) -> dict[str, Any]:
    session_id = getattr(args, "session_id", None)
    return _session_by_id(session_id) if session_id else session_mod.require_current()


def cmd_session_outputs(args: argparse.Namespace) -> int:
    record = _observed_session(args)
    streams = follow_mod.catalog(record)
    return emit({"ok": True, "session_id": record.get("session_id"),
                 "artifacts_dir": record.get("artifacts_dir"),
                 "count": len(streams), "streams": streams}, as_json=True)


def _follow_device(args: argparse.Namespace) -> dict[str, Any]:
    target = _target(args)
    if target.platform == ANDROID:
        # starts at the device's now (`logcat -T`) unless --from-start; with
        # --package, the app's uid on API 31+ (plus the lifecycle lines
        # naming it), else its running pid, else lines naming it — and says
        # which, up front. An unknown package is app_not_installed.
        plan = logs_mod.android_follow_plan(target.tool, target.target_id, args.package,
                                            from_start=args.from_start)
        return follow_mod.follow_processes(
            plan["streams"], source="device", emit=_stream_print,
            max_seconds=args.max_seconds, max_lines=args.max_lines, grep=args.grep,
            warnings=plan["warnings"],
            detail={"filter": plan["filter"]} if plan["filter"] else None)

    # iOS: a named past session can only replay its recorded stream file; the
    # current session's file is preferred only while its writer is alive —
    # a dead stream must not shadow live device logs.
    record = (_session_by_id(args.session_id) if args.session_id
              else session_mod.load_current())
    stream = (Path(record["artifacts_dir"]) / "logs" / "stream.ndjson"
              if record else None)
    # the session's install path helps resolve the app's image, when the
    # package followed is the session's own app
    app_path = (record.get("install_path")
                if record and args.package and record.get("app_id") == args.package
                else None)
    if args.session_id:
        if stream is None or not stream.exists():
            raise errors.AutonomError(
                errors.STREAM_NOT_FOUND,
                f"session {args.session_id} recorded no device log stream",
                "Only sessions started with --log-stream have one; live "
                "device logs belong to the current target, not a past session.",
            )
        writer_alive = True  # replaying a past recording, liveness irrelevant
        args.from_start = True  # a recording is read, not awaited
        # the file holds every same-named app's records: the session's own
        # app is matched by the identity the stream recorded (executable,
        # binary UUIDs) — it may have moved, been replaced or be gone since;
        # any other package is resolved from its installed binary; with no
        # package, the session's own app
        line_filter = (logs_mod.recorded_line_filter(record, args.package, target=target)
                       if args.package else
                       logs_mod.session_stream_filter(record, stream)
                       or logs_mod.recorded_line_filter(record, None))
    else:
        pid = (record or {}).get("background", {}).get("log_stream_pid")
        writer_alive = session_mod.pid_alive(pid)
        line_filter = None
    if stream is not None and stream.exists() and writer_alive:
        if line_filter is None:
            # the installed binary's UUIDs plus those the session recorded;
            # with no package, the session's own app
            line_filter = (logs_mod.ios_line_filter(target, args.package, app_path=app_path,
                                                    record=record)
                           if args.package else
                           logs_mod.session_stream_filter(record, stream, target=target)
                           or logs_mod.ios_line_filter(target))
        return follow_mod.follow_file(
            stream, source="device", emit=_eof_detail(line_filter),
            from_start=args.from_start, max_seconds=args.max_seconds,
            max_lines=args.max_lines, grep=args.grep,
            poll_ms=args.poll_ms, line_filter=line_filter)
    # a live `log stream`: the predicate names the app's executable, which
    # every same-named app (every Flutter `Runner`) shares, so the line
    # filter keeps the records of the app's binary UUIDs — and drops the
    # `log` tool's own banner and trailer
    argv = logs_mod.ios_follow_argv(target, args.package, app_path=app_path)
    line_filter = logs_mod.ios_line_filter(target, args.package, app_path=app_path,
                                           record=record)
    return follow_mod.follow_process(
        argv, source="device", emit=_eof_detail(line_filter),
        max_seconds=args.max_seconds, max_lines=args.max_lines, grep=args.grep,
        line_filter=line_filter)


def _eof_detail(line_filter: Any) -> Any:
    """`_stream_print`, with the binary UUIDs an iOS line filter matched on
    added to the eof line (``image_uuids``, sorted) when there were any."""
    uuids = logs_mod.filter_image_uuids(line_filter)
    if not uuids:
        return _stream_print

    def emit_line(payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("kind") == "eof":
            payload.setdefault("image_uuids", uuids)
        _stream_print(payload)

    return emit_line


def cmd_logs_follow(args: argparse.Namespace) -> int:
    if args.source == "device":
        return _finish_stream(_follow_device(args))
    record = _observed_session(args)
    base = Path(record["artifacts_dir"])
    if args.path:
        path = follow_mod.confine(base, args.path)
        source = args.path
    elif args.source:
        path = follow_mod.resolve_source(record, args.source)
        source = args.source
    elif follow_mod.default_source(record):
        # the session's only device-log stream: nothing to choose between
        source = follow_mod.default_source(record)
        path = follow_mod.resolve_source(record, source)
    else:
        raise errors.AutonomError(
            errors.STREAM_NOT_FOUND,
            "nothing to follow: pass --source or --path",
            "List followable streams with 'autonom session outputs', or "
            "use --source device for the device log.",
        )
    # An iOS session's own `--log-stream` file (or its `.1` rotation) holds
    # every same-named app's raw records (every Flutter `Runner`): read it as
    # the session's app. Any other file is followed as it is.
    line_filter = logs_mod.session_stream_filter(
        record, path, target=_session_target_or_none(args, record))
    return _finish_stream(follow_mod.follow_file(
        path, source=source, emit=_eof_detail(line_filter),
        from_start=args.from_start, max_seconds=args.max_seconds,
        max_lines=args.max_lines, grep=args.grep, poll_ms=args.poll_ms,
        line_filter=line_filter))


def _session_target_or_none(args: argparse.Namespace, record: dict[str, Any]) -> Any:
    """The current session's own iOS simulator, to resolve its app from the
    installed binary; None for a past session (`--session-id`), a session
    that is not iOS or has no app, or a target that cannot be resolved (the
    stream's recorded identity is used then)."""
    if (getattr(args, "session_id", None) or record.get("platform") != IOS
            or not record.get("app_id")):
        return None
    try:
        target = _target(args)
    except errors.AutonomError:
        return None
    return target if target.target_id == record.get("target_id") else None


def cmd_journal(args: argparse.Namespace) -> int:
    # --session-id reads a finished session's journal, with or without --follow
    session = (_session_by_id(args.session_id) if getattr(args, "session_id", None)
               else session_mod.load_current())
    if getattr(args, "follow", False):
        record = _observed_session(args)
        return _finish_stream(follow_mod.follow_file(
            journal_mod.journal_path(record), source="journal",
            emit=_stream_print, raw=True, from_start=args.from_start,
            max_seconds=args.max_seconds, max_lines=args.max_lines,
            grep=args.grep))
    if not session:
        return emit({"ok": True, "count": 0, "total_matched": 0, "truncated": False,
                     "entries": [], "note": "no active session"}, as_json=True)
    entries, total = journal_mod.read(
        session, kind=getattr(args, "kind", None), verb=getattr(args, "verb", None),
        task=getattr(args, "task", None), grep=getattr(args, "grep", None),
        max_entries=args.max,
    )
    return emit({"ok": True, "count": len(entries), "total_matched": total,
                 "truncated": total > len(entries), "entries": entries,
                 "journal": str(journal_mod.journal_path(session))}, as_json=True)


def cmd_ui_find(args: argparse.Namespace) -> int:
    ui_mod.require_selector(_selectors(args))
    _check_selector_regex(args)
    nodes, source, label, target = _snapshot(args)
    # without --all: the node `ui tap` would act on (one resolution core,
    # on-screen matches first); --all lists every match in the order --index
    # counts them, each with the `index` that selects it. Off-screen matches
    # are marked visible: false either way, dumps included.
    matches = ui_mod.select_for_find(
        nodes,
        _selectors(args),
        mode=args.mode,
        case_sensitive=args.case_sensitive,
        index=args.index,
        all_matches=args.all,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "source": source,
        "target": label,
        "count": len(matches),
        "matches": matches,
    }
    evidence_warning = None
    if target:
        payload.update(target.identity())
        detail, evidence_warning = actions_mod.record_detail_checked(
            session_mod.load_current(), "find", {
            "kind": "find",
            "selector": {
                **{k: v for k, v in _selectors(args).items() if v is not None},
                "mode": args.mode, "case_sensitive": args.case_sensitive,
                "index": args.index,
            },
            "count": len(matches),
        })
        if detail:
            payload["detail"] = detail
    if not matches and args.resource_id and not any(node.get("resource_id") for node in nodes):
        payload["warnings"] = [{
            "code": "no_accessibility_identifiers",
            "error": "no element on this screen exposes an identifier",
            "hint": "Select by --text or --desc instead.",
        }]
    if not matches:
        platform = (target.platform if target else
                    _dump_platform(_read_dump(args.dump)) if getattr(args, "dump", None)
                    else None)
        hint = ui_mod.no_match_hint(platform or "", nodes, _selectors(args),
                                    mode=args.mode, case_sensitive=args.case_sensitive)
        if hint:
            payload.setdefault("warnings", []).append({
                "code": "label_is_in_desc",
                "error": "--text matched nothing, but the same value matches desc",
                "hint": hint,
            })
    if evidence_warning:
        payload.setdefault("warnings", []).append(evidence_warning)
    return emit(payload, as_json=True)


def _backend(returned: Any) -> str | None:
    """Which backend delivered the input: the ui_mod return value, else what
    ui_ios recorded for the last HID verb (None when nothing reported one)."""
    if isinstance(returned, str) and returned:
        return returned
    from autonom_lib import ui_ios

    return ui_ios.last_backend()


class _ObservePlan:
    """Whether an action verb reports the screen it leads to (AL-11), and
    how: ``--observe`` / ``--no-observe`` win, else the session's profile
    decides; the budget is checked before any input is sent."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.timeout_ms = observe_mod.check_timeout(getattr(args, "observe_timeout_ms", None))
        self.flag = getattr(args, "observe", None)
        self.record: dict[str, Any] | None = None
        self.on = False
        self.mark: Any = None

    def resolve(self, target: Target) -> "_ObservePlan":
        """Bind to the session of ``target`` once the target is known, and
        mark where this thread stands, so the read this command holds when
        its input goes out can be told from an earlier command's."""
        self.record = observe_mod.session_for(target)
        self.on = observe_mod.wanted(self.flag, self.record)
        self.mark = observe_mod.read_mark(target)
        return self

    def into(self, payload: dict[str, Any], target: Target, verb: str,
             secret: str | None = None) -> dict[str, Any]:
        """Add what observe saw to ``payload`` (nothing when observe is off).
        The baseline of the diff is the read this command held when its
        input went out (a selector tap's read, `ui type`'s focus read, the
        iOS guard's `describe-all` lookup), noted right before it was sent;
        no read is made for it. An interrupt that ended observing is
        remembered for `main`, which exits 130 once this reply is printed
        and journaled."""
        global _OBSERVE_INTERRUPTED
        if not self.on:
            return payload
        keys, warnings = observe_mod.after_action(
            target, self.record, verb=verb,
            baseline_payload=observe_mod.own_read(target, since=self.mark),
            timeout_ms=self.timeout_ms, secret=secret)
        if observe_mod.interrupted(keys):
            _OBSERVE_INTERRUPTED = True
        payload.update(keys)
        if warnings:
            payload.setdefault("warnings", []).extend(warnings)
        return payload


def cmd_ui_tap(args: argparse.Namespace) -> int:
    has_selector = any(value is not None for value in _selectors(args).values())
    has_x, has_y = args.x is not None, args.y is not None
    if has_selector and (has_x or has_y):
        raise errors.AutonomError(
            errors.USAGE_ERROR,
            "ui tap takes a selector or --x/--y, not both",
            "Tap a node by --desc/--text/--resource-id, or a point by --x and --y.",
        )
    if has_x != has_y:
        raise errors.AutonomError(
            errors.USAGE_ERROR,
            "--x and --y go together",
            "Pass both --x and --y for a point, or a selector for a node.",
        )
    _check_selector_regex(args)
    plan = _ObservePlan(args)
    target = _target(args)
    plan.resolve(target)
    ref = None
    node = None
    nodes: list[dict[str, Any]] = []
    if has_x and has_y:
        x, y = args.x, args.y
    else:
        if not has_selector:
            # An empty selector matches every node, which used to surface as
            # "ambiguous_selector: matched 78 nodes" — true, but not the point.
            raise errors.AutonomError(
                errors.SELECTOR_REQUIRED,
                "ui tap needs a selector (--text/--desc/--resource-id/...) or --x/--y",
                "Run 'autonom ui tree', then tap by --desc/--text/--resource-id, or "
                "pass --x and --y for a point.",
            )
        nodes = ui_mod.snapshot(target)
        # resolved exactly as `ui find` resolves it: on-screen matches first,
        # counted the way a flow counts; a node never laid out on screen is
        # refused with element_offscreen
        node = ui_mod.select_for_action(
            nodes,
            _selectors(args),
            mode=args.mode,
            case_sensitive=args.case_sensitive,
            index=args.index,
        )
        if node is None:
            # carries the "the label is in desc" hint when it applies
            raise ui_mod.no_match_error(
                target.platform, nodes, _selectors(args), mode=args.mode,
                case_sensitive=args.case_sensitive, message="no matching node to tap")
        x, y = ui_mod.center_of(node)
        ref = node.get("ref")
    duration = getattr(args, "duration", None)
    if duration:
        backend = _backend(ui_mod.long_press(target, x, y, duration))
    else:
        backend = _backend(ui_mod.tap(target, x, y, node=node))
    detail, evidence_warning = actions_mod.record_detail_checked(
        session_mod.load_current(), "tap", {
        "kind": "tap",
        "coordinate": node is None,
        "selector": None if node is None else {
            **{k: v for k, v in _selectors(args).items() if v is not None},
            "mode": args.mode, "case_sensitive": args.case_sensitive,
            "index": args.index,
        },
        "node": node,
        "x": x, "y": y,
        "duration_ms": duration,
        "nodes": nodes,
    })
    payload: dict[str, Any] = {"ok": True, "x": x, "y": y, "ref": ref}
    if backend:
        payload["backend"] = backend
    if duration:
        payload["duration_ms"] = duration
    if detail:
        payload["detail"] = detail
    if evidence_warning:
        payload.setdefault("warnings", []).append(evidence_warning)
    plan.into(payload, target, "ui tap")
    return emit({**payload, **target.identity()}, as_json=True)


# `ui swipe --duration` is in seconds; a value past this is a millisecond
# count by mistake (400 would hold the finger for almost seven minutes).
SWIPE_MAX_SECONDS = 60.0


def _swipe_duration(duration: float) -> float:
    """`--duration` of `ui swipe`: finite seconds in (0, SWIPE_MAX_SECONDS].
    NaN, infinities, zero and negative values are refused like a value in
    milliseconds, before anything reaches the device (B.2)."""
    if not math.isfinite(duration) or not 0 < duration <= SWIPE_MAX_SECONDS:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--duration {duration:g} is not a swipe length: it is in seconds, more than 0 "
            f"and at most {SWIPE_MAX_SECONDS:g}",
            "--duration is seconds, e.g. 0.4 (not milliseconds).",
            duration=duration if math.isfinite(duration) else str(duration),
            max_seconds=SWIPE_MAX_SECONDS)
    return duration


def cmd_ui_swipe(args: argparse.Namespace) -> int:
    _swipe_duration(args.duration)
    x1, y1 = _point(args.start, "--from")
    x2, y2 = _point(args.end, "--to")
    plan = _ObservePlan(args)
    target = _target(args)
    plan.resolve(target)
    backend = _backend(ui_mod.swipe(target, x1, y1, x2, y2, args.duration))
    payload: dict[str, Any] = {"ok": True, "from": [x1, y1], "to": [x2, y2],
                               "duration": args.duration}
    if backend:
        payload["backend"] = backend
    plan.into(payload, target, "ui swipe")
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_ui_gesture(args: argparse.Namespace) -> int:
    target = _target(args)
    kwargs: dict[str, Any] = {}
    if args.gesture == "pinch":
        if args.at is None:
            raise errors.AutonomError(
                errors.INVALID_COORDINATES, "pinch requires --at X,Y", "Example: --at 200,400"
            )
        x, y = _point(args.at, "--at")
        kwargs = {"x": x, "y": y, "scale": args.scale}
    ui_mod.gesture(target, args.gesture, **kwargs)
    return emit({"ok": True, "gesture": args.gesture, **target.identity()}, as_json=True)


def cmd_ui_type(args: argparse.Namespace) -> int:
    plan = _ObservePlan(args)
    target = _target(args)
    plan.resolve(target)
    # Typing with nothing focused "succeeds" on both platforms while every
    # character lands nowhere (seen on an iOS Settings screen with no field).
    # The CLI is exploratory, so it still types — but says what it saw.
    focus_warning: dict[str, Any] | None = None
    certainty = "unknown"
    try:
        focused, certainty = ui_mod.typing_target(target.platform, ui_mod.snapshot(target))
    except errors.AutonomError:
        focused = None  # no tree backend (idb missing): nothing to check against
    else:
        if focused is None:
            focus_warning = {
                "code": "no_focused_field",
                "error": "no node on screen reports keyboard focus; the text may "
                         "have been swallowed",
                "hint": "Tap the field first (ui tap), wait for it to appear, then "
                        "type; confirm with ui find on the typed text.",
            }
    sensitive = bool(getattr(args, "sensitive", False))
    backend = _backend(ui_mod.type_text(target, args.text))
    detail, evidence_warning = actions_mod.record_detail_checked(
        session_mod.load_current(), "type", {
        "kind": "type",
        "sensitive": sensitive,
        # Screenshots already capture what the screen shows; the typed value
        # in an owner-only artifact is the same privacy class — unless the
        # caller marked it sensitive, in which case only the length is kept.
        "text": None if sensitive else args.text,
        "text_len": len(args.text),
    })
    payload: dict[str, Any] = {"ok": True, "typed": args.text}
    if sensitive:
        payload["typed"] = f"<{len(args.text)} chars>"
        payload["sensitive"] = True
    if backend:
        payload["backend"] = backend
    if detail:
        payload["detail"] = detail
    if focused is not None:
        payload["focused"] = {key: focused.get(key)
                              for key in ("ref", "role", "resource_id", "desc")}
        # iOS trees carry no focus attribute: the field is on screen, whether
        # it has the keyboard is not knowable from the dump.
        payload["focus"] = "verified" if certainty == "focused" else "unverified"
    if focus_warning:
        payload["warnings"] = [focus_warning]
    if evidence_warning:
        payload.setdefault("warnings", []).append(evidence_warning)
    # a sensitive value never reaches the observed screen or its saved tree
    plan.into(payload, target, "ui type", secret=args.text if sensitive else None)
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_ui_key(args: argparse.Namespace) -> int:
    plan = _ObservePlan(args)
    target = _target(args)
    plan.resolve(target)
    backend = _backend(ui_mod.press_key(target, args.keycode))
    payload: dict[str, Any] = {"ok": True, "keycode": args.keycode}
    if backend:
        payload["backend"] = backend
    plan.into(payload, target, "ui key")
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_ui_do(args: argparse.Namespace) -> int:
    """`ui do` (AL-12): Flow v1 steps from stdin, run in the active session
    through the flow executor in one call. It stops at the first failing
    step and replies with the step results, `failure` and the final screen
    (`screen`, `changes`, `foreground` as observe gives them after an
    action). A problem with the input is `invalid_value` before any step;
    exit 0 when every step passed, 1 when the run failed, as `flow run`."""
    global _OBSERVE_INTERRUPTED
    from autonom_lib.flow import inline as flow_inline

    timeout_ms = observe_mod.check_timeout(getattr(args, "observe_timeout_ms", None))
    inline = flow_inline.load(flow_inline.read(sys.stdin))
    record = session_mod.load_current()
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION, "ui do needs an active session",
            "Start one with 'autonom session start'.")
    flow_inline.bind_app(inline, record.get("app_id"))
    target = _target(args)
    runner = flow_executor.Executor(target, record, flow_executor.RunConfig())
    # every sensitive value as the run typed, set or read it, step by step
    sensitive = flow_inline.watch(runner, inline)
    result = flow_inline.run(runner, inline)
    payload = flow_inline.reply(inline, result)
    # the screen the steps ended on, read (never acted on) the way observe
    # reads it after an action, also after a failed or interrupted run; each
    # value the run kept sensitive stays out of it on its own
    with flow_inline.masking(sensitive) as secret:
        keys, warnings = observe_mod.after_action(
            target, observe_mod.session_for(target), verb="ui do", timeout_ms=timeout_ms,
            secret=secret)
    if observe_mod.interrupted(keys):
        _OBSERVE_INTERRUPTED = True
    payload.update(keys)
    if warnings:
        payload.setdefault("warnings", []).extend(warnings)
    emit({**payload, **target.identity()}, as_json=True)
    return 0 if result.status == "passed" else 1


# --- evidence ----------------------------------------------------------------


def cmd_screenshot(args: argparse.Namespace) -> int:
    out = _output_path(args.out, "--out") if args.out else None
    target = _target(args)
    # A live Android status-bar pin drifts within a minute; re-send it so
    # before/after frames show the same bar.
    repin = simulator_mod.repin_before_capture(target)
    detail = shot_mod.capture_evidence(
        target,
        session_mod.load_current(),
        label=getattr(args, "label", None),
        task=getattr(args, "task", None),
        out=out,
    )
    payload: dict[str, Any] = {"ok": True, **detail}
    if repin is not None:
        payload["repinned"] = repin["repinned"]
        payload["repinned_controls"] = repin["controls"]
        if repin.get("repin_error"):
            payload.setdefault("warnings", []).append({
                "code": errors.SIGNAL_UNSTABLE, "error": repin["repin_error"],
                "hint": "The status-bar pin could not be re-sent before this "
                        "capture; re-run 'simulator status-bar pin'."})
        if repin.get("stale_pin_dropped"):
            payload["stale_pin_dropped"] = True
            payload.setdefault("warnings", []).append({
                "code": "stale_pin_dropped",
                "error": "a status-bar pin recorded for another device (or an earlier "
                         "boot) on this serial was dropped, not replayed",
                "hint": "Run 'simulator status-bar pin' on this device if the capture "
                        "needs a deterministic bar."})
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_shots_list(args: argparse.Namespace) -> int:
    limit = getattr(args, "max", 50)
    if limit is None or limit < 0:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--max must be >= 0, got {limit}",
            "Pass the number of most recent shots to list (0 lists none, "
            "total_matched still counts them).")
    record = session_mod.require_current()
    entries = shot_mod.load_index(record)
    if getattr(args, "task", None):
        wanted = shot_mod.slugify(args.task, limit=32)
        entries = [e for e in entries if shot_mod.slugify(e.get("task") or "", 32) == wanted]
    if getattr(args, "grep", None):
        pattern = re.compile(args.grep, re.IGNORECASE)
        entries = [e for e in entries
                   if pattern.search(json.dumps(e, ensure_ascii=False))]
    if getattr(args, "mocked_only", False):
        entries = [e for e in entries if e.get("mocks_active")]
    total = len(entries)
    shown = entries[-limit:] if limit else []
    return emit({"ok": True, "count": len(shown), "total_matched": total,
                 "truncated": total > len(shown), "shots": shown,
                 "index": str(shot_mod.index_path(record))}, as_json=True)


def cmd_shots_show(args: argparse.Namespace) -> int:
    """Read the metadata back out of the PNG itself."""
    path = Path(args.path).expanduser()
    if not path.is_absolute():
        # `shots list` names each shot by its path inside the session
        # artifacts dir; that is what an agent pastes back here.
        record = session_mod.load_current()
        if record and record.get("artifacts_dir"):
            try:
                inside = follow_mod.confine(Path(record["artifacts_dir"]), str(path))
            except errors.AutonomError:
                inside = None  # escapes the session: read it as cwd-relative
            if inside is not None and inside.exists():
                path = inside
    if not path.exists():
        raise errors.AutonomError(
            errors.BODY_FILE_NOT_FOUND, f"no such file: {path}",
            "Pass a path from 'autonom shots list'.",
        )
    size = shot_mod.png_size(path)
    return emit({"ok": True, "path": str(path),
                 "width": size[0] if size else None,
                 "height": size[1] if size else None,
                 "metadata": shot_mod.read_metadata(path)}, as_json=True)


def cmd_logs_tail(args: argparse.Namespace) -> int:
    target = _target(args)
    current = session_mod.load_current()
    stream_path = None
    if current and target.platform == IOS:
        candidate = Path(current["artifacts_dir"]) / "logs" / "stream.ndjson"
        stream_path = candidate if candidate.exists() else None
    # Android --package narrows by the app's uid (API 31+), else its pid, else
    # lines naming it, and says which in `filter`; an unknown package is
    # app_not_installed. iOS matches the installed app exactly and names the
    # `executable` the predicate used.
    detail = logs_mod.tail_detailed(
        target,
        stream_path=stream_path,
        package=args.package,
        since_seconds=args.since,
        max_lines=args.max_lines,
        grep=args.grep,
        app_path=(current.get("install_path")
                  if current and args.package and current.get("app_id") == args.package
                  else None),
    )
    entries, warnings = detail["entries"], detail["warnings"]
    payload: dict[str, Any] = {
        "ok": True,
        "count": len(entries),
        "lines": entries,
        **target.identity(),
    }
    if detail.get("filter") is not None:
        payload["filter"] = detail["filter"]
    if detail.get("executable"):
        payload["executable"] = detail["executable"]
    if detail.get("image_uuids"):
        # the binary UUIDs the app's records were matched by
        payload["image_uuids"] = detail["image_uuids"]
    if warnings:
        payload["warnings"] = warnings
    # A stopped session named with --session-id is only read: its evidence
    # is never overwritten with logs captured after the stop.
    if current and not current.get("stopped_at"):
        out = session_mod.artifact_path(current, "logs", "latest.json")
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        payload["saved"] = str(out)
    return emit(payload, as_json=True)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Always exits 0 unless --strict: a diagnostic that fails is useless in a pipe."""
    report = doctor_mod.collect(args)
    healthy = doctor_mod.is_healthy(report)
    if args.strict and not healthy:
        # a strict failure must read as one in the JSON too, not only the exit
        report["ok"] = False
        report["strict_failures"] = sorted(
            name for name, entry in report["tools"].items() if entry.get("state") != "ok")
    emit(report, as_json=True)
    return 0 if (healthy or not args.strict) else 1


# --- metrics -----------------------------------------------------------------


def _safe_label(label: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", label or "").strip("_") or "capture"


def _snapshot_base_dir(out: str | None) -> Path | None:
    """Artifact destination for snapshots: --out (dir, or a .json file's
    parent), else the session metrics/ dir, else None — no destination is
    allowed, the stdout payload still carries every metric."""
    from autonom_lib.metrics import artifacts as metrics_artifacts

    if out:
        target = Path(out)
        if target.suffix == ".json":
            target.parent.mkdir(parents=True, exist_ok=True)
            return target.parent
        target.mkdir(parents=True, exist_ok=True)
        return target
    record = session_mod.load_current()
    return metrics_artifacts.metrics_dir(record) if record else None


def _write_snapshot_artifacts(payload: dict[str, Any], raw: str | None,
                              label: str, out: str | None,
                              base: Path | None) -> list[str]:
    """Persist the snapshot JSON (and the raw meminfo text on Android).

    The raw file is named `…-meminfo.raw.txt` deliberately: `metrics memory
    analyze` globs `*-meminfo.txt` for capture packs, and a snapshot's raw
    dump must never fold into that series unasked. Stems are uniquified —
    a fast series would otherwise overwrite same-second artifacts."""
    from autonom_lib.metrics import artifacts as metrics_artifacts

    if base is None:
        return []
    if out and Path(out).suffix == ".json":
        target = Path(out)
        stem = metrics_artifacts.unique_stem(base, target.stem, ".json")
        json_path = base / f"{stem}.json"
    else:
        stem = metrics_artifacts.unique_stem(
            base, f"{metrics_artifacts.stamp()}-{label}",
            "-snapshot.json", "-meminfo.raw.txt")
        json_path = base / f"{stem}-snapshot.json"
    written: list[str] = []
    if raw:
        written.append(metrics_artifacts.write_text(
            base / f"{stem}-meminfo.raw.txt", raw))
    payload["artifacts"] = [str(json_path)] + written
    metrics_artifacts.write_text(
        json_path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return payload["artifacts"]


def cmd_metrics_snapshot(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import snapshot as metrics_snapshot

    target = _target(args)
    app_id = _app_id(args)
    payload, raw = metrics_snapshot.take(target, app_id)
    if args.task:
        payload["task"] = args.task
    _write_snapshot_artifacts(payload, raw,
                              _safe_label(args.label or "snapshot"),
                              args.out, _snapshot_base_dir(args.out))
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_metrics_series(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import artifacts as metrics_artifacts
    from autonom_lib.metrics import series as metrics_series
    from autonom_lib.metrics import snapshot as metrics_snapshot

    if args.count < 1:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--count must be >= 1, got {args.count}",
            "A series needs at least one snapshot.")
    if args.interval < 0:
        # 0 is allowed and pinned by callers: back-to-back snapshots
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--interval must be >= 0 seconds, got {args.interval}",
            "Pass the pause between snapshots, e.g. --interval 2.")
    label = _safe_label(args.label or "series")
    if args.from_dir:
        samples = metrics_series.from_dir(Path(args.from_dir), args.glob)
        identity: dict[str, Any] = {}
    else:
        target = _target(args)
        app_id = _app_id(args)
        identity = target.identity()
        base = _snapshot_base_dir(args.out)  # resolved once, not per sample

        def snap() -> dict[str, Any]:
            payload, raw = metrics_snapshot.take(target, app_id)
            _write_snapshot_artifacts(payload, raw, label, None, base)
            return payload

        samples = metrics_series.capture(snap, count=max(args.count, 1),
                                         interval=args.interval,
                                         sleep=time.sleep)
    report = metrics_series.summarize(samples, max(args.min_growth_kb, 0))
    payload = {"ok": True, "label": label, "samples": samples, **report,
               **identity}
    record = session_mod.load_current()
    if record and not args.from_dir:
        out = session_mod.artifact_path(record, "metrics", f"series-{label}.json")
        metrics_artifacts.write_text(
            out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        payload["artifact"] = str(out)
    return emit(payload, as_json=True)


def _metrics_out_dir(args: argparse.Namespace) -> Path:
    """--out dir, else the session's metrics/ dir; metrics depth verbs write
    real evidence, so unlike snapshot they refuse to run with nowhere to put it."""
    from autonom_lib.metrics import artifacts as metrics_artifacts

    if getattr(args, "out", None):
        return Path(args.out)
    record = session_mod.load_current()
    if record:
        return metrics_artifacts.metrics_dir(record)
    raise errors.AutonomError(
        errors.NO_ACTIVE_SESSION,
        "no session to hold the artifacts and no --out given",
        "Start a session, or pass --out DIR.",
    )


def _require_android(target: Target, message: str, hint: str) -> None:
    if target.platform != ANDROID:
        raise errors.AutonomError(errors.UNSUPPORTED_ON_PLATFORM, message, hint)


def cmd_metrics_memory_capture(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import android_memory as metrics_memory

    target = _target(args)
    _require_android(target, "the memory pack reads Android dumpsys",
                     "On iOS use 'metrics snapshot' and "
                     "'metrics trace --preset allocations'.")
    payload = metrics_memory.capture(
        target, _app_id(args), out_dir=_metrics_out_dir(args),
        label=_safe_label(args.label), want_hprof=not args.no_hprof)
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_metrics_memory_analyze(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import android_memory as metrics_memory
    from autonom_lib.metrics import artifacts as metrics_artifacts

    if args.dir:
        directory = Path(args.dir)
    else:
        record = session_mod.load_current()
        if not record:
            raise errors.AutonomError(
                errors.NO_ACTIVE_SESSION,
                "no session whose metrics/ dir could be analyzed",
                "Start a session, or pass --dir with the capture directory.",
            )
        directory = metrics_artifacts.metrics_dir(record)
    report = metrics_memory.analyze(directory, glob=args.glob,
                                    min_growth_kb=args.min_growth_kb)
    return emit({"ok": True, **report}, as_json=True)


def cmd_metrics_memory_warn(args: argparse.Namespace) -> int:
    target = _target(args)
    if target.platform == ANDROID:
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            "memory-warning injection exists only on the iOS Simulator",
            "On Android drive real pressure instead (fill memory in-app).",
        )
    ios_simctl.run_simctl(
        target.tool,
        ["spawn", target.target_id, "notifyutil", "-p",
         "UISimulatedMemoryWarningNotification"])
    return emit({"ok": True, "stimulus": "memory_warning",
                 "note": "the Darwin notification was posted (delivery "
                         "verified); whether the app reacts is not — newer "
                         "runtimes may ignore it, check the app's logs",
                 **target.identity()}, as_json=True)


def _frames_android_only() -> tuple[str, str]:
    """The refusal of the gfxinfo verbs off Android. The hint must not send a
    Simulator run to `trace --preset hitches`: Instruments refuses Animation
    Hitches on the Simulator."""
    from autonom_lib.metrics import frames as metrics_frames

    return ("gfxinfo frame stats are Android-only", metrics_frames.IOS_FRAMES_HINT)


def cmd_metrics_frames_reset(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import frames as metrics_frames

    target = _target(args)
    _require_android(target, *_frames_android_only())
    app_id = _app_id(args)
    metrics_frames.reset(target, app_id)
    return emit({"ok": True, "reset": app_id, **target.identity()}, as_json=True)


def cmd_metrics_frames_capture(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import artifacts as metrics_artifacts
    from autonom_lib.metrics import frames as metrics_frames

    target = _target(args)
    _require_android(target, *_frames_android_only())
    app_id = _app_id(args)
    raw, summary = metrics_frames.capture(target, app_id)
    payload: dict[str, Any] = {"ok": True, "summary": summary,
                               **target.identity()}
    if summary.get("warnings"):
        # `no_frames` (a Flutter app draws outside HWUI) belongs where every
        # other verb's warnings are, not only inside the summary
        payload["warnings"] = list(summary["warnings"])
    if args.out or session_mod.load_current():
        out_dir = _metrics_out_dir(args)
        stem = metrics_artifacts.unique_stem(
            out_dir,
            f"{metrics_artifacts.stamp()}-{_safe_label(args.label)}",
            "-gfxinfo.txt")
        payload["artifacts"] = [metrics_artifacts.write_text(
            out_dir / f"{stem}-gfxinfo.txt", raw)]
    return emit(payload, as_json=True)


def cmd_metrics_frames_flutter(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import frames as metrics_frames

    path = Path(args.file)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise errors.AutonomError(errors.BACKEND_FAILED, str(exc))
    except json.JSONDecodeError as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"{path} is not JSON: {exc}")
    summary = metrics_frames.flutter_summary(payload, args.budget_ms)
    if summary is None:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"no supported frame timing arrays found in {path}",
            "Expected keys like frame_build_times / frame_rasterizer_times "
            "from an integration-test timeline summary.",
        )
    return emit({"ok": True, "file": str(path), **summary}, as_json=True)


def cmd_metrics_trace(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import trace as metrics_trace

    target = _target(args)
    payload = metrics_trace.run_preset(
        target, _app_id(args), args.preset, duration=max(args.duration, 1.0),
        out_dir=_metrics_out_dir(args), label=_safe_label(args.label))
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_metrics_list_presets(args: argparse.Namespace) -> int:
    from autonom_lib.metrics import presets as metrics_presets

    platform = getattr(args, "platform", None)
    try:
        adb_path: str | None = adb_mod.find_adb(getattr(args, "adb", None))
    except errors.AutonomError:
        adb_path = None
    try:
        xcrun: str | None = ios_simctl.find_simctl(getattr(args, "simctl", None))
    except errors.AutonomError:
        xcrun = None
    listing = metrics_presets.listing(platform, adb=adb_path, xcrun=xcrun)
    return emit({"ok": True, "platform": platform or "all", **listing},
                as_json=True)


# --- device state ------------------------------------------------------------


def _app_id(args: argparse.Namespace, *, required: bool = True) -> str | None:
    app_id = getattr(args, "app_id", None)
    if not app_id:
        current = session_mod.load_current()
        app_id = (current or {}).get("app_id")
    if not app_id and required:
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED,
            "no app id given and none recorded in the session",
            "Pass --app-id, or start the session with --app-id.",
        )
    return app_id


_VIEW_ACTION = "android.intent.action.VIEW"
# What Android shows when several apps (or none by default) take a URL: the
# system resolver/chooser, not the app a deep link was meant for.
_CHOOSER_MARKERS = ("ResolverActivity", "ChooserActivity", "intentresolver")


def _open_android(target: Target, url: str, *, app_id: str | None = None) -> dict[str, Any]:
    """The VIEW intent through `am start -W`, which names the activity that
    took the URL: a deep link that lands in Chrome or the chooser instead of
    the app used to be reported exactly like one that reached the app."""
    if not device_state._URL.match(url or ""):  # noqa: SLF001 - open_url's own check
        device_state.open_url(target, url)  # raises its invalid_url envelope
    from autonom_lib.pretest import guard as pretest_guard

    # counted and checked against the allow list like device_state.open_url,
    # before any device command; a no-op without an active pre-test
    pretest_guard.before_mutation("open", target=target, url=url)
    completed = adb_mod.run_adb(
        target.tool,
        ["shell", "am", "start", "-W", "-a", _VIEW_ACTION, "-d", shlex.quote(url),
         *(["-p", app_id] if app_id else [])],
        serial=target.target_id, timeout=60, check=True,
    )
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    report = session_mod.parse_am_wait(output)
    handled_by = (report or {}).get("activity")
    detail: dict[str, Any] = {"handled_by": handled_by}
    if report is not None:
        detail["launch"] = report
    warnings: list[dict[str, Any]] = []
    diagnostics = output + "\n" + (completed.stderr if isinstance(completed.stderr, str) else "")
    unresolved = next((line.strip() for line in diagnostics.splitlines()
                       if line.strip().startswith("Error")), None)
    if unresolved:
        warnings.append({
            "code": "url_not_handled",
            "error": f"no activity took the URL: {unresolved[:200]}",
            "hint": "Check the app declares an intent filter for this scheme and "
                    "host ('adb shell dumpsys package d'), and that it is installed.",
        })
    elif handled_by and any(marker in handled_by for marker in _CHOOSER_MARKERS):
        warnings.append({
            "code": "url_opened_chooser",
            "error": f"the URL opened the system chooser ({handled_by}), not an app",
            "hint": "More than one app takes this URL and none is verified as its "
                    "default; verify the App Link or pick the app in the chooser.",
        })
    if warnings:
        detail["warnings"] = warnings
    return detail


def cmd_open(args: argparse.Namespace) -> int:
    target = _target(args)
    if target.platform == ANDROID:
        detail = _open_android(target, args.url)
    else:
        device_state.open_url(target, args.url)
        # `simctl openurl` does not say which app took the URL
        detail = {"handled_by": "unknown"}
    return emit({"ok": True, "opened": args.url, **detail, **target.identity()},
                as_json=True)


def cmd_permissions(args: argparse.Namespace) -> int:
    target = _target(args)
    detail = device_state.permissions(
        target, args.action, args.service, _app_id(args, required=target.platform == ANDROID)
    )
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_location(args: argparse.Namespace) -> int:
    target = _target(args)
    if args.location_command == "clear":
        device_state.clear_location(target)
        return emit({"ok": True, "location": None, **target.identity()}, as_json=True)
    session = session_mod.load_current()
    if args.location_command == "get":
        detail = device_state.get_location(target, session)
        return emit({"ok": True, **detail, **target.identity()}, as_json=True)
    detail = device_state.set_location(target, args.coordinates, session)
    if session is not None:
        session_mod.save(session)
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_media_add(args: argparse.Namespace) -> int:
    target = _target(args)
    detail = device_state.add_media(target, Path(args.path))
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_crash_list(args: argparse.Namespace) -> int:
    target = _target(args)
    entries = device_state.crash_list(target, _app_id(args, required=False))
    return emit(
        {"ok": True, "count": len(entries), "crashes": entries, **target.identity()},
        as_json=True,
    )


def cmd_crash_show(args: argparse.Namespace) -> int:
    target = _target(args)
    text = device_state.crash_show(target, args.name)
    payload: dict[str, Any] = {"ok": True, "name": args.name, **target.identity()}
    current = session_mod.load_current()
    if current:
        out = session_mod.artifact_path(current, "crashes", f"{args.name}.txt")
        out.write_text(text, encoding="utf-8")
        payload["saved"] = str(out)
    payload["preview"] = "\n".join(text.splitlines()[:100])
    return emit(payload, as_json=True)


def cmd_file_ls(args: argparse.Namespace) -> int:
    target = _target(args)
    entries = device_state.file_ls(target, _app_id(args), args.remote)
    return emit(
        {"ok": True, "count": len(entries), "entries": entries, **target.identity()},
        as_json=True,
    )


def cmd_file_pull(args: argparse.Namespace) -> int:
    out = _output_path(args.out, "--out") if args.out else None
    target = _target(args)
    if out is not None:
        destination = out
    else:
        current = session_mod.require_current()
        destination = session_mod.artifact_path(
            current, "files", *device_state.safe_relative(args.remote).split("/")
        )
    detail = device_state.file_pull(target, _app_id(args), args.remote, destination)
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


_RECORDING_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


# What `device_state.record_start` runs: `simctl io <udid> recordVideo … <path>`
# on iOS, `adb -s <serial> shell screenrecord /sdcard/autonom-recording.mp4`
# on Android. Its command line is what shows a recorded pid is still it.
_ANDROID_RECORDING = "/sdcard/autonom-recording.mp4"


def _command_tokens(command: str) -> list[str] | None:
    """`command` split like argv, or None when it cannot be (`ps` does not
    quote, so an argument with a quote character can defeat shlex)."""
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _has_token(command: str, value: str, tokens: list[str] | None) -> bool:
    """`value` is one whole argument of `command`: `R58M123` is not found in
    `R58M123ABCD`, nor a path in `<path>.bak`."""
    if tokens is not None and not any(char.isspace() for char in value):
        return value in tokens
    return re.search(r"(?:^|\s)" + re.escape(value) + r"(?:\s|$)", command) is not None


def _has_pair(command: str, flag: str, value: str, tokens: list[str] | None) -> bool:
    """`flag value` as two adjacent whole arguments (`-s <serial>`)."""
    if tokens is not None and not any(char.isspace() for char in value):
        return any(first == flag and second == value
                   for first, second in zip(tokens, tokens[1:]))
    return re.search(r"(?:^|\s)" + re.escape(flag) + r"\s+" + re.escape(value)
                     + r"(?:\s|$)", command) is not None


def _recorder_state(record: dict[str, Any], pid: int | None) -> str:
    """``none`` | ``gone`` | ``ours`` | ``pid_reused`` | ``unverified``."""
    if not pid:
        return "none"
    if not session_mod.pid_alive(pid):
        return "gone"
    command = processes_mod.command_of(pid)
    if command is None:
        return "unverified" if session_mod.pid_alive(pid) else "gone"
    tokens = _command_tokens(command)
    path = (record.get("background") or {}).get("recorder_path")
    target_id = str(record.get("target_id") or record.get("serial") or "")
    if not target_id:
        return "pid_reused"
    if (path and _has_token(command, "recordVideo", tokens)
            and _has_token(command, target_id, tokens)
            and _has_token(command, str(path), tokens)):
        return "ours"
    if (_has_token(command, "screenrecord", tokens)
            and _has_token(command, _ANDROID_RECORDING, tokens)
            and _has_pair(command, "-s", target_id, tokens)):
        return "ours"
    return "pid_reused"


def _stop_recorder(record: dict[str, Any]) -> dict[str, Any]:
    """Stop the session's screen recorder — only once its command line shows
    it is it (`_recorder_state`); through its registry row when there is one,
    which re-checks its own signature. ``{"pid", "result"}`` in
    `processes.terminate_entry`'s vocabulary."""
    pid = (record.get("background") or {}).get("recorder_pid")
    if not pid:
        return {"pid": None, "result": "none"}
    row = next((entry for entry in processes_mod.entries()
                if entry.get("pid") == pid and entry.get("kind") == "recorder"), None)
    state = _recorder_state(record, pid)
    if state == "ours":
        if row is not None:
            result = processes_mod.terminate_entry(row)
        else:
            result = "terminated" if session_mod.terminate_pid(pid) else "already_exited"
    else:
        result = {"gone": "already_exited", "pid_reused": "pid_reused",
                  "unverified": "unverified_skipped"}[state]
    if row is not None and result in ("terminated", "already_exited", "pid_reused"):
        processes_mod.deregister(int(pid))
    return {"pid": pid, "result": result}


def cmd_record_start(args: argparse.Namespace) -> int:
    # the name becomes <artifacts>/recordings/<name>.mp4: never a path
    if not _RECORDING_NAME.fullmatch(args.name or "") or ".." in args.name:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--name {args.name!r} is not a plain file name",
            "Use letters, digits, '.', '_' or '-' (no '/' and no '..'), "
            "e.g. --name login-flow.",
        )
    target = _target(args)
    record = session_mod.require_current()
    # a recorded pid that now runs something else is no recording in progress
    if _recorder_state(record, (record.get("background") or {}).get("recorder_pid")) in (
            "ours", "unverified"):
        raise errors.AutonomError(
            errors.RECORDING_ALREADY_ACTIVE,
            "a recording is already in progress for this session",
            "Stop it first with 'autonom record stop'.",
        )
    destination = session_mod.artifact_path(record, "recordings", f"{args.name}.mp4")
    pid = device_state.record_start(target, destination)
    record.setdefault("background", {})["recorder_pid"] = pid
    record["background"]["recorder_path"] = str(destination)
    session_mod.save(record)
    return emit(
        {"ok": True, "recording": args.name, "path": str(destination), "pid": pid,
         **target.identity()},
        as_json=True,
    )


def cmd_record_stop(args: argparse.Namespace) -> int:
    target = _target(args)
    record = session_mod.require_current()
    background = record.setdefault("background", {})
    if not background.get("recorder_pid"):
        # Nothing to stop: say so without inventing a `latest.mp4` path.
        return emit({"ok": True, "was_recording": False, "path": None, "bytes": 0,
                     **target.identity()}, as_json=True)
    destination = Path(background.get("recorder_path") or
                       session_mod.artifact_path(record, "recordings", "latest.mp4"))
    # SIGINT only a pid shown to still be this recorder: the recorded one
    # may have died long ago and been reused
    pid = background.get("recorder_pid")
    state = _recorder_state(record, pid)
    detail = device_state.record_stop(target, pid if state == "ours" else None, destination)
    warnings: list[dict[str, Any]] = []
    if state in ("pid_reused", "unverified"):
        detail["recorder"] = {"pid": pid, "result": ("pid_reused" if state == "pid_reused"
                                                     else "unverified_skipped")}
        if state == "unverified":
            warnings.append({
                "code": "unverified_skipped",
                "error": f"the recorder (pid {pid}) was not signalled: ps could not "
                         "confirm it is still the screen recorder",
                "hint": f"Inspect it with 'ps -p {pid}' and stop it yourself only if "
                        "it is yours.",
            })
    background["recorder_pid"] = None
    background["recorder_path"] = None
    session_mod.save(record)
    payload: dict[str, Any] = {"ok": True, **detail, **target.identity()}
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


# --- network -----------------------------------------------------------------


def cmd_network_start(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    return emit(network_start_payload(
        record, port=args.port, capture_bodies=args.capture_bodies,
        acknowledged=args.i_understand_mitm,
        mitmdump=getattr(args, "mitmdump", None),
        ignore_hosts=getattr(args, "ignore_hosts", None),
        intercept_connectivity_checks=getattr(args, "intercept_connectivity_checks", False),
    ), as_json=True)


def network_start_payload(record: dict[str, Any], *, port: int | None,
                          capture_bodies: bool, acknowledged: bool,
                          mitmdump: str | None = None, ignore_hosts: str | None = None,
                          intercept_connectivity_checks: bool = False) -> dict[str, Any]:
    """`network start` for `record`: consent, proxy, session update, payload.

    Shared by the CLI verb and the Canvas tools process, so both start the
    proxy, record consent and warn exactly the same way.
    """
    from autonom_lib.network import mocks as mocks_mod
    from autonom_lib.network import proxy as proxy_mod

    entry = consent_mod.require(
        consent_mod.Operation(
            kind="mitm_proxy",
            target=f"127.0.0.1:{port or 'auto'}",
            effect=("start a man-in-the-middle proxy that decrypts and records this "
                    "session's HTTP(S) traffic to disk"),
            flags=("--i-understand-mitm",),
        ),
        acknowledged=acknowledged,
    )
    state = proxy_mod.start(
        record, port=port, capture_bodies=capture_bodies,
        mitmdump=mitmdump,
        ignore_hosts=ignore_hosts,
        intercept_connectivity_checks=intercept_connectivity_checks,
    )
    consent_mod.record(record, entry)
    network = record.setdefault("network", {})
    network.update({"enabled": True, "proxy_host": state["proxy_host"],
                    "proxy_port": state["port"]})
    session_mod.register_stream(record, stream_id="network_flows",
                                kind="network", path="network/flows.jsonl",
                                label="mitm flows")
    session_mod.save(record)

    payload: dict[str, Any] = {"ok": True, **state}
    ca = proxy_mod.ca_certificate(record)
    if ca:
        payload["ca_certificate"] = str(ca)

    # The library's own warnings (proxy_already_running with different
    # settings) used to be overwritten by the list below.
    warnings: list[dict[str, str]] = list(state.get("warnings") or [])
    # What is actually being captured decides the warning, not what was asked:
    # an already-running proxy keeps its own --capture-bodies setting.
    if state.get("capture_bodies", capture_bodies):
        warnings.append({
            "code": "full_body_capture_enabled",
            "error": "full request and response bodies are being written to disk",
            "hint": "Bodies frequently contain credentials and personal data. "
                    "Delete the session artifacts when finished.",
        })
    # The registry outlives the session, so a rule added days ago is in force
    # again the moment the proxy starts. Say so before it can mislead anyone.
    mocks_state = mocks_mod.summary()
    payload["mocks"] = mocks_state
    session_registry = Path(record["artifacts_dir"]) / "network" / "session-mocks"
    if (session_registry / "registry.json").is_file():
        payload["session_mocks"] = mocks_mod.summary(session_registry)
    persistent = mocks_mod.persistent_mocks_warning(mocks_state)
    if persistent:
        warnings.append(persistent)
    if warnings:
        payload["warnings"] = warnings
    return payload


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Reap Autonom's leftover processes, from anywhere on the machine."""
    detail = processes_mod.cleanup(dry_run=args.dry_run, include_live=args.all)
    payload: dict[str, Any] = {"ok": True, **detail}
    warnings: list[dict[str, Any]] = []
    if detail["failed"]:
        warnings.append({
            "code": "termination_failed",
            "error": f"{detail['failed']} process(es) would not terminate",
            "hint": "They may belong to another user; inspect them with "
                    "'ps -p <pid>' before escalating.",
        })
    if detail.get("skipped_group_remnants"):
        warnings.append({
            "code": "group_remnant_left_running",
            "error": f"{detail['skipped_group_remnants']} process group(s) lost their "
                     "recorded leader; they were not signalled because the group id "
                     "may have been reused",
            "hint": "Inspect each with 'ps -Ao pid,pgid,command', and kill -- -<pgid> "
                    "only if it is yours.",
        })
    if detail.get("skipped_unverified"):
        warnings.append({
            "code": "unverified_skipped",
            "error": f"{detail['skipped_unverified']} process(es) were not signalled: "
                     "ps could not confirm they are still the process Autonom started",
            "hint": "Re-run 'autonom cleanup'; if it persists, inspect them with "
                    "'ps -p <pid>'.",
        })
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_processes(_: argparse.Namespace) -> int:
    """Read-only view of every process Autonom started on this machine."""
    return emit({"ok": True, **processes_mod.scan()}, as_json=True)


def cmd_network_stop(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    return emit(network_stop_payload(record, lambda: _target(args)), as_json=True)


def network_stop_payload(record: dict[str, Any], target_of: Any = None) -> dict[str, Any]:
    """Restore the target before stopping its proxy; keep both on failure."""
    from autonom_lib.network import attachment as attachment_mod
    from autonom_lib.network import proxy as proxy_mod

    if attachment_mod.transparent_route_live(record):
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "The emulator is still routed through this proxy at launch time.",
            "Shut down the routed emulator before stopping capture; detach cannot "
            "remove its launch-time proxy route.", capability="network.transparent_capture")
    detached = {"was_attached": False}
    if (record.get("network") or {}).get("attached"):
        target = target_of() if target_of else _target(argparse.Namespace())
        try:
            detached = network_detach_payload(record, target)
        except errors.AutonomError as exc:
            # The device may still point at the proxy: it is never stopped here.
            exc.extra.setdefault("proxy_kept_running", True)
            raise
        detached.pop("ok", None)
    detail = proxy_mod.stop(record)
    record.setdefault("network", {})["enabled"] = False
    session_mod.save(record)
    return {"ok": True, **detail, "detach": detached}


def cmd_network_status(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    return emit(network_status_payload(record, lambda: _target(args), _mock_hit_counts),
                as_json=True)


def network_status_payload(record: dict[str, Any], target: Any,
                           hit_counts: Any) -> dict[str, Any]:
    """`network status` for `record` (shared with the Canvas tools process).

    `target` is called only when the Android proxy setting must be read back;
    `hit_counts` returns the mock hit counts the idle-rule warning is judged by.
    """
    from autonom_lib.network import attachment as attachment_mod
    from autonom_lib.network import device_proxy_android
    from autonom_lib.network import mocks as mocks_mod
    from autonom_lib.network import proxy as proxy_mod

    state = proxy_mod.status(record)
    network = record.get("network") or {}
    payload: dict[str, Any] = {"ok": True, "proxy": state}

    # Only a flow whose client is the target is evidence: a host `curl`
    # through the proxy used to answer attached: true. The Android setting
    # is read back when no flow decides, and an unreadable device is
    # `setting_unreadable`, never "cleared externally".
    platform = record.get("platform") or ANDROID
    observe = ((lambda: device_proxy_android.read_setting(target()))
               if platform == ANDROID else None)
    evidence = attachment_mod.attachment_evidence(record, platform=platform,
                                                  observe_setting=observe)
    payload["attached"] = evidence["attached"]  # value types unchanged: bool or "unknown"
    payload["evidence"] = evidence["evidence"]
    # how far the attach got, independent of whether traffic was observed
    payload["attach_state"] = (
        network.get("attach_state")
        or ("manual" if network.get("platform_manual")
            else "automated" if network.get("attached") else "not_attached"))
    payload["recent_flow_count"] = evidence["recent_flow_count"]
    for key in ("reason", "target_flow_count", "unattributed_flow_count",
                "recent_user_agents"):
        if key in evidence:
            payload[key] = evidence[key]
    # How this attach captures, and whether the system CA is in place — a
    # transparent (rooted, `-http-proxy`-routed) attach captures Flutter/pinned
    # traffic; the app-proxy fallback does not. Additive keys.
    payload["capture_mode"] = network.get("capture_mode")
    if network.get("system_ca"):
        payload["system_ca"] = network.get("system_ca")
    if network.get("capture_mode") == "transparent" and network.get("http_proxy_routed"):
        payload["http_proxy_routed"] = network.get("http_proxy_routed")
    mocks_state = mocks_mod.summary()
    idle = mocks_mod.annotate_hits(mocks_mod.active(), hit_counts())
    payload["mocks"] = mocks_state
    session_registry = Path(record["artifacts_dir"]) / "network" / "session-mocks"
    if (session_registry / "registry.json").is_file():
        payload["session_mocks"] = mocks_mod.summary(session_registry)
    if idle:
        payload.setdefault("warnings", []).extend(idle)
    # the verb re-run while judging evidence says so too, as start and doctor do
    persistent = mocks_mod.persistent_mocks_warning(mocks_state)
    if persistent:
        payload.setdefault("warnings", []).append(persistent)
    if payload["attached"] == "unknown":
        payload["next_action"] = "exercise the app, then re-run 'autonom network status'"
    return payload


def cmd_network_attach(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    return emit(network_attach_payload(
        record, lambda: _target(args), acknowledged=args.i_understand_mitm,
        system_ca=getattr(args, "system_ca", False), install_ca=args.install_ca,
        network_cycle=not getattr(args, "no_network_cycle", False),
    ), as_json=True)


def network_attach_payload(record: dict[str, Any], target_of: Any, *, acknowledged: bool,
                           system_ca: bool = False, install_ca: bool = False,
                           network_cycle: bool = True) -> dict[str, Any]:
    """`network attach` for `record` (shared with the Canvas tools process).

    `target_of` is called once the proxy is known to run, as the verb always did.
    """
    from autonom_lib.network import device_proxy_android
    from autonom_lib.network import proxy as proxy_mod

    state = proxy_mod.status(record)
    if not state["running"]:
        raise errors.AutonomError(
            errors.PROXY_NOT_RUNNING,
            "no proxy is running for this session",
            "Start one first: 'autonom network start --i-understand-mitm'.",
        )
    target = target_of()
    if target.platform == ANDROID:
        if system_ca:
            # Transparent capture: the emulator is already routed through the
            # proxy at launch (`devices boot --http-proxy`); this only installs
            # the system CA so the proxy can decrypt. Refuses when not routed.
            detail = device_proxy_android.attach_transparent(
                target, record, port=state["port"], acknowledged=acknowledged
            )
            session_mod.save(record)
            return {"ok": True, "mode": "transparent", **detail, **target.identity()}
        ca_detail = None
        if install_ca:
            ca_detail = device_proxy_android.install_ca_certificate(
                target, record, acknowledged=acknowledged
            )
        try:
            detail = device_proxy_android.attach(
                target, record, port=state["port"], acknowledged=acknowledged,
                network_cycle=network_cycle,
            )
        finally:
            # A written proxy must stay recoverable even if refreshing Wi-Fi fails.
            session_mod.save(record)
        if ca_detail:
            detail["ca_installed"] = ca_detail
        session_mod.save(record)
        return {"ok": True, "mode": "automated", **detail, **target.identity()}

    from autonom_lib.network import device_proxy_ios

    detail = device_proxy_ios.attach(
        target, record, port=state["port"],
        acknowledged=acknowledged, install_ca=install_ca,
    )
    session_mod.save(record)
    return {"ok": True, **detail, **target.identity()}


def cmd_network_detach(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    return emit(network_detach_payload(record, _target(args)), as_json=True)


def network_detach_payload(record: dict[str, Any], target: Target) -> dict[str, Any]:
    """`network detach` for `record` (shared with the Canvas tools process)."""
    from autonom_lib.network import device_proxy_android

    # Never write one session's snapshot onto another device, or clear it there.
    if (target.target_id != record.get("target_id")
            or target.platform != record.get("platform")):
        raise errors.AutonomError(
            errors.SESSION_TARGET_MISMATCH,
            f"the proxy of session {record.get('session_id')} is attached to "
            f"{record.get('target_id')}, not {target.target_id}",
            "Select the session that owns this target (--session-id, or that "
            "target's flags) before detaching or stopping capture.",
            session_id=record.get("session_id"), target_id=record.get("target_id"))
    try:
        if target.platform == ANDROID:
            detail = device_proxy_android.detach(target, record)
        else:
            from autonom_lib.network import device_proxy_ios

            detail = device_proxy_ios.detach(target, record)
    finally:
        session_mod.save(record)
    return {"ok": True, **detail, **target.identity()}


def cmd_network_requests_list(args: argparse.Namespace) -> int:
    from autonom_lib.network import store as store_mod

    record = session_mod.require_current()
    payload = store_mod.listing(
        record,
        max_items=store_mod.DEFAULT_MAX if args.max is None else args.max,
        host=args.host,
        method=args.method,
        status=args.status,
        path_glob=args.path,
        since_seconds=args.since,
        mocked=args.mocked,
        since_id=args.since_id,
    )
    return emit({"ok": True, **payload}, as_json=True)


def cmd_network_requests_follow(args: argparse.Namespace) -> int:
    from autonom_lib.network import redact as redact_mod
    from autonom_lib.network import store as store_mod

    record = session_mod.require_current()
    filters = {"host": args.host, "method": args.method, "status": args.status,
               "path_glob": args.path, "mocked": args.mocked}
    flows_file = store_mod.flows_path(record)
    seen: set[str] = set()
    # The store is append-only JSONL (its own contract), so the follow tails
    # by byte offset and parses only what was appended — never the whole
    # history again on every tick. A shrink means the store was replaced:
    # start over from byte 0; `seen` keeps replayed flows from re-emitting.
    # `skipped` counts lines that are not flow records — not JSON, deeper than
    # the parser allows, JSON but not an object, nested deeper than
    # `store.MAX_LINE_DEPTH`, or refused by the scrubber — exactly like
    # `store.read_all_counted`; the eof line carries the count. A record whose
    # id is not a string is counted too: it cannot be deduplicated.
    state = {"offset": 0, "buffer": b"", "skipped": 0}

    def read_appended() -> list[dict[str, Any]]:
        if not flows_file.exists():
            return []
        size = flows_file.stat().st_size
        if size < state["offset"]:
            state["offset"], state["buffer"] = 0, b""
        if size == state["offset"]:
            return []
        with flows_file.open("rb") as handle:
            handle.seek(state["offset"])
            chunk = handle.read()
        state["offset"] += len(chunk)
        state["buffer"] += chunk
        *complete, state["buffer"] = state["buffer"].split(b"\n")
        parsed: list[dict[str, Any]] = []
        for raw_line in complete:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                state["skipped"] += 1  # a torn write or a hostile nesting depth
                continue
            if not isinstance(row, dict) or store_mod.too_deep(row):
                # `3`, `null`, `[1]`, `"x"` are not flow records; a too-deep
                # value would raise RecursionError when filtered or printed.
                state["skipped"] += 1
                continue
            if row.get("id") is not None and not isinstance(row.get("id"), str):
                state["skipped"] += 1  # a list or dict id is unhashable for `seen`
                continue
            try:
                parsed.append(redact_mod.scrub_flow(row))
            except (TypeError, ValueError, AttributeError, RecursionError):
                state["skipped"] += 1
        return parsed

    if not args.from_start:
        for flow in read_appended():  # baseline: everything already recorded
            if flow.get("id"):
                seen.add(flow["id"])

    def fetch_new() -> list[dict[str, Any]]:
        fresh = []
        for flow in read_appended():
            flow_id = flow.get("id")
            if not flow_id or flow_id in seen:
                continue
            seen.add(flow_id)  # dedup happens pre-filter: skipped ≠ unseen
            fresh.append(flow)
        return store_mod.filter_flows(fresh, **filters)

    def emit_line(payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("kind") == "eof" and state["skipped"]:
            payload["skipped"] = state["skipped"]  # the same dict becomes the journal payload
        _stream_print(payload)

    return _finish_stream(follow_mod.follow_poll(
        fetch_new, emit=emit_line, interval=args.interval,
        max_seconds=args.max_seconds, max_items=args.max))


def cmd_network_requests_show(args: argparse.Namespace) -> int:
    from autonom_lib.network import store as store_mod

    record = session_mod.require_current()
    flow = store_mod.find(record, args.id)
    payload: dict[str, Any] = {"ok": True, "request": flow}
    if args.full:
        store_mod.require_bodies(record)
        request_body = store_mod.body(record, args.id, "req")
        response_body = store_mod.body(record, args.id, "res")
        payload["full"] = {
            "request_body": request_body.decode("utf-8", "replace") if request_body else None,
            "response_body": response_body.decode("utf-8", "replace") if response_body else None,
        }
        payload["warnings"] = [{
            "code": "full_body_disclosed",
            "error": "full bodies may contain credentials or personal data",
            "hint": "Do not paste this output into a shared document.",
        }]
    return emit(payload, as_json=True)


def cmd_network_export(args: argparse.Namespace) -> int:
    from autonom_lib.network import har as har_mod
    from autonom_lib.network import proxy as proxy_mod
    from autonom_lib.network import store as store_mod

    record = session_mod.require_current()
    destination = Path(args.har).expanduser()
    if not destination.is_absolute():
        destination = Path(record["artifacts_dir"]) / destination
    destination = _output_path(destination, "--har")
    flows, warnings = store_mod.read_all(record)
    state = proxy_mod.status(record)
    document = har_mod.build(flows, bodies_captured=bool(state.get("capture_bodies")))
    destination.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    payload: dict[str, Any] = {"ok": True, "path": str(destination),
                               "entries": len(document["log"]["entries"])}
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def _mock_headers(args: argparse.Namespace) -> dict[str, str] | None:
    """`--header 'Name: value'`, repeatable. None when the flag was not used;
    a malformed header is refused rather than silently dropped."""
    from autonom_lib.network import mocks as mocks_mod

    return mocks_mod.parse_header_args(getattr(args, "header", None))


def _mock_body(args: argparse.Namespace) -> tuple[Path | None, str | None]:
    body_file = Path(args.body_file) if getattr(args, "body_file", None) else None
    body_text = getattr(args, "json", None)
    if body_file is not None and body_text is not None:
        raise errors.AutonomError(
            errors.CONFLICTING_TARGET_FLAGS,
            "--json and --body-file both given",
            "Pass one: --json for an inline body, --body-file for a file.",
        )
    return body_file, body_text


def _mock_selector(args: argparse.Namespace) -> dict[str, Any]:
    """`--url` (exact endpoint) and `--match` (glob) are mutually exclusive."""
    from autonom_lib.network import mocks as mocks_mod

    url = getattr(args, "url", None)
    match = getattr(args, "match", None)
    if url and match:
        raise errors.AutonomError(
            errors.CONFLICTING_TARGET_FLAGS,
            "--url and --match both given",
            "Pass one: --url for an exact endpoint, --match for a glob.",
        )
    if url:
        selector = mocks_mod.url_to_match(url)
        if getattr(args, "host", None):
            selector["host"] = args.host
        if getattr(args, "method", None):
            selector["method"] = args.method
        return selector
    return {"url_glob": match, "method": getattr(args, "method", None),
            "host": getattr(args, "host", None), "ignore_query": False}


def _mock_hit_counts() -> dict[str, int]:
    """How often each rule actually fired, read back from the recorded flows.

    A rule whose glob does not match is the quietest possible failure: the
    registry says enabled, `network status` says attached, the app gets real
    responses, and nothing anywhere says the rule never fired. It happened in
    this repository's own first live run — a glob ending in `/list` could not
    match a URL carrying a query string.
    """
    from autonom_lib.network import store as store_mod

    record = session_mod.load_current()
    if not record:
        return {}
    try:
        flows, _ = store_mod.read_all(record)
    except errors.AutonomError:
        return {}
    counts: dict[str, int] = {}
    for flow in flows:
        identifier = flow.get("mock_id")
        if identifier:
            counts[identifier] = counts.get(identifier, 0) + 1
    return counts


def _annotate_hits(rules: list[dict[str, Any]]) -> list[dict[str, str]]:
    from autonom_lib.network import mocks as mocks_mod

    return mocks_mod.annotate_hits(rules, _mock_hit_counts())


def cmd_network_mock_add(args: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod

    selector = _mock_selector(args)
    mocks_mod.require_target(selector.get("url_glob"))
    body_file, body_text = _mock_body(args)
    # No Content-Type of the author's own: the library types the body (a
    # --body-file by its extension, JSON-looking --json as application/json)
    # and marks the type implicit, so an explicit --header always wins (A.5).
    rule = mocks_mod.add(
        url_glob=selector["url_glob"],
        method=selector.get("method"),
        host=selector.get("host"),
        ignore_query=selector.get("ignore_query", False),
        status=args.status if args.status is not None else 200,
        headers=_mock_headers(args),
        body_file=body_file,
        body_text=body_text,
        note=getattr(args, "note", None),
        delay_s=getattr(args, "delay", None),
        hang=bool(getattr(args, "hang", None)),
    )
    return emit({"ok": True, "mock": rule, "registry": str(mocks_mod.registry_file())},
                as_json=True)


def cmd_network_mock_list(args: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod

    rules = mocks_mod.load()
    if not getattr(args, "all", False):
        rules = [rule for rule in rules if rule.get("enabled", True)]
    warnings = _annotate_hits(rules)
    payload: dict[str, Any] = {"ok": True, "count": len(rules), "mocks": rules,
                               **mocks_mod.summary()}
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_network_mock_show(args: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod
    from autonom_lib.network import redact as redact_mod

    rule = mocks_mod.get(args.id)
    payload: dict[str, Any] = {"ok": True, "mock": rule}
    body_path = (rule.get("response") or {}).get("body_path")
    if body_path and Path(body_path).exists():
        # A mock body is frequently a captured response, so it gets the same
        # scrubbing as captured traffic. That re-serialises JSON, so this is a
        # preview — the file at body_path is what is actually served.
        payload["body_preview"] = redact_mod.scrub_body(
            Path(body_path).read_text(encoding="utf-8", errors="replace")
        )
    return emit(payload, as_json=True)


def cmd_network_mock_update(args: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod

    selector = _mock_selector(args) if (args.url or args.match) else {}
    body_file, body_text = _mock_body(args)
    rule = mocks_mod.update(
        args.id,
        url_glob=selector.get("url_glob"),
        method=args.method,
        host=args.host,
        ignore_query=selector.get("ignore_query"),
        status=args.status,
        headers=_mock_headers(args),
        body_file=body_file,
        body_text=body_text,
        note=getattr(args, "note", None),
        delay_s=getattr(args, "delay", None),
        hang=getattr(args, "hang", None),
    )
    return emit({"ok": True, "mock": rule}, as_json=True)


def cmd_network_mock_enable(args: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod

    detail = mocks_mod.set_enabled(args.id, True, all_rules=args.all)
    return emit({"ok": True, **detail}, as_json=True)


def cmd_network_mock_disable(args: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod

    detail = mocks_mod.set_enabled(args.id, False, all_rules=args.all)
    return emit({"ok": True, **detail}, as_json=True)


def cmd_network_mock_remove(args: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod

    return emit({"ok": True, **mocks_mod.remove(args.id)}, as_json=True)


def cmd_network_mock_clear(_: argparse.Namespace) -> int:
    from autonom_lib.network import mocks as mocks_mod

    return emit({"ok": True, **mocks_mod.clear()}, as_json=True)


# --- flow --------------------------------------------------------------------


def _flow_summary(flow) -> dict[str, Any]:
    return {
        "file": flow.path,
        "id": flow.flow_id,
        "name": flow.name,
        "tags": flow.tags,
        # what it can really run on: requires.platform, else both minus iOS
        # when a step that runs there is Android-only
        "platforms": flow_validator.flow_platforms(flow),
    }


def _flow_files(path: Path, warnings: list[dict[str, Any]] | None = None) -> list[Path]:
    """Flow files under `path`; `warnings` (when given) receives one entry for
    the files discovery skipped because their header names another schema."""
    if path.is_dir():
        files, skipped = flow_validator.discover_with_skipped(path)
        skipped_note = flow_validator.skipped_warning(skipped)
        if skipped_note is not None and warnings is not None:
            warnings.append(skipped_note)
        if not files:
            raise errors.AutonomError(
                errors.FLOW_NO_FLOWS_FOUND,
                f"no .yaml flow files under {path}",
                hint="Flow files use the .yaml extension.",
            )
        return files
    return [path]


def cmd_flow_check(args: argparse.Namespace) -> int:
    path = Path(args.path)
    warnings: list[dict[str, Any]] = []
    files = _flow_files(path, warnings)
    flows: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for file in files:
        try:
            flows.append(_flow_summary(flow_validator.validate_tree(file)))
        except errors.AutonomError as exc:
            if len(files) == 1:
                raise
            problems.append(exc.as_dict())
    if problems:
        raise errors.AutonomError(
            errors.FLOW_CHECK_FAILED,
            f"{len(problems)} of {len(files)} flow files failed validation",
            hint="Each entry in 'errors' carries file, line, and column.",
            errors=problems, checked=len(files),
            **({"warnings": warnings} if warnings else {}),
        )
    payload: dict[str, Any] = {"ok": True, "checked": len(files), "flows": flows}
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_flow_fmt(args: argparse.Namespace) -> int:
    path = Path(args.path)
    files = _flow_files(path)
    drop_comments = bool(getattr(args, "drop_comments", False))
    prepared = []
    refused: list[dict[str, Any]] = []
    for file in files:
        flow = flow_validator.load_flow(file)
        canonical_text = flow_canonical.emit_flow(flow)
        original = file.read_text(encoding="utf-8")
        lost = flow_canonical.lost_comments(original, canonical_text)
        prepared.append((file, flow, canonical_text, original, lost))
        if (args.write and lost and not drop_comments
                and canonical_text != original and not flow.converted_from):
            refused.append({"file": str(file), "comments": len(lost),
                            "first_line": lost[0].get("line")})
    if refused:
        # Checked for every file before any is written: a directory run never
        # leaves half the suite rewritten and half refused.
        raise errors.AutonomError(
            errors.COMMENTS_WOULD_BE_LOST,
            f"{sum(item['comments'] for item in refused)} comment(s) in "
            f"{len(refused)} file(s) would be dropped by the canonical format",
            "Canonical output carries no comments. Move them into 'description:' "
            "or step labels, or pass --drop-comments to rewrite anyway.",
            files=refused,
        )
    results: list[dict[str, Any]] = []
    changed_any = False
    for file, flow, canonical_text, original, lost in prepared:
        changed = canonical_text != original
        entry: dict[str, Any] = {"file": str(file), "changed": changed}
        if lost:
            entry["comments_lost"] = len(lost)
        if flow.converted_from:
            entry["converted_from"] = flow.converted_from
        if changed and args.diff:
            entry["diff"] = "".join(difflib.unified_diff(
                original.splitlines(keepends=True),
                canonical_text.splitlines(keepends=True),
                fromfile=str(file), tofile=f"{file} (canonical)",
            ))
        if changed and args.write:
            if flow.converted_from:
                # --write must never destroy a Maestro source in place;
                # conversion to a file is always the explicit flow import.
                entry["write_skipped"] = (
                    "a Maestro source is never rewritten in place; use "
                    "'flow import --out' for an explicit conversion")
            else:
                file.write_text(canonical_text, encoding="utf-8")
                entry["written"] = True
        if not args.write and len(files) == 1:
            entry["canonical"] = canonical_text
        results.append(entry)
        changed_any = changed_any or changed
    emit({"ok": True, "changed": changed_any, "files": results}, as_json=True)
    if args.check and changed_any and not args.write:
        return 1  # doctor --strict precedent: report on stdout, nonzero exit
    return 0


def cmd_flow_list(args: argparse.Namespace) -> int:
    base = Path(args.path) if args.path else Path(".autonom/flows")
    if not base.exists():
        raise errors.AutonomError(
            errors.FLOW_NO_FLOWS_FOUND, f"no flow directory at {base}",
            hint="Pass a directory or file path, or create .autonom/flows.",
        )
    warnings: list[dict[str, Any]] = []
    files = _flow_files(base, warnings)
    flows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for file in files:
        try:
            flows.append(_flow_summary(flow_validator.load_flow(file)))
        except errors.AutonomError as exc:
            invalid.append({"file": str(file), "error_code": exc.code,
                            "error": exc.message})
    payload: dict[str, Any] = {"ok": True, "count": len(flows), "flows": flows}
    if invalid:
        payload["invalid"] = invalid
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def _tag_selected(tags: list[str], include: list[str], exclude: list[str]) -> bool:
    if exclude and any(tag in exclude for tag in tags):
        return False
    if include and not any(tag in include for tag in tags):
        return False
    return True


def _session_by_id(session_id: str) -> dict[str, Any]:
    if session_id == "current":
        record = session_mod.load_current()
        if not record:
            raise errors.AutonomError(
                errors.NO_ACTIVE_SESSION, "no active session",
                "Start one with 'autonom session start', or pass a session id.",
            )
        return record
    path = session_mod.sessions_home() / session_id / "session.json"
    if not path.is_file():
        raise errors.AutonomError(
            errors.SESSION_NOT_FOUND, f"no session {session_id!r}",
            f"Sessions live under {session_mod.sessions_home()}.",
        )
    return session_mod.upgrade(json.loads(path.read_text(encoding="utf-8")))


def cmd_flow_create(args: argparse.Namespace) -> int:
    record = _session_by_id(args.from_session)
    text, report = flow_compiler.compile_to_text(
        record, name=args.name, task=args.task)
    payload: dict[str, Any] = {
        "ok": True,
        "session_id": record.get("session_id"),
        **report,
    }
    if args.out:
        out = _output_path(args.out, "--out")
        out.write_text(text, encoding="utf-8")
        payload["out"] = args.out
        secrets = " ".join(f"--secret {name}"
                           for name in report.get("secrets_required", []))
        payload["replay"] = f"autonom flow run {args.out}" + (
            f" {secrets}" if secrets else "")
    else:
        payload["canonical"] = text
    return emit(payload, as_json=True)


def cmd_flow_import(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        raise errors.AutonomError(
            errors.FLOW_FILE_NOT_FOUND, f"flow file not found: {path}",
            file=str(path),
        )
    canonical_text = flow_maestro.import_flow(
        path.read_text(encoding="utf-8"), str(path))
    payload: dict[str, Any] = {"ok": True, "imported": str(path)}
    if args.out:
        _output_path(args.out, "--out").write_text(canonical_text, encoding="utf-8")
        payload["out"] = args.out
    else:
        payload["canonical"] = canonical_text
    return emit(payload, as_json=True)


def cmd_flow_export(args: argparse.Namespace) -> int:
    path = Path(args.path)
    flow = flow_validator.load_flow(path)
    exported = flow_maestro.export_flow(flow, str(path))
    payload: dict[str, Any] = {"ok": True, "exported": str(path),
                               "format": args.format}
    if args.out:
        _output_path(args.out, "--out").write_text(exported, encoding="utf-8")
        payload["out"] = args.out
    else:
        payload["maestro"] = exported
    return emit(payload, as_json=True)


ENV_HINT = "use NAME=VALUE, e.g. --env USER=qa"


def _flow_env_overrides(args: argparse.Namespace,
                        code: str = errors.FLOW_COMMAND_INVALID) -> dict[str, str]:
    """One owner of --env parsing for every verb that runs flows. `flow run`
    and `proof` keep their `flow_command_invalid`; the pre-test verbs pass
    `invalid_value`. Either way the refusal names the NAME=VALUE form."""
    env_overrides: dict[str, str] = {}
    for pair in args.env or []:
        if "=" not in pair or not pair.split("=", 1)[0]:
            raise errors.AutonomError(
                code, f"--env takes NAME=VALUE, got {pair!r}", ENV_HINT, flag="--env")
        key, value = pair.split("=", 1)
        env_overrides[key] = value
    return env_overrides


def _flow_secrets(args: argparse.Namespace) -> dict[str, str]:
    """One owner of --secret resolution for every verb that runs flows."""
    secrets: dict[str, str] = {}
    for name in args.secret or []:
        value = os.environ.get(name)
        if value is None:
            raise errors.AutonomError(
                errors.FLOW_SECRET_UNDEFINED,
                f"--secret {name} is not present in the process environment",
                hint="Export the variable before running; its value never "
                     "enters the flow file or artifacts.",
            )
        secrets[name] = value
    return secrets


_COMPOSITE = ("runFlow", "group", "retry", "repeat")


def _plan_steps(flow) -> list[dict[str, Any]]:
    """What a run would execute, numbered the way the executor numbers it.

    The executor counts every step pre-order across the whole tree: a
    composite (runFlow, group, retry, repeat) takes the next index, then its
    children do — onFlowStart first, then the steps, then onFlowComplete. So
    `runFlow` then `assertVisible` puts assertVisible after every child of
    the runFlow, not at 2. Each retry is planned as passing on its first
    attempt and each repeat as running all `times`; an index after a retry,
    a `while:` repeat or a `when:` runFlow can shift at runtime, and says so
    (`index_exact: false`).
    """
    planned: list[dict[str, Any]] = []
    counter = 0
    exact = True
    children: dict[str, Any] = {}

    def child_of(parent, step):
        target = (Path(parent.path).resolve().parent / step.args["file"]).resolve()
        key = str(target)
        if key not in children:
            children[key] = flow_validator.load_flow(target)
        return children[key]

    def walk(steps, owner, depth: int, parent_index: int | None,
             hook: str | None) -> None:
        nonlocal counter, exact
        for step in steps:
            counter += 1
            index = counter
            entry: dict[str, Any] = {"index": index, "command": step.command,
                                     "line": step.line}
            if getattr(step, "label", None):
                entry["label"] = step.label
            if owner is not flow:
                entry["flow"] = owner.path
            if depth:
                entry["depth"] = depth
                entry["parent_index"] = parent_index
            if hook:
                entry["hook"] = hook
            if not exact:
                entry["index_exact"] = False
            planned.append(entry)
            if step.command not in _COMPOSITE:
                continue
            if step.command == "runFlow":
                inline = "file" not in step.args
                body_owner = owner if inline else child_of(owner, step)
                body = step.args["commands"] if inline else body_owner.steps
                walk(body, body_owner, depth + 1, index, hook)
                if step.args.get("when") is not None:
                    exact = False  # the condition may skip every child
            elif step.command == "repeat":
                for _ in range(int(step.args.get("times") or 1)):
                    walk(step.args["commands"], owner, depth + 1, index, hook)
                if step.args.get("while") is not None:
                    exact = False  # may stop before `times`
            else:
                walk(step.args["commands"], owner, depth + 1, index, hook)
                if step.command == "retry":
                    exact = False  # a failed first attempt re-runs the block

    walk(flow.on_flow_start, flow, 0, None, "onFlowStart")
    walk(flow.steps, flow, 0, None, None)
    walk(flow.on_flow_complete, flow, 0, None, "onFlowComplete")
    return planned


def cmd_flow_run(args: argparse.Namespace) -> int:
    path = Path(args.path)
    include = args.include_tag or []
    exclude = args.exclude_tag or []
    discovery_warnings: list[dict[str, Any]] = []
    if path.is_dir():
        candidates = _flow_files(path, discovery_warnings)
        selected = []
        for file in candidates:
            flow = flow_validator.load_flow(file)
            if _tag_selected(flow.tags, include, exclude):
                selected.append(file)
        if not selected:
            raise errors.AutonomError(
                errors.FLOW_NO_FLOWS_FOUND,
                f"no flows under {path} match the tag filters",
                hint="Check --include-tag/--exclude-tag against 'flow list'.",
            )
        files = selected
    else:
        files = [path]

    if args.until_step is not None:
        if args.until_step < 1:
            raise errors.AutonomError(
                errors.FLOW_COMMAND_INVALID,
                "--until-step must be a positive runtime step index",
            )
        if len(files) != 1:
            raise errors.AutonomError(
                errors.FLOW_COMMAND_INVALID,
                "--until-step accepts one flow file, not a directory suite",
                hint="Replay one source run at a time from its detailed report.",
            )

    flows = [flow_validator.validate_tree(file) for file in files]

    record = session_mod.load_current()
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION,
            "flow run needs an active session",
            "Start one with 'autonom session start'.",
        )
    target = _target(args)

    skipped: list[str] = []
    if path.is_dir():
        # A suite is run on one target: a flow that cannot run there (declared
        # for the other platform, or Android-only steps on iOS) is skipped
        # with a warning, instead of its pre-flight refusal aborting the rest.
        runnable = []
        for flow in flows:
            platforms = flow_validator.flow_platforms(flow)
            if target.platform in platforms:
                runnable.append(flow)
                continue
            skipped.append(flow.path)
            discovery_warnings.append({
                "code": "flow_skipped_for_platform",
                "error": f"{flow.path} runs on {'/'.join(platforms) or 'no platform'}, "
                         f"not {target.platform}; skipped",
                "hint": "'autonom flow check <file>' shows why; a single-file run "
                        "names the step that is refused.",
                "file": flow.path,
                "platforms": platforms,
            })
        if not runnable:
            raise errors.AutonomError(
                errors.FLOW_NO_FLOWS_FOUND,
                f"no flow under {path} can run on {target.platform}",
                hint="Run the suite against a target of the platform the flows "
                     "declare, or check them with 'autonom flow list'.",
                skipped=skipped,
                **({"warnings": discovery_warnings} if discovery_warnings else {}),
            )
        flows = runnable

    env_overrides = _flow_env_overrides(args)
    secrets = _flow_secrets(args)

    config = flow_executor.RunConfig(
        env=env_overrides, secrets=secrets,
        default_timeout_ms=args.default_timeout_ms, dry_run=args.dry_run,
        stop_after_step=args.until_step,
        evidence_mode=(None if args.evidence == "flow" else args.evidence),
        evidence_collect=tuple(args.collect) if args.collect else None,
    )

    def run_one(flow) -> dict[str, Any]:
        runner = flow_executor.Executor(
            target, record, config,
            stdout_stream=sys.stdout if args.events else None,
        )
        result = runner.run(flow)
        summary: dict[str, Any] = {
            "status": result.status,
            "run_id": result.run_id,
            "flow": flow.path,
            "name": flow.name,
            "steps": [
                {k: v for k, v in vars(step).items() if v is not None}
                for step in result.steps
            ],
            "events": result.events_path,
            "sensitive": result.sensitive,
        }
        if getattr(result, "warnings", None):
            summary["warnings"] = list(result.warnings)
        if args.dry_run:
            # A dry run executes nothing, so `steps` is empty by definition;
            # list what *would* run — numbered exactly as the executor numbers
            # runtime steps, so `--until-step N` can be read off this list.
            summary["dry_run"] = True
            planned = _plan_steps(flow)
            if args.until_step is not None:
                planned = [entry for entry in planned if entry["index"] <= args.until_step]
                summary["until_step"] = args.until_step
            summary["planned"] = planned
            if summary["status"] == "passed":
                summary["status"] = "planned"
        if flow.converted_from:
            summary["converted_from"] = flow.converted_from
        if result.failure:
            summary["failure"] = result.failure
            brief = flow_repair.repair_brief(
                flow.path, result.failure, summary["steps"],
                events_path=result.events_path)
            if brief:
                summary["repair"] = brief
        if result.hook_failures:
            summary["hook_failures"] = result.hook_failures
        if result.replay_target:
            summary["replay_target"] = result.replay_target
        return summary

    clean = ("passed", "replayed", "planned")
    if len(flows) == 1:
        run = run_one(flows[0])
        # `runs` gives a single file the list a directory run has, so one
        # reader handles both; the run's own keys stay at the top level
        summary = {"ok": True, **run, "runs": [run], **target.identity()}
        exit_code = 0 if summary["status"] in clean else 1
    else:
        runs = []
        for flow in flows:  # a test failure moves on; an AutonomError aborts
            runs.append(run_one(flow))
        overall = ("planned" if args.dry_run and all(r["status"] == "planned" for r in runs)
                   else "passed" if all(r["status"] in clean for r in runs)
                   else "failed")
        summary = {"ok": True, "status": overall,
                   "flows": len(runs),
                   "failed": sum(1 for r in runs if r["status"] not in clean),
                   "runs": runs, **target.identity()}
        exit_code = 0 if overall in ("passed", "planned") else 1
    if skipped:
        summary["skipped_flows"] = skipped
    if discovery_warnings:
        summary.setdefault("warnings", [])
        summary["warnings"] = discovery_warnings + summary["warnings"]
    if not args.events:
        emit(summary, as_json=True)
    else:
        global _LAST_EMIT
        _LAST_EMIT = _with_control(summary)  # the journal summary stays useful in events mode
    return exit_code


# --- proof -------------------------------------------------------------------


def _proof_platform(args: argparse.Namespace) -> str | None:
    """The platform the proof's suite runs on, read without touching a device:
    ``--platform``, else the kind of ``--serial``/``--udid``, else the active
    session's platform; None when nothing names one."""
    platform = getattr(args, "platform", None)
    if platform:
        return platform
    if getattr(args, "serial", None):
        return "android"
    if getattr(args, "udid", None):
        return "ios"
    try:
        record = session_mod.load_current()
    except (OSError, ValueError, errors.AutonomError):
        return None
    if isinstance(record, dict) and (record.get("target_id") or record.get("serial")):
        return record.get("platform") or "android"
    return None


def cmd_proof(args: argparse.Namespace) -> int:
    repo = Path(args.repo or ".").resolve()
    # A malformed --env is the caller's CLI mistake, exactly as in flow run —
    # a hard refusal, never a silently dropped override or a blocked verdict.
    env_overrides = _flow_env_overrides(args)
    changed = proof_mod.changed_files(repo, args.base, args.head)
    flows_dir = Path(args.flows) if args.flows else repo / ".autonom/flows"
    out_dir = _output_dir(args.out, "--out") if args.out else None
    selected: list[dict[str, Any]] = []
    covered: list[str] = []
    invalid: list[dict[str, Any]] = []
    flow_warnings: list[dict[str, Any]] = []
    if flows_dir.is_dir():
        # a flow that does not load is a coverage gap to report, not to forget
        suite = proof_mod.select_suite(flows_dir, repo, changed,
                                       platform=_proof_platform(args))
        selected, covered, invalid = suite["selected"], suite["covered"], suite["invalid"]
        flow_warnings = list(suite.get("warnings") or [])
    uncovered = [name for name in changed if name not in covered]

    runs: list[dict[str, Any]] = []
    blocked_reason: str | None = None
    if selected:
        record = session_mod.load_current()
        if not record:
            blocked_reason = ("no active session — start one with "
                              "'autonom session start' so the suite can run")
        else:
            try:
                target = _target(args)
                # a missing secret blocks the verdict (config problem), it
                # does not crash the proof — hence inside this try
                config = flow_executor.RunConfig(
                    env=env_overrides, secrets=_flow_secrets(args))
                for entry in selected:
                    flow = flow_validator.validate_tree(entry["path"])
                    runner = flow_executor.Executor(target, record, config)
                    result = runner.run(flow)
                    runs.append({
                        "flow": str(entry["path"]),
                        "reasons": entry["reasons"],
                        "status": result.status,
                        "run_id": result.run_id,
                        "failure": result.failure,
                        "events": result.events_path,
                        "steps": [
                            {k: v for k, v in vars(step).items()
                             if v is not None}
                            for step in result.steps],
                    })
            except errors.AutonomError as exc:
                blocked_reason = f"{exc.code}: {exc.message}"

    status = proof_mod.verdict(selected, runs, uncovered, blocked_reason)
    result_payload: dict[str, Any] = {
        "ok": True,
        "status": status,
        "base": args.base,
        "head": args.head,
        "changed_files": changed,
        "changed_areas": proof_mod.changed_areas(changed),
        "selected": [{"flow": str(e["path"]), "reasons": e["reasons"]}
                     for e in selected],
        "uncovered_files": uncovered,
        "runs": [{k: v for k, v in run.items() if k != "steps"}
                 for run in runs],
    }
    if blocked_reason:
        result_payload["blocked_reason"] = blocked_reason
    if invalid:
        result_payload["invalid_flows"] = invalid
    # `partial` on a pass with unverified changed files, plus warnings
    result_payload.update(proof_mod.coverage_flags(status, uncovered, invalid))
    if flow_warnings:
        # a pull-request flow for another platform is still selected; say so
        result_payload["flow_warnings"] = flow_warnings
        result_payload["warnings"] = list(result_payload.get("warnings") or []) + [
            str(item.get("message")) for item in flow_warnings]

    if out_dir is not None:
        (out_dir / "proof.json").write_text(
            json.dumps(result_payload, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (out_dir / "proof.md").write_text(
            proof_mod.render_markdown(result_payload, repo), encoding="utf-8")
        result_payload["out"] = str(out_dir)

    emit(result_payload, as_json=True)
    if status == "pass":
        return 0
    if status in ("fail", "not_covered"):
        return 1  # a CI gate must go red; not_covered is never upgraded
    return 2  # blocked / inconclusive: verification itself did not happen


# --- ticket pre-test ------------------------------------------------------------
#
# Thin verbs: every rule (budgets, ids, masking, the pack, reviews) lives in
# autonom_lib.pretest, imported on demand so no other verb loads it.

PRETEST_TICKET_MAX_BYTES = 1_048_576
# flow run status -> check verdict (tp-008); `broken` is a run that did not
# reach the app's behaviour (infrastructure or definition failure).
_FLOW_VERDICTS = {"passed": "pass", "failed": "fail", "broken": "blocked",
                  "skipped": "inconclusive"}


def _pretest_session() -> dict[str, Any]:
    """The live session a pre-test verb works in, else `no_active_session`."""
    record = session_mod.load_current()
    if not record or record.get("stopped_at"):
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION, "no active session",
            "Start one with 'autonom session start --serial <id> --app-id <app>' "
            "(--udid on iOS), then run the pre-test verb again.")
    return record


def _pretest_newest(session: dict[str, Any]) -> dict[str, Any] | None:
    """The session's most recently created pre-test record, any status."""
    from autonom_lib.pretest import store as pretest_store

    records = pretest_store.list_records(session)
    if not records:
        return None
    return max(records, key=lambda item: (str(item.get("created_at") or ""),
                                          item["pretest_id"]))


def _pretest_active(session: dict[str, Any], *, changing: bool = False) -> dict[str, Any]:
    """The active pre-test (`no_active_pretest`). For a verb that changes it,
    a session whose newest pre-test is packed answers `pretest_finalized`:
    a pack is final, and the hint names the way to continue."""
    from autonom_lib.pretest import store as pretest_store

    try:
        return pretest_store.require_active(session)
    except errors.AutonomError as exc:
        if changing and exc.code == errors.NO_ACTIVE_PRETEST:
            newest = _pretest_newest(session)
            if newest is not None and newest.get("status") == "packed":
                pretest_store.ensure_mutable(newest)  # raises pretest_finalized
        raise


def _read_ticket(raw: str) -> str:
    """The ticket text from a file or, for `-`, from stdin (at most 1 MiB)."""
    limit = PRETEST_TICKET_MAX_BYTES
    if raw == "-":
        stream = getattr(sys.stdin, "buffer", None)
        data = (stream.read(limit + 1) if stream is not None
                else sys.stdin.read(limit + 1).encode("utf-8"))
    else:
        path = Path(raw).expanduser()
        if path.is_dir():
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--ticket must be a file: {raw} is a directory",
                "Pass the ticket's text or Markdown file (for example ticket.md inside "
                "it), or '-' to read the text from stdin.", path=raw)
        if not path.is_file():
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--ticket {raw}: no such file",
                "Pass the ticket as a text or Markdown file, or '-' to read it from stdin.",
                path=raw)
        try:
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--ticket {raw}: cannot read it ({exc.strerror or exc})",
                "Check the file's permissions, or pipe the text with --ticket -.",
                path=raw) from None
    if len(data) > limit:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "the ticket is larger than 1 MiB",
            "Pass the ticket's description and acceptance criteria only; attach logs "
            "and screenshots as finding evidence instead.")
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise errors.AutonomError(
            errors.INVALID_VALUE, "the ticket is empty",
            "Pass the ticket text: its title, description and acceptance criteria.")
    return text


def _max_actions(value: str | None) -> int | None:
    """`--max-actions` as given: a whole number >= 1, else invalid_value."""
    if value is None:
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        number = None
    if number is None or number < 1:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--max-actions must be a whole number >= 1, got {value!r}",
            "For example --max-actions 300.", flag="--max-actions")
    return number


def _whole(value: str | float | None) -> int | float | None:
    """`--max-minutes` as given: a finite number > 0 (never NaN, infinite, zero
    or negative, else invalid_value); whole numbers stay whole."""
    if value is None:
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        number = float("nan")
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--max-minutes must be a finite number > 0, got {value!r}",
            "For example --max-minutes 60 (not NaN, inf, 0 or a negative number).",
            flag="--max-minutes")
    return int(number) if number.is_integer() else number


def _pretest_doc(session: dict[str, Any], record: dict[str, Any], which: str) -> dict[str, Any]:
    """The pre-test's context.json or baseline.json as an object."""
    from autonom_lib.pretest import store as pretest_store

    reader = pretest_store.read_context if which == "context" else pretest_store.read_baseline
    try:
        doc = reader(session, record["pretest_id"])
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"the pre-test's {which}.json is missing or unreadable",
            "The pre-test directory was damaged; stop it with 'autonom pretest stop' and "
            "start a new one.", pretest_id=record["pretest_id"])
    return doc


def cmd_pretest_start(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import store as pretest_store

    session = _pretest_session()
    existing = pretest_store.active(session)
    if existing is not None:
        raise errors.AutonomError(
            errors.PRETEST_ALREADY_ACTIVE,
            f"pre-test {existing.get('pretest_id')} is still active in this session",
            "Finish it with 'autonom pretest pack' or 'autonom pretest stop' first.",
            pretest_id=existing.get("pretest_id"))
    ticket = _read_ticket(args.ticket)
    target = _target(args)
    budget = {"max_actions": _max_actions(args.max_actions),
              "max_minutes": _whole(args.max_minutes),
              "forbid_text": args.forbid_text, "allow_apps": args.allow_app}
    record = pretest_store.create(
        session, target=target, ticket_text=ticket, ticket_ref=args.ticket_ref,
        title=args.title, repo=Path(args.repo or ".").expanduser(), base=args.base,
        head=args.head, flows_dir=args.flows, app_id=args.app_id, budget=budget)
    pretest_id = record["pretest_id"]
    context = _pretest_doc(session, record, "context")
    baseline = _pretest_doc(session, record, "baseline")
    flows = context.get("flows") if isinstance(context.get("flows"), dict) else {}
    return emit({
        "ok": True, **record,
        "dir": str(pretest_store.pretest_dir(session, pretest_id)),
        "changed_files_count": len(context.get("changed_files") or []),
        "diff_truncated": bool(context.get("diff_truncated")),
        "selected_flows": [entry.get("flow") for entry in flows.get("selected") or []
                           if isinstance(entry, dict)],
        **({"flow_warnings": [str(item.get("message")) for item in flows["warnings"]
                              if isinstance(item, dict) and item.get("message")]}
           if isinstance(flows.get("warnings"), list) and flows["warnings"] else {}),
        "baseline": {key: (baseline.get(key) if isinstance(baseline.get(key), dict)
                           else {}).get("state")
                     for key in ("crashes", "logs", "network", "metrics")},
    }, as_json=True)


def cmd_pretest_status(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import checks as pretest_checks
    from autonom_lib.pretest import findings as pretest_findings
    from autonom_lib.pretest import guard as pretest_guard
    from autonom_lib.pretest import store as pretest_store

    session = _pretest_session()
    damage = pretest_store.damaged_status(session)
    if damage is not None:
        # a damaged active record is a state to report, not a crash: every
        # guarded device action refuses until it is stopped or restored
        return emit({"ok": True, **damage, "hint": pretest_store.DAMAGED_HINT},
                    as_json=True)
    record = _pretest_active(session)
    usage = pretest_guard.usage(session, record)
    states = pretest_checks.fold(session, record)
    found = pretest_findings.fold(session, record)
    opened = next((item for item in states if item["state"] == "open"), None)
    by_state: dict[str, int] = {}
    for item in states:
        by_state[item["state"]] = by_state.get(item["state"], 0) + 1
    return emit({
        "ok": True,
        **{key: record.get(key) for key in (
            "pretest_id", "status", "title", "ticket_ref", "kind", "parent_pack",
            "created_at", "target", "app_id", "base", "head")},
        "actions_used": usage.get("actions_used"),
        "actions_limit": usage.get("max_actions"),
        "actions_left": usage.get("actions_left"),
        "minutes_elapsed": usage.get("minutes_elapsed"),
        "minutes_limit": usage.get("max_minutes"),
        "minutes_left": usage.get("minutes_left"),
        "budget": usage,
        "open_check": None if opened is None else {
            "check_id": opened["check_id"], "title": opened["title"],
            "began_at": opened.get("began_at")},
        "checks": len(states),
        "checks_by_state": by_state,
        "findings": len(found),
        "auto_findings": sum(1 for item in found if item.get("origin") == "auto"),
    }, as_json=True)


def cmd_pretest_context(args: argparse.Namespace) -> int:
    session = _pretest_session()
    record = _pretest_active(session)
    context = _pretest_doc(session, record, "context")
    return emit({"ok": True, "pretest_id": record["pretest_id"], **context}, as_json=True)


def cmd_pretest_stop(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import store as pretest_store

    record = pretest_store.stop(_pretest_session(), force=bool(getattr(args, "force", False)))
    return emit({"ok": True, **record}, as_json=True)


def cmd_pretest_list(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import store as pretest_store

    session = _pretest_session()
    ids = pretest_store.active_ids(session)
    rows = [{key: record.get(key) for key in (
        "pretest_id", "status", "kind", "title", "ticket_ref", "created_at", "ended_at",
        "parent_pack", "pack")} for record in pretest_store.list_records(session)]
    return emit({"ok": True, "count": len(rows), "pretests": rows,
                 "active": ids[0] if ids else None}, as_json=True)


def cmd_pretest_check_add(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import checks as pretest_checks

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    state = pretest_checks.add(session, record, title=args.title, kind=args.kind,
                               case=args.case, why=args.why, flow=args.flow)
    return emit({"ok": True, "pretest_id": record["pretest_id"], **state}, as_json=True)


def cmd_pretest_check_begin(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import checks as pretest_checks

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    state = pretest_checks.begin(session, record, args.check_id)
    return emit({"ok": True, "pretest_id": record["pretest_id"], **state}, as_json=True)


def cmd_pretest_check_end(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import checks as pretest_checks

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    state = pretest_checks.end(session, record, args.check_id, verdict=args.verdict,
                               note=args.note,
                               observed_count=_observed_count(args.observed_count),
                               race_rule=True)
    return emit({"ok": True, "pretest_id": record["pretest_id"], **state}, as_json=True)


def _observed_count(value: str | None) -> int | None:
    """`--observed-count N`: a whole number >= 0 of at most 9 ASCII digits,
    else invalid_value (a longer digit string is never a request count, and
    int() of thousands of digits raises ValueError)."""
    if value is None:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,9}", text):
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--observed-count must be a whole number >= 0, got {value!r}",
            "Count the requests in 'autonom network requests list', e.g. "
            "--observed-count 1.", flag="--observed-count")
    return int(text)


def cmd_pretest_check_amend(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import checks as pretest_checks

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    state = pretest_checks.amend(session, record, args.check_id, verdict=args.verdict,
                                 note=args.note,
                                 observed_count=_observed_count(args.observed_count))
    return emit({"ok": True, "pretest_id": record["pretest_id"], **state}, as_json=True)


def cmd_pretest_check_list(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import checks as pretest_checks

    session = _pretest_session()
    record = _pretest_active(session)
    states = pretest_checks.fold(session, record)
    opened = next((item["check_id"] for item in states if item["state"] == "open"), None)
    return emit({"ok": True, "pretest_id": record["pretest_id"], "count": len(states),
                 "open_check": opened, "checks": states}, as_json=True)


def cmd_pretest_finding_add(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import findings as pretest_findings

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    finding = pretest_findings.add(
        session, record, check_id=args.check, severity=args.severity, title=args.title,
        actual=args.actual, expected=args.expected, evidence=args.evidence or ())
    return emit({"ok": True, "pretest_id": record["pretest_id"], **finding}, as_json=True)


def cmd_pretest_finding_list(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import findings as pretest_findings

    session = _pretest_session()
    record = _pretest_active(session)
    found = pretest_findings.fold(session, record)
    return emit({"ok": True, "pretest_id": record["pretest_id"], "count": len(found),
                 "findings": found}, as_json=True)


def _flow_execution(status: str, failure: dict[str, Any] | None, steps: list[Any]) -> str:
    """passed|failed|broken|skipped for one finished flow run, the key of
    `_FLOW_VERDICTS` (tp-008): a failure that is not the app's (infrastructure,
    definition) is broken, and a run whose every step was skipped proved nothing."""
    if status in ("passed", "replayed"):
        if steps and all(getattr(step, "status", None) == "skipped" for step in steps):
            return "skipped"
        return "passed"
    if (failure or {}).get("failure_class") in (None, "test_failure"):
        return "failed"
    return "broken"


def cmd_pretest_flows(args: argparse.Namespace) -> int:
    from autonom_lib.flow import schema as flow_schema
    from autonom_lib.pretest import checks as pretest_checks

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    opened = pretest_checks.open_check(session, record)
    if opened is not None:
        raise errors.AutonomError(
            errors.CHECK_ALREADY_OPEN, f"check {opened['check_id']} is still open",
            f"End it first with 'autonom pretest check end {opened['check_id']} "
            "--verdict <verdict>'; each selected flow runs as its own check.",
            open_check_id=opened["check_id"])
    context = _pretest_doc(session, record, "context")
    flows = context.get("flows") if isinstance(context.get("flows"), dict) else {}
    selected = [entry for entry in flows.get("selected") or [] if isinstance(entry, dict)]
    env_overrides = _flow_env_overrides(args, errors.INVALID_VALUE)
    secrets = _flow_secrets(args)
    runs: list[dict[str, Any]] = []
    if selected:
        target = _target(args)
        config = flow_executor.RunConfig(env=env_overrides, secrets=secrets)
    for entry in selected:
        path = str(entry.get("flow") or "")
        reasons = [str(item) for item in entry.get("reasons") or []]
        run: dict[str, Any] = {"flow": path, "reasons": reasons, "run_id": None,
                               "failure": None}
        flow = None
        try:
            if not path:
                raise errors.AutonomError(
                    errors.FLOW_FILE_NOT_FOUND, "a selected flow has no path",
                    "Start a new pre-test so its context is computed again.")
            flow = flow_validator.validate_tree(Path(path))
        except errors.AutonomError as exc:
            run["failure"] = {"error_code": exc.code,
                              "failure_class": flow_schema.failure_class(exc.code),
                              "error": exc.message}
        name = (flow.name if flow is not None and flow.name else Path(path).name) or "flow"
        title = f"Regression flow: {name}"
        state = pretest_checks.add(session, record, title=title, kind="regression",
                                   why="; ".join(reasons) or None,
                                   flow=path if path and Path(path).is_file() else None)
        run["check_id"] = state["check_id"]
        pretest_checks.begin(session, record, state["check_id"])
        execution = "broken"
        if flow is not None:
            runner = flow_executor.Executor(target, session, config)
            try:
                result = runner.run(flow)
            except errors.AutonomError as exc:
                # the run aborted (budget, backend, definition): its evidence
                # is written; the check is blocked, never a test failure
                run["run_id"] = getattr(runner, "_writer_run_id", None)
                run["failure"] = {"error_code": exc.code,
                                  "failure_class": flow_schema.failure_class(exc.code),
                                  "error": exc.message}
            except BaseException as exc:
                # an unexpected stop (a wedged backend, an interrupt) never
                # leaves the check open; the CLI still reports the error
                with contextlib.suppress(Exception):  # the original error wins
                    pretest_checks.end(session, record, state["check_id"],
                                       verdict="blocked",
                                       note=f"run stopped: {type(exc).__name__}",
                                       run_id=getattr(runner, "_writer_run_id", None))
                raise
            else:
                run["run_id"] = result.run_id
                run["failure"] = result.failure
                execution = _flow_execution(result.status, result.failure, result.steps)
        verdict = _FLOW_VERDICTS[execution]
        failure = run["failure"] or {}
        note = (f"{failure.get('error_code')}: {failure.get('error')}"
                if failure.get("error_code") else None)
        ended = pretest_checks.end(session, record, state["check_id"], verdict=verdict,
                                   note=note, run_id=run["run_id"])
        run.update({"status": execution, "verdict": ended["verdict"]})
        runs.append(run)
    payload: dict[str, Any] = {
        "ok": True, "pretest_id": record["pretest_id"], "count": len(runs),
        # one line per flow: which check it became and how it ended
        "flows": [{key: run.get(key) for key in
                   ("flow", "check_id", "run_id", "status", "verdict")} for run in runs],
        "runs": runs}
    if not selected:
        payload["warnings"] = [{
            "code": "no_flows_selected",
            "error": "the pre-test's context selected no flow for this diff",
            "hint": "Add flows whose 'properties.covers' globs match the changed files "
                    "(see 'autonom pretest context'), or plan the checks by hand."}]
    return emit(payload, as_json=True)


def cmd_pretest_health(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import health as pretest_health

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    result = pretest_health.run(session, record, _target(args))
    return emit({"ok": True, "pretest_id": record["pretest_id"], **result}, as_json=True)


def cmd_pretest_pack(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import pack as pretest_pack
    from autonom_lib.pretest import store as pretest_store

    session = _pretest_session()
    record = pretest_store.active(session)
    if record is None:
        # a stopped pre-test is packed; a packed one returns its pack unchanged
        record = _pretest_newest(session)
        if record is None:
            record = pretest_store.require_active(session)  # raises no_active_pretest
    target = None
    if record.get("status") != "packed":
        try:
            target = _target(args)
        except errors.AutonomError:
            target = None  # the device is gone: health reads what it can
    result = pretest_pack.build(session, record, out=args.out, target=target)
    return emit({"ok": True, **result}, as_json=True)


def _start_more_checks(session: dict[str, Any], target: Target, pack: Path,
                       titles: list[str], *, app_id: str | None,
                       budget: dict[str, Any]) -> dict[str, Any]:
    """The child pre-test of a `more-checks` review: linked to the pack, with
    the parent's ticket and diff context, holding the requested checks.

    Its safety rules (app id, budget, forbidden text, allowed apps) come from
    the reviewer: the review's own flags, else this session and the defaults.
    The pack's record is never trusted for them, so a pack made with relaxed
    rules cannot loosen the guard of a pre-test that runs on QA's device."""
    from autonom_lib.pretest import checks as pretest_checks
    from autonom_lib.pretest import pack as pretest_pack
    from autonom_lib.pretest import store as pretest_store

    parent = pretest_store.find_pack_record(pack)
    try:
        context = json.loads(pretest_pack.read_regular_bytes(pack / pretest_store.CONTEXT_FILE))
        ticket = pretest_pack.read_regular_bytes(pack / pretest_store.TICKET_FILE).decode(
            "utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"cannot read the pack's ticket or context: {exc}",
            "Verify the pack with 'autonom report verify <pack>'.", path=str(pack)) from None
    if not isinstance(context, dict):
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, "the pack's context.json is not an object",
            "Verify the pack with 'autonom report verify <pack>'.", path=str(pack))
    child = pretest_store.create(
        session, target=target, ticket_text=ticket,
        ticket_ref=parent.get("ticket_ref"), title=parent.get("title"),
        repo=parent.get("repo"), base=parent.get("base"), head=parent.get("head"),
        flows_dir=parent.get("flows_dir"), app_id=app_id,
        budget=budget, parent_pack=pack, kind="more-checks",
        context=context)
    added = [pretest_checks.add(session, child, title=title, kind="ticket")
             for title in titles]
    return {key: child.get(key) for key in (
        "pretest_id", "status", "kind", "parent_pack", "title", "app_id",
        "budget")} | {"checks": added}


def cmd_pretest_review(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import review as pretest_review
    from autonom_lib.pretest import store as pretest_store

    session, target = None, None
    child_app_id: str | None = None
    child_budget: dict[str, Any] = {}
    rule_flags = [flag for flag, value in (
        ("--app-id", args.app_id), ("--max-actions", args.max_actions),
        ("--max-minutes", args.max_minutes), ("--forbid-text", args.forbid_text),
        ("--allow-app", args.allow_app)) if value is not None]
    if args.decision != "more-checks" and rule_flags:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"{', '.join(rule_flags)} only applies to more-checks",
            "Only more-checks starts a child pre-test; drop the flag or use more-checks.",
            flags=rule_flags)
    if args.decision == "more-checks":
        # everything the child needs is checked before the decision is written
        if not args.check:
            raise errors.AutonomError(
                errors.INVALID_VALUE, "more-checks needs at least one --check",
                'Name each check to add, e.g. --check "Rotate on results".')
        # the child's rules are the reviewer's, never the pack's (see
        # _start_more_checks); a bad value is refused before anything is written
        child_budget = {"max_actions": _max_actions(args.max_actions),
                        "max_minutes": _whole(args.max_minutes),
                        "forbid_text": args.forbid_text, "allow_apps": args.allow_app}
        session = _pretest_session()
        child_app_id = args.app_id or session.get("app_id")
        pretest_store._normalize_budget(child_budget, child_app_id)
        existing = pretest_store.active(session)
        if existing is not None:
            raise errors.AutonomError(
                errors.PRETEST_ALREADY_ACTIVE,
                f"pre-test {existing.get('pretest_id')} is still active in this session",
                "The more-checks child starts in this session; pack or stop the active "
                "pre-test first.", pretest_id=existing.get("pretest_id"))
        target = _target(args)
    result = pretest_review.write(
        args.pack, args.decision, note=args.note, checks=args.check or (),
        findings=args.finding or (), confirm=args.confirm or (),
        dismiss=args.dismiss or (), author=args.author)
    payload: dict[str, Any] = {"ok": True, **result}
    if session is not None:
        pack = Path(args.pack).expanduser().resolve()
        try:
            payload["child"] = _start_more_checks(session, target, pack,
                                                  list(result["checks"]),
                                                  app_id=child_app_id,
                                                  budget=child_budget)
        except errors.AutonomError as exc:
            raise errors.AutonomError(
                exc.code, f"{exc.message} (the more-checks decision was recorded, but its "
                          "child pre-test did not start)",
                (exc.hint + " " if exc.hint else "")
                + "Fix the cause and run the same review again; it adds a new decision.",
                **{**exc.extra, "review": result["path"]}) from None
    return emit(payload, as_json=True)


def cmd_pretest_review_status(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import review as pretest_review

    return emit(pretest_review.status(args.pack), as_json=True)


def cmd_pretest_kit(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import checks as pretest_checks
    from autonom_lib.pretest import kit as pretest_kit
    from autonom_lib.pretest import store as pretest_store

    payload: dict[str, Any] = {"ok": True, "count": len(pretest_kit.KIT),
                               "kit": [dict(case) for case in pretest_kit.KIT]}
    try:
        session = session_mod.load_current()
        record = pretest_store.active(session) if session else None
        if record is not None:
            payload["pretest_id"] = record["pretest_id"]
            payload["coverage"] = pretest_kit.coverage(pretest_checks.fold(session, record))
    except (errors.AutonomError, OSError, ValueError):
        pass  # the kit itself never depends on a session
    return emit(payload, as_json=True)


def _flow_draft_message(result: dict[str, Any]) -> str:
    """What `pretest flow-draft` wrote and where, in one sentence."""
    path = result.get("draft_flow")
    if result.get("written"):
        said = f"wrote the draft flow to {path}"
    else:
        said = f"the draft flow at {path} already holds this flow; nothing was written"
    if result.get("pack"):
        said += "; the pack is final, so nothing was recorded on the finding"
    elif result.get("recorded"):
        said += f"; recorded it on finding {result.get('finding_id')}"
    else:
        said += f"; finding {result.get('finding_id')} already names it"
    return said + ". Review its closing assertion before you run it."


def _same_file(left: Any, right: Any) -> bool:
    try:
        return Path(str(left)).expanduser().resolve() == Path(str(right)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _pack_draft_repro(pack: str, check_id: str, draft: str) -> tuple[str, bool]:
    """The next command for a draft written from a pack, and whether it is
    `pretest rerun`. rerun replays the check's own flow, else a finding's
    recorded draft or its default draft path, so a draft elsewhere (a custom
    --out) is replayed with `flow run` instead."""
    from autonom_lib.pretest import regress as pretest_regress

    try:
        planned = pretest_regress.plan_rerun(pack, [check_id])["plan"]
    except errors.AutonomError:
        planned = []
    if any(_same_file(item.get("flow"), draft) for item in planned):
        return f"autonom pretest rerun <pack> --check {check_id}", True
    return f"autonom flow run {shlex.quote(draft)}", False


def cmd_pretest_flow_draft(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import regress as pretest_regress

    if args.out is not None and not str(args.out).strip():
        raise errors.AutonomError(
            errors.INVALID_VALUE, "--out is empty",
            "Pass a .yaml file path, or leave --out out for the default "
            "<repo>/.autonom/flows/drafts/<ticket-slug>-<finding-id>.yaml.", flag="--out")
    if args.pack:
        # a pack is read (and verified) as it is; nothing is recorded in it
        session = session_mod.load_current()
        result = pretest_regress.flow_draft(session, args.pack, args.finding_id, out=args.out)
    else:
        session = _pretest_session()
        record = _pretest_active(session, changing=True)
        result = pretest_regress.flow_draft(session, record, args.finding_id, out=args.out)
    payload: dict[str, Any] = {"ok": True, **result, "message": _flow_draft_message(result)}
    if result.get("pack") and result.get("check_id"):
        # repro records on the active pre-test only (refused on a pack); rerun
        # replays the draft only when it would find this very file
        repro, by_rerun = _pack_draft_repro(str(result["pack"]), str(result["check_id"]),
                                            str(result.get("draft_flow")))
        payload["repro"] = repro
        if not by_rerun:
            payload["message"] += (
                f" 'autonom pretest rerun <pack> --check {result['check_id']}' does not "
                "replay this file (it uses the check's own flow or the draft at the default "
                "path); replay it with 'autonom flow run'.")
    return emit(payload, as_json=True)


def _pretest_run_config(args: argparse.Namespace) -> Any:
    return flow_executor.RunConfig(env=_flow_env_overrides(args, errors.INVALID_VALUE),
                                   secrets=_flow_secrets(args))


def cmd_pretest_repro(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import pack as pretest_pack
    from autonom_lib.pretest import regress as pretest_regress

    if args.pack:
        # child-free design: a pack is final, and repro records on the finding
        if not pretest_pack.is_pack(args.pack):
            raise errors.AutonomError(
                errors.NOT_A_PRETEST_PACK, f"{args.pack} is not a pre-test pack",
                "Pass the directory 'autonom pretest pack' printed.", path=args.pack)
        raise errors.AutonomError(
            errors.PRETEST_FINALIZED,
            "a packed pre-test cannot record repro results",
            "Replay the pack's drafts on the current build with 'autonom pretest rerun "
            "<pack> --check <id>', or run repro in the active pre-test before packing.",
            pack=args.pack, finding_id=args.finding_id)
    pretest_regress.check_times(args.times)  # refused before anything else
    session = _pretest_session()
    _pretest_active(session, changing=True)
    config = _pretest_run_config(args)
    result = pretest_regress.repro(session, args.finding_id, times=args.times,
                                   target=_target(args), config=config)
    return emit({"ok": True, **result}, as_json=True)


def cmd_pretest_rerun(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import regress as pretest_regress

    session = _pretest_session()
    config = _pretest_run_config(args)
    result = pretest_regress.rerun(session, args.pack, check_ids=args.check or (),
                                   target=_target(args), config=config)
    return emit({"ok": True, **result}, as_json=True)


def cmd_pretest_warmup(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import warmup as pretest_warmup

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    result = pretest_warmup.run(session, record, _target(args),
                                config=_pretest_run_config(args))
    return emit({"ok": True, "pretest_id": record["pretest_id"], **result}, as_json=True)


def cmd_pretest_a11y(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import a11y as pretest_a11y

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    result = pretest_a11y.run(session, record, _target(args), check_id=args.check)
    return emit({"ok": True, "pretest_id": record["pretest_id"], **result}, as_json=True)


def cmd_pretest_events(args: argparse.Namespace) -> int:
    from autonom_lib.pretest import events as pretest_events

    session = _pretest_session()
    record = _pretest_active(session, changing=True)
    result = pretest_events.run(session, record, check_id=args.check)
    return emit({"ok": True, "pretest_id": record["pretest_id"], **result}, as_json=True)


# --- atlas -------------------------------------------------------------------


def _atlas_app_id(args: argparse.Namespace,
                  record: dict[str, Any] | None = None) -> str:
    app_id = getattr(args, "app_id", None) or (record or {}).get("app_id")
    if not app_id:
        raise errors.AutonomError(
            errors.NO_TARGET, "no app id for the atlas",
            "Pass --app-id, or use a session started with --app-id.",
        )
    return app_id


def cmd_atlas_update(args: argparse.Namespace) -> int:
    from autonom_lib.atlas import graph as atlas_graph

    record = _session_by_id(args.session)
    app_id = _atlas_app_id(args, record)
    graph = atlas_graph.load(app_id)
    session_id = record.get("session_id")
    artifacts = Path(record["artifacts_dir"])

    runs = 0
    edges = 0
    for events_path in sorted(artifacts.glob("flows/*/events.ndjson")):
        events = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        edges += atlas_graph.ingest_flow_events(graph, events, session_id)
        runs += 1
    details = list(actions_mod.read_details(record).values())
    edges += atlas_graph.ingest_action_details(graph, details, session_id)

    path = atlas_graph.save(app_id, graph)
    return emit({"ok": True, "app_id": app_id, "graph": str(path),
                 "runs_ingested": runs, "transitions_touched": edges,
                 **atlas_graph.summary(graph)}, as_json=True)


def cmd_atlas_show(args: argparse.Namespace) -> int:
    from autonom_lib.atlas import graph as atlas_graph

    app_id = _atlas_app_id(args, session_mod.load_current())
    return emit({"ok": True,
                 **atlas_graph.summary(atlas_graph.load(app_id))}, as_json=True)


def cmd_atlas_coverage(args: argparse.Namespace) -> int:
    from autonom_lib.atlas import graph as atlas_graph

    app_id = _atlas_app_id(args, session_mod.load_current())
    return emit({"ok": True, "app_id": app_id,
                 **atlas_graph.coverage(atlas_graph.load(app_id))}, as_json=True)


def cmd_atlas_paths(args: argparse.Namespace) -> int:
    from autonom_lib.atlas import graph as atlas_graph

    app_id = _atlas_app_id(args, session_mod.load_current())
    graph = atlas_graph.load(app_id)
    return emit({"ok": True, "app_id": app_id,
                 **atlas_graph.paths(graph, getattr(args, "from"), args.to)},
                as_json=True)


def cmd_atlas_export(args: argparse.Namespace) -> int:
    from autonom_lib.atlas import graph as atlas_graph

    app_id = _atlas_app_id(args, session_mod.load_current())
    graph = atlas_graph.load(app_id)
    out = _output_path(args.out, "--out")
    out.write_text(json.dumps(graph, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return emit({"ok": True, "app_id": app_id, "out": str(out),
                 "screens": len(graph.get("screens", {})),
                 "transitions": len(graph.get("transitions", {}))}, as_json=True)


def cmd_atlas_diff(args: argparse.Namespace) -> int:
    from autonom_lib.atlas import graph as atlas_graph

    # load_snapshot validates the shape: a truncated or foreign JSON file is a
    # clean envelope, never a traceback or a diff of garbage
    base = atlas_graph.load_snapshot(Path(args.base))
    if args.head:
        head = atlas_graph.load_snapshot(Path(args.head))
    else:
        head = atlas_graph.load(_atlas_app_id(args, session_mod.load_current()))
    return emit({"ok": True, **atlas_graph.diff(base, head)}, as_json=True)


# --- report ------------------------------------------------------------------


def _resolve_run_dir(record: dict[str, Any], run_id: str | None) -> Path:
    flows_dir = Path(record["artifacts_dir"]) / "flows"
    if run_id:
        run_dir = flows_dir / run_id
        try:
            contained = run_dir.resolve().is_relative_to(flows_dir.resolve())
        except OSError:
            contained = False
        if not contained or Path(run_id).name != run_id:
            raise errors.AutonomError(
                errors.PATH_FORBIDDEN,
                "report run id must name one run inside this session",
            )
        if not (run_dir / "manifest.json").is_file():
            raise errors.AutonomError(
                errors.FLOW_FILE_NOT_FOUND,
                f"no run {run_id!r} with a manifest in this session",
                hint=f"Runs live under {flows_dir}.",
            )
        return run_dir
    candidates = sorted(
        (path.parent for path in flows_dir.glob("*/manifest.json")),
        key=lambda directory: directory.stat().st_mtime,
    )
    if not candidates:
        raise errors.AutonomError(
            errors.FLOW_NO_FLOWS_FOUND,
            "this session has no flow runs with a manifest",
            hint="Run a flow first: autonom flow run <file>.",
        )
    return candidates[-1]


def _suite_manifests(record: dict[str, Any], last: int | None) -> list[dict]:
    """Every run of this session with a manifest, oldest first."""
    flows_dir = Path(record["artifacts_dir"]) / "flows"
    paths = sorted(flows_dir.glob("*/manifest.json"),
                   key=lambda p: p.stat().st_mtime)
    if not paths:
        raise errors.AutonomError(
            errors.FLOW_NO_FLOWS_FOUND,
            "this session has no flow runs with a manifest",
            hint="Run a flow first: autonom flow run <file>.",
        )
    if last:
        paths = paths[-last:]
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def _report_model(record: dict[str, Any], run_id: str | None) -> tuple[Path, dict, dict]:
    from autonom_lib import report_model as report_model_mod
    from autonom_lib.flow import report as flow_report

    run_dir = _resolve_run_dir(record, run_id)
    manifest = flow_report.load_manifest(run_dir)
    return run_dir, manifest, report_model_mod.compile_manifest(manifest)


def cmd_report_model(args: argparse.Namespace) -> int:
    record = _session_by_id(args.session)
    run_dir, _manifest, model = _report_model(record, args.run)
    out = _output_path(args.out if args.out else run_dir / "report-model-v2.json", "--out")
    out.write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.chmod(out, 0o600)
    return emit({"ok": True, "schema": model["schema"],
                 "run_id": model["attempt"]["run_id"], "out": str(out)},
                as_json=True)


def cmd_report_bundle(args: argparse.Namespace) -> int:
    from autonom_lib import report_bundle as report_bundle_mod
    from autonom_lib.flow import report as flow_report

    record = _session_by_id(args.session)
    run_dir = _resolve_run_dir(record, args.run)
    manifest = flow_report.load_manifest(run_dir)
    out = Path(args.out) if args.out else run_dir / "bundle-v2"
    result = report_bundle_mod.build(
        manifest, artifacts_root=Path(record["artifacts_dir"]), out=out)
    return emit({"ok": True, **result}, as_json=True)


def cmd_report_verify(args: argparse.Namespace) -> int:
    from autonom_lib import report_bundle as report_bundle_mod
    from autonom_lib.pretest import pack as pretest_pack

    path = Path(args.bundle)
    if pretest_pack.is_pack(path):
        # a pre-test pack: every digest, nested flow bundles included
        return emit(pretest_pack.verify(path), as_json=True)
    return emit(report_bundle_mod.verify(path), as_json=True)


def cmd_report_annotate(args: argparse.Namespace) -> int:
    from autonom_lib import report_bundle as report_bundle_mod

    return emit({"ok": True, **report_bundle_mod.annotate(
        Path(args.bundle), args.text, author=args.author, step_id=args.step)},
        as_json=True)


def cmd_report_gate(args: argparse.Namespace) -> int:
    from autonom_lib import gates as gates_mod

    out = _output_path(args.out, "--out") if args.out else None
    record = _session_by_id(args.session)
    _run_dir, _manifest, model = _report_model(record, args.run)
    result = gates_mod.evaluate(
        model, gates_mod.load_rules(Path(args.rules) if args.rules else None))
    if out is not None:
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        result["out"] = str(out)
    emit({"ok": True, **result}, as_json=True)
    return 0 if result["passed"] else 1


def cmd_report_history(args: argparse.Namespace) -> int:
    from autonom_lib import gates as gates_mod
    from autonom_lib import report_model as report_model_mod

    record = _session_by_id(args.session)
    manifests = _suite_manifests(record, args.last)
    result = gates_mod.history([
        report_model_mod.compile_manifest(manifest) for manifest in manifests])
    if args.out:
        out = _output_path(args.out, "--out")
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        result["out"] = str(out)
    return emit({"ok": True, **result}, as_json=True)


def cmd_report_pack(args: argparse.Namespace) -> int:
    from autonom_lib import campaign as campaign_mod

    bundle = Path(args.bundle)
    out = Path(args.out)
    return emit(campaign_mod.pack(bundle, out, shard_id=args.shard_id),
                as_json=True)


def cmd_report_merge(args: argparse.Namespace) -> int:
    from autonom_lib import campaign as campaign_mod

    result = campaign_mod.merge(
        [Path(path) for path in args.packs], Path(args.out),
        expected_shards=args.expected_shards)
    emit({"ok": True, **result}, as_json=True)
    return 0 if result["missing_shards"] == 0 else 1


def cmd_report_finalize(args: argparse.Namespace) -> int:
    from autonom_lib import campaign as campaign_mod

    result = campaign_mod.finalize(
        Path(args.campaign),
        rules_path=Path(args.rules) if args.rules else None)
    publication = None
    if args.publish:
        try:
            publication = campaign_mod.publish(Path(args.campaign), args.publish)
        except errors.AutonomError as exc:
            publication = {"publication_status": "failed", "error_code": exc.code,
                           "error": exc.message}
    result["publication"] = publication
    emit({"ok": True, **result}, as_json=True)
    return 0 if result["status"] == "passed" else 1


def cmd_report_watch(args: argparse.Namespace) -> int:
    """Incrementally emit newly finalized run summaries as NDJSON."""
    from autonom_lib import report_model as report_model_mod

    record = _session_by_id(args.session)
    flows_dir = Path(record["artifacts_dir"]) / "flows"
    seen: set[str] = set()
    deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
    emitted = 0
    while True:
        for path in sorted(flows_dir.glob("*/manifest.json"),
                           key=lambda item: item.stat().st_mtime):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            manifest = json.loads(path.read_text(encoding="utf-8"))
            model = report_model_mod.compile_manifest(manifest)
            print(json.dumps(report_model_mod.summary(model), ensure_ascii=False),
                  flush=True)
            emitted += 1
            if args.max_runs and emitted >= args.max_runs:
                return 0
        if deadline is not None and time.monotonic() >= deadline:
            return 0
        time.sleep(max(0.05, args.interval_ms / 1000))


def cmd_report_suite(args: argparse.Namespace) -> int:
    """One page for the whole session — the suite view CI and humans read."""
    from autonom_lib.flow import report as flow_report

    record = _session_by_id(args.session)
    # --last N: the latest run of each of the N flows that ran most recently;
    # a re-run replaces its flow's earlier run instead of pushing another
    # flow out of the window (report history still counts runs)
    manifests = flow_report.latest_runs_per_flow(_suite_manifests(record, None), args.last)
    out_dir = _output_dir(args.out if args.out else Path(record["artifacts_dir"]) / "flows",
                          "--out")
    base = Path(args.relative_to).resolve() if args.relative_to else None
    artifacts_dir = Path(record["artifacts_dir"])
    sensitive_run = any(m.get("sensitive") for m in manifests)
    # a sensitive run's frames may show private data: keep the whole tree
    # owner-only, exactly as `report build` does for a single run
    mode = 0o600 if sensitive_run else 0o644
    pages: dict[str, str] = {}
    if args.detailed:
        assets = out_dir / "assets"
        runs_dir = out_dir / "runs"
        # a stale page or asset dir from a previous, different run must not
        # survive into this site and be read as current evidence
        for stale in (runs_dir, assets):
            if stale.is_dir() and stale.resolve().is_relative_to(out_dir.resolve()):
                shutil.rmtree(stale)
        runs_dir.mkdir(parents=True, exist_ok=True)
        for manifest in manifests:
            run_id = str(manifest.get("run_id") or "run")
            evidence = flow_report._step_assets(
                manifest, artifacts_dir, assets, args.screenshots,
                file_mode=mode)
            page = runs_dir / f"{run_id}.html"
            page.write_text(
                flow_report.render_run_page(manifest, evidence, base=base)
                .replace('href="index.html"', 'href="../index.html"')
                .replace("src='assets/", "src='../assets/")
                .replace("href='assets/", "href='../assets/"),
                encoding="utf-8")
            os.chmod(page, mode)
            pages[run_id] = f"runs/{run_id}.html"
    html_path = out_dir / ("index.html" if pages else "suite.html")
    html_path.write_text(
        flow_report.render_suite_html(manifests, base=base, pages=pages or None),
        encoding="utf-8")
    os.chmod(html_path, mode)
    junit_path = out_dir / "suite.xml"
    junit_path.write_text(flow_report.render_suite_junit(manifests),
                          encoding="utf-8")
    os.chmod(junit_path, mode)  # CI must be able to read the JUnit it consumes
    failed = [m for m in manifests if m.get("status") == "failed"]
    replayed = [m for m in manifests if m.get("status") == "replayed"]
    interrupted = [m for m in manifests if m.get("status") == "interrupted"]
    # exactly what suite.html calls failed: any run that neither passed nor
    # was a prefix replay (an interrupted run included) fails the suite
    not_passed = [m for m in manifests if m.get("status") not in ("passed", "replayed")]
    # the same counts suite.xml carries, so the JSON and the JUnit agree; a
    # broken run (aborted, often with no failed step) is counted on its own
    junit_counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for manifest in manifests:
        counts = flow_report.junit_counts(manifest)
        for key in junit_counts:
            junit_counts[key] += int(counts.get(key) or 0)
    payload = {
        "ok": True,
        "flows": len(manifests),
        "passed": sum(1 for m in manifests if m.get("status") == "passed"),
        "failed": len(failed),
        "broken": sum(1 for m in manifests if flow_report._is_broken(m)),  # noqa: SLF001
        "replayed": len(replayed),
        "junit_counts": junit_counts,
        "html": str(html_path),
        "junit": str(junit_path),
        "sensitive": any(m.get("sensitive") for m in manifests),
    }
    # additive only when present: a suite with no interrupted run answers
    # with exactly the keys it had before the control lease existed
    if interrupted:
        payload["interrupted"] = len(interrupted)
    if pages:
        payload["pages"] = len(pages)
        payload["screenshots"] = args.screenshots
    if not_passed:
        failures = []
        for m in not_passed:
            entry = {"flow": m.get("flow_name"), "flow_id": m.get("flow_id"),
                     "error_code": (m.get("primary_error") or {}).get("error_code")}
            if m.get("status") != "failed":
                entry["status"] = m.get("status")
            failures.append(entry)
        payload["failures"] = failures
    if args.open:
        import webbrowser
        payload["opened"] = bool(webbrowser.open(html_path.as_uri()))
    emit(payload, as_json=True)
    return 1 if not_passed else 0


def cmd_report_build(args: argparse.Namespace) -> int:
    from autonom_lib import report_bundle as report_bundle_mod
    from autonom_lib.flow import report as flow_report

    record = _session_by_id(args.session)
    run_dir = _resolve_run_dir(record, args.run)
    manifest = flow_report.load_manifest(run_dir)
    artifacts_dir = Path(record["artifacts_dir"])
    html_path = run_dir / "report.html"
    html_path.write_text(flow_report.render_html(manifest, artifacts_dir),
                         encoding="utf-8")
    os.chmod(html_path, 0o600)
    junit_path = run_dir / "report.xml"
    junit_path.write_text(flow_report.render_junit(manifest), encoding="utf-8")
    os.chmod(junit_path, 0o600)
    bundle = report_bundle_mod.build(
        manifest, artifacts_root=artifacts_dir, out=run_dir / "bundle-v2")
    return emit({
        "ok": True,
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "html": str(html_path),
        "junit": str(junit_path),
        "bundle": bundle["bundle"],
        "report_model": str(Path(bundle["bundle"]) / "model/report.json"),
        "sensitive": manifest.get("sensitive", False),
    }, as_json=True)


def cmd_report_open(args: argparse.Namespace) -> int:
    from autonom_lib.flow import report as flow_report

    record = _session_by_id(args.session)
    run_dir = _resolve_run_dir(record, args.run)
    html_path = run_dir / "report.html"
    if not html_path.is_file():
        manifest = flow_report.load_manifest(run_dir)
        html_path.write_text(
            flow_report.render_html(manifest, Path(record["artifacts_dir"])),
            encoding="utf-8")
        os.chmod(html_path, 0o600)
    import webbrowser
    opened = webbrowser.open(html_path.as_uri())
    return emit({"ok": True, "html": str(html_path), "opened": bool(opened)},
                as_json=True)


def cmd_report_export(args: argparse.Namespace) -> int:
    from autonom_lib import report_bundle as report_bundle_mod
    from autonom_lib import report_export as report_export_mod
    from autonom_lib import report_model as report_model_mod
    from autonom_lib.flow import report as flow_report

    record = _session_by_id(args.session)
    run_dir = _resolve_run_dir(record, args.run)
    manifest = flow_report.load_manifest(run_dir)
    if args.format in ("allure", "agent", "csv", "metrics"):
        bundle_dir = run_dir / "bundle-v2"
        report_bundle_mod.build(
            manifest, artifacts_root=Path(record["artifacts_dir"]), out=bundle_dir)
        model = report_model_mod.load(bundle_dir / "model/report.json")
        defaults = {
            "allure": run_dir / "allure-results",
            "agent": run_dir / "agent-report.json",
            "csv": run_dir / "steps.csv",
            "metrics": run_dir / "metrics.json",
        }
        out = Path(args.out) if args.out else defaults[args.format]
        result = report_export_mod.export(
            model, args.format, out, bundle_root=bundle_dir)
        return emit({"ok": True, **result,
                     "sensitive": manifest.get("sensitive", False)}, as_json=True)
    if args.format == "junit":
        rendered = flow_report.render_junit(manifest)
        default_name = "report.xml"
    else:
        rendered = flow_report.render_html(manifest,
                                           Path(record["artifacts_dir"]))
        default_name = "report.html"
    out = _output_path(args.out if args.out else run_dir / default_name, "--out")
    out.write_text(rendered, encoding="utf-8")
    os.chmod(out, 0o600)
    return emit({"ok": True, "format": args.format, "out": str(out),
                 "sensitive": manifest.get("sensitive", False)}, as_json=True)


def cmd_report_serve(args: argparse.Namespace) -> int:
    """Serve one local report with narrowly-scoped prefix replay controls."""
    import html
    import http.server
    import secrets as secrets_mod
    import urllib.parse

    from autonom_lib.flow import report as flow_report

    record = _session_by_id(args.session)
    initial_dir = _resolve_run_dir(record, args.run)
    initial_manifest = flow_report.load_manifest(initial_dir)
    target = platform_mod.resolve(args, session_record=record)
    token = secrets_mod.token_urlsafe(24)

    def page_for(manifest: dict[str, Any]) -> str:
        evidence = flow_report._inline_step_assets(  # one-file HTTP response
            manifest, Path(record["artifacts_dir"]))
        return flow_report.render_run_page(
            manifest, evidence, standalone=True,
            control_url="/replay", csrf_token=token)

    def replay(run_id: str, step_index: int) -> dict[str, Any]:
        run_dir = _resolve_run_dir(record, run_id)
        source = flow_report.load_manifest(run_dir)
        step = next((item for item in source.get("steps", [])
                     if item.get("index") == step_index), None)
        if step is None:
            raise errors.AutonomError(
                errors.FLOW_REPLAY_STEP_NOT_REACHED,
                f"run {run_id} has no recorded runtime step {step_index}",
            )
        path = Path(str(source.get("flow_path") or "")).resolve()
        root = flow_validator.workspace_root(path).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise errors.AutonomError(
                errors.FLOW_PATH_ESCAPES_WORKSPACE,
                "the source flow is no longer a valid file in its workspace",
                file=str(path),
            )
        secret_values: dict[str, str] = {}
        missing = []
        for name in source.get("secret_names") or []:
            value = os.environ.get(str(name))
            if value is None:
                missing.append(str(name))
            else:
                secret_values[str(name)] = value
        if missing:
            raise errors.AutonomError(
                errors.FLOW_SECRET_UNDEFINED,
                "prefix replay needs secret environment variables that are not set",
                hint="Restart report serve with the required names available in "
                     "its process environment.",
                secret_names=missing,
            )
        flow = flow_validator.validate_tree(path)
        config = flow_executor.RunConfig(
            env=dict(source.get("env") or {}), secrets=secret_values,
            stop_after_step=step_index,
            stop_after_step_id=step.get("step_id"),
            stop_after_expected_status=step.get("status"),
            evidence_mode="always",
            evidence_collect=("screenshot", "hierarchy", "logs", "network"),
        )
        result = flow_executor.Executor(target, record, config).run(flow)
        if result.status != "replayed":
            actual = (result.steps[-1].status if result.steps else result.status)
            raise errors.AutonomError(
                errors.FLOW_REPLAY_STEP_NOT_REACHED,
                "the flow did not reproduce the selected step outcome",
                hint="Inspect the new failed run; app state or conditional control "
                     "flow diverged from the source run.",
                requested_step=step_index,
                expected_status=step.get("status"), actual_status=actual,
                replay_run_id=result.run_id,
            )
        return {
            "status": result.status, "run_id": result.run_id,
            "target": result.replay_target, "events": result.events_path,
        }

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "AutonomReport/1"

        def _send(self, status: int, body: str,
                  content_type: str = "text/html; charset=utf-8") -> None:
            blob = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            route = urllib.parse.urlsplit(self.path).path
            try:
                if route == "/":
                    manifest = initial_manifest
                elif route.startswith("/run/"):
                    run_id = urllib.parse.unquote(route.removeprefix("/run/"))
                    manifest = flow_report.load_manifest(
                        _resolve_run_dir(record, run_id))
                else:
                    self._send(404, "<h1>Not found</h1>")
                    return
                self._send(200, page_for(manifest))
            except errors.AutonomError as exc:
                self._send(404, f"<h1>Report unavailable</h1><p>{html.escape(exc.message)}</p>")

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            if urllib.parse.urlsplit(self.path).path != "/replay":
                self._send(404, "<h1>Not found</h1>")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length < 1 or length > 4096:
                self._send(400, "<h1>Invalid request</h1>")
                return
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", errors="replace"),
                keep_blank_values=True)
            supplied = (form.get("token") or [""])[0]
            if not secrets_mod.compare_digest(supplied, token):
                self._send(403, "<h1>Replay token rejected</h1>")
                return
            try:
                run_id = (form.get("run_id") or [""])[0]
                step_index = int((form.get("step_index") or [""])[0])
                result = replay(run_id, step_index)
            except (ValueError, errors.AutonomError) as exc:
                message = (exc.message if isinstance(exc, errors.AutonomError)
                           else "step index must be an integer")
                self._send(409, "<h1>Replay did not complete</h1>"
                           f"<p>{html.escape(message)}</p><p><a href='/'>Back</a></p>")
                return
            new_run = urllib.parse.quote(str(result["run_id"]), safe="")
            body = ("<h1>State reconstructed</h1>"
                    f"<p>Flow replay stopped after step {step_index}; cleanup hooks "
                    "were skipped so the target remains at that state.</p>"
                    f"<p><a href='/run/{new_run}'>Open replay evidence</a></p>")
            self._send(200, body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}/"
    emit({"ok": True, "url": url, "run_id": initial_manifest.get("run_id"),
          "control": "prefix-replay", "bind": "127.0.0.1"}, as_json=True)
    sys.stdout.flush()
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# --- blueprint control surfaces ---------------------------------------------


def cmd_capabilities(args: argparse.Namespace) -> int:
    # A capability snapshot describes a target, not a session: with no
    # session it is taken against the explicit (or sole ready) target, from a
    # record that declares nothing attached and no tooling probed yet.
    record = session_mod.load_current()
    target = _target(args)
    if record is None or record.get("target_id") != target.target_id:
        record = {"platform": target.platform, "target_id": target.target_id,
                  "tooling": {}, "network": {}, "session_id": None}
    if getattr(args, "probe", False):
        # --probe reads the current UI once, so ui.accessibility / ui.input
        # describe what was measured, not only what is installed.
        ui_mod.tree(target)
        if target.platform == IOS:
            from autonom_lib import ui_ios
            observed = ui_ios.observation(target.target_id)
            if observed:
                record = {**record, "ui_observation": observed}
    snapshot = providers_mod.open_session(target, record).capabilities()
    return emit({"ok": True, "session_id": record.get("session_id"),
                 **snapshot.as_dict()}, as_json=True)


def cmd_tour(args: argparse.Namespace) -> int:
    from autonom_lib import tour as tour_mod

    payload, text = tour_mod.command(args)
    if getattr(args, "human", False):
        # The one verb written for a person first: --human prints the
        # Markdown account to stdout instead of the JSON envelope.
        print(text)
        global _LAST_EMIT
        _LAST_EMIT = {k: v for k, v in payload.items() if k != "run"}
        return 0
    return emit(payload, as_json=True)


def cmd_teach_start(args: argparse.Namespace) -> int:
    return emit({"ok": True, **teach_mod.start(
        session_mod.require_current(), args.name)}, as_json=True)


def cmd_teach_mark(args: argparse.Namespace) -> int:
    return emit({"ok": True, **teach_mod.mark(
        session_mod.require_current(), args.name)}, as_json=True)


def cmd_teach_stop(args: argparse.Namespace) -> int:
    return emit({"ok": True, **teach_mod.stop(
        session_mod.require_current())}, as_json=True)


def cmd_teach_show(args: argparse.Namespace) -> int:
    state = teach_mod.load(session_mod.require_current())
    return emit({"ok": True, **state}, as_json=True)


def cmd_teach_compile(args: argparse.Namespace) -> int:
    result = teach_mod.compile_recording(
        session_mod.require_current(), out=Path(args.out),
        recording_id=args.recording, from_marker=args.from_marker,
        to_marker=args.to_marker)
    return emit({"ok": True, **result}, as_json=True)


def cmd_teach_approve(args: argparse.Namespace) -> int:
    # refused before anything replays: an approval with zero runs proves nothing
    teach_mod._require_minimum_runs(args.minimum_runs)  # noqa: SLF001
    env_overrides = _flow_env_overrides(args)
    secrets = _flow_secrets(args)
    record = session_mod.require_current()
    replays: list[dict[str, Any]] = []
    if getattr(args, "run", False):
        # `approve` only counts replays that already happened; with --run it
        # performs them here, against the session's target, and stops at the
        # first failure — an approval is a claim about consecutive passes.
        # teach.replay binds each replay to the flow's bytes (flow_sha256).
        target = _target(args)
        replays = teach_mod.replay(record, target, Path(args.flow),
                                   runs=args.minimum_runs, env=env_overrides,
                                   secrets=secrets)
    result = teach_mod.approve(record, Path(args.flow), minimum_runs=args.minimum_runs)
    payload = {"ok": True, **result}
    if replays:
        payload["replays"] = replays
    return emit(payload, as_json=True)


def cmd_app_skill_validate(args: argparse.Namespace) -> int:
    return emit(app_skills_mod.validate(Path(args.workspace).resolve(), args.app_id),
                as_json=True)


def cmd_app_skill_promote(args: argparse.Namespace) -> int:
    return emit(app_skills_mod.promote(
        Path(args.workspace).resolve(), args.app_id, Path(args.flow),
        approval=Path(args.approval) if args.approval else None), as_json=True)


def _source_for_replay(args: argparse.Namespace) -> tuple[dict, dict]:
    from autonom_lib import report_bundle as report_bundle_mod
    from autonom_lib import report_model as report_model_mod
    from autonom_lib.flow import report as flow_report

    if args.bundle:
        bundle = Path(args.bundle)
        report_bundle_mod.verify(bundle)
        source = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
        source["_bundle_root"] = str(bundle.resolve())
        return source, report_model_mod.load(bundle / "model/report.json")
    record = _session_by_id(args.session)
    run_dir = _resolve_run_dir(record, args.run)
    manifest = flow_report.load_manifest(run_dir)
    return manifest, report_model_mod.compile_manifest(manifest)


def cmd_replay(args: argparse.Namespace) -> int:
    from autonom_lib.flow import inline as flow_inline

    source, model = _source_for_replay(args)
    if str(source.get("flow_path") or "") == flow_inline.INLINE_PATH:
        # A `ui do` run: its steps came on stdin and no flow file holds them.
        # None is written for replay, because a sensitive value typed by a
        # step would then sit on disk in clear, and a masked one would make
        # the replay type the mask instead of the value.
        run_id = source.get("run_id")
        raise errors.AutonomError(
            errors.REPLAY_MANIFEST_INVALID,
            f"run {run_id or '(unknown)'} ran steps sent to 'ui do' on stdin; there is "
            "no flow file to replay",
            hint="Send the same steps to 'autonom ui do' again, or save them as a flow "
                 "file (a 'schema:' and 'name:' header, a '---' line, then the steps) "
                 "and run 'autonom flow run <file>'.",
            flow=flow_inline.INLINE_PATH, **({"run_id": run_id} if run_id else {}))
    raw_flow_path = Path(str(source.get("flow_path") or ""))
    flow_path = ((Path(source["_bundle_root"]) / raw_flow_path).resolve()
                 if source.get("_bundle_root") and not raw_flow_path.is_absolute()
                 else raw_flow_path.resolve())
    if not flow_path.is_file():
        raise errors.AutonomError(
            errors.REPLAY_MANIFEST_INVALID,
            "the pinned replay flow is unavailable",
            hint="Restore the source checkout or keep the flow beside the bundle.",
            flow=str(flow_path))
    step = None
    if args.to_step:
        needle = str(args.to_step)
        step = next((item for item in model.get("steps") or []
                     if str(item.get("index")) == needle
                     or item.get("step_id") == needle
                     or (item.get("command") == "checkpoint"
                         and item.get("name") == needle)), None)
        if step is None:
            raise errors.AutonomError(
                errors.FLOW_REPLAY_STEP_NOT_REACHED,
                f"source attempt has no step or checkpoint {needle!r}")
    secret_values: dict[str, str] = {}
    missing = []
    for name in source.get("secret_names") or []:
        if os.environ.get(name) is None:
            missing.append(name)
        else:
            secret_values[name] = os.environ[name]
    if missing:
        raise errors.AutonomError(
            errors.FLOW_SECRET_UNDEFINED,
            "replay requires secret environment variables that are not set",
            secret_names=missing)
    record = session_mod.require_current()
    target = _target(args)
    flow = flow_validator.validate_tree(flow_path)
    config = flow_executor.RunConfig(
        env=dict(source.get("env") or {}), secrets=secret_values,
        stop_after_step=step.get("index") if step else None,
        stop_after_step_id=step.get("step_id") if step else None,
        stop_after_expected_status=(
            "passed" if step and step.get("status") == "passed"
            else "failed" if step and step.get("status") in ("failed", "broken")
            else None),
        evidence_mode="always",
        evidence_collect=("screenshot", "hierarchy", "logs", "network"),
        parent_attempt_id=model["attempt"]["attempt_id"],
        retry_of=model["attempt"]["attempt_id"],
    )
    result = flow_executor.Executor(target, record, config).run(flow)
    payload = {
        "ok": True, "status": result.status, "run_id": result.run_id,
        "parent_attempt_id": model["attempt"]["attempt_id"],
        "strategy": "baseline", "target": result.replay_target,
        "events": result.events_path,
    }
    emit(payload, as_json=True)
    return 0 if result.status in ("passed", "replayed") else 1


def cmd_simulator(args: argparse.Namespace) -> int:
    values: dict[str, Any] = {}
    if args.json:
        parsed = json.loads(args.json)
        if not isinstance(parsed, dict):
            raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                      "--json must be an object")
        values.update(parsed)
    for pair in args.value or []:
        if "=" not in pair:
            raise errors.AutonomError(errors.FLOW_COMMAND_INVALID,
                                      f"--value takes KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        values[key] = value
    target = _target(args)
    # the session record defaults come from (the app `push send` addresses)
    result = simulator_mod.apply(target, args.control, args.action, values,
                                 session=session_mod.load_current())
    return emit({"ok": True, **result, **target.identity()}, as_json=True)


def cmd_ci_run(args: argparse.Namespace) -> int:
    """Supervise capture → bundle → shard → merge → gate → publish."""
    from autonom_lib import campaign as campaign_mod
    from autonom_lib import report_bundle as report_bundle_mod
    from autonom_lib.flow import report as flow_report

    path = Path(args.path)
    files = _flow_files(path) if path.is_dir() else [path]
    if not files:
        raise errors.AutonomError(errors.FLOW_NO_FLOWS_FOUND,
                                  f"no flows under {path}")
    flows = [flow_validator.validate_tree(item) for item in files]
    record = session_mod.require_current()
    target = _target(args)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    campaign = campaign_mod.create_campaign(
        out / "campaign-spec.json",
        expected_shards=args.expected_shards or len(flows),
        campaign_id=args.campaign_id)
    phase_path = out / "ci-state.json"
    state: dict[str, Any] = {
        "schema": "autonom.ci-state/v1", "campaign_id": campaign["campaign_id"],
        "phase": "capture", "runs": [], "errors": [],
        "spool": str(out), "resumable": True,
    }

    def save_state() -> None:
        phase_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")

    save_state()
    packs: list[Path] = []
    for index, flow in enumerate(flows, 1):
        config = flow_executor.RunConfig(
            env=_flow_env_overrides(args), secrets=_flow_secrets(args),
            evidence_mode="always",
            evidence_collect=("screenshot", "hierarchy", "logs", "network"),
            campaign_id=campaign["campaign_id"],
            shard_id=f"{args.shard_id}-{index}",
        )
        try:
            result = flow_executor.Executor(target, record, config).run(flow)
            run_dir = Path(result.events_path).parent
            manifest = flow_report.load_manifest(run_dir)
            bundle_dir = out / "bundles" / result.run_id
            bundle = report_bundle_mod.build(
                manifest, artifacts_root=Path(record["artifacts_dir"]), out=bundle_dir)
            pack_path = out / "packs" / f"{args.shard_id}-{index}.zip"
            packed = campaign_mod.pack(bundle_dir, pack_path,
                                       shard_id=f"{args.shard_id}-{index}")
            packs.append(pack_path)
            state["runs"].append({"flow": flow.path, "run_id": result.run_id,
                                  "status": result.status, "bundle": bundle["bundle"],
                                  "pack": packed["out"]})
        except errors.AutonomError as exc:
            state["errors"].append(exc.as_dict())
            save_state()
            break
        save_state()

    state["phase"] = "merge"
    save_state()
    merged = campaign_mod.merge(
        packs, out / "campaign",
        expected_shards=args.expected_shards or len(flows))
    state["phase"] = "finalize"
    save_state()
    final = campaign_mod.finalize(
        out / "campaign", rules_path=Path(args.rules) if args.rules else None)
    publication = {"publication_status": "not_requested"}
    if args.publish:
        state["phase"] = "publish"
        save_state()
        try:
            publication = campaign_mod.publish(out / "campaign", args.publish)
        except errors.AutonomError as exc:
            publication = {"publication_status": "failed",
                           "error_code": exc.code, "error": exc.message}
    state.update({"phase": "complete", "final": final,
                  "publication": publication})
    save_state()
    payload = {"ok": True, "campaign_id": campaign["campaign_id"],
               "status": final["status"], "execution": merged["summary"],
               "missing_shards": merged["missing_shards"],
               "publication": publication, "out": str(out),
               "state": str(phase_path)}
    emit(payload, as_json=True)
    if publication.get("publication_status") == "failed":
        return 2
    return 0 if final["status"] == "passed" and not state["errors"] else 1


def cmd_ci_publish(args: argparse.Namespace) -> int:
    from autonom_lib import campaign as campaign_mod

    return emit({"ok": True, **campaign_mod.publish(
        Path(args.campaign), args.destination)}, as_json=True)


def _agent_model(args: argparse.Namespace) -> tuple[dict[str, Any], Path | None]:
    from autonom_lib import report_bundle as report_bundle_mod
    from autonom_lib import report_model as report_model_mod

    if args.bundle:
        bundle = Path(args.bundle)
        report_bundle_mod.verify(bundle)
        return report_model_mod.load(bundle / "model/report.json"), bundle
    record = _session_by_id(args.session)
    _run_dir, _manifest, model = _report_model(record, args.run)
    return model, None


def cmd_agent_export(args: argparse.Namespace) -> int:
    from autonom_lib import report_export as report_export_mod

    model, bundle = _agent_model(args)
    out = Path(args.out)
    return emit({"ok": True, **report_export_mod.agent(model, out)}, as_json=True)


def cmd_agent_inspect(args: argparse.Namespace) -> int:
    model, _bundle = _agent_model(args)
    needle = str(args.step)
    step = next((item for item in model.get("steps") or []
                 if item.get("step_id") == needle
                 or str(item.get("index")) == needle), None)
    if step is None:
        raise errors.AutonomError(errors.FLOW_REPLAY_STEP_NOT_REACHED,
                                  f"report has no step {needle!r}")
    attachment_ids = set(step.get("attachment_ids") or [])
    attachments = [item for item in model.get("attachments") or []
                   if item["attachment_id"] in attachment_ids]
    return emit({"ok": True, "attempt": model["attempt"], "step": step,
                 "attachments": attachments}, as_json=True)


CANVAS_TRANSPORTS = ("auto", "scrcpy", "idb", "screenrecord", "screencap")
# The iOS Simulator fast transport: an idb_companion the Canvas starts and owns. Its
# binary comes from --idb-companion, AUTONOM_IDB_COMPANION_BIN, then PATH, exactly as
# resolveIdbCompanionBinary in ios-idb-companion.mjs finds it. AUTONOM_IDB_COMPANION is
# the remote companion of every idb call and never names this binary.
CANVAS_IDB_CAPABILITY = "canvas.idb"
CANVAS_IDB_BIN_ENV = "AUTONOM_IDB_COMPANION_BIN"
CANVAS_IDB_HINT = ("Install it with `brew install facebook/fb/idb-companion`, set "
                   "AUTONOM_IDB_COMPANION_BIN to its path, or pass --idb-companion PATH.")
# The same bounds as parseArgs in browser-lib.mjs, which also takes 0 (native size).
CANVAS_MAX_SIZE = (320, 4096)
CANVAS_MAX_SIZE_HINT = ("Without --max-size scrcpy follows the device's H.264 encoder (1280 when "
                        "software only, native with a hardware one, 1280 when it cannot tell); "
                        "screenrecord uses 1280.")
CANVAS_BIT_RATE = (100_000, 100_000_000)
CANVAS_CONTROL_IDLE = (15, 3600)
SCRCPY_VERSION_TEXT = re.compile(doctor_mod.SCRCPY_VERSION_TOKEN)


def _canvas_bounded(flag: str, value: int | None, bounds: tuple[int, int],
                    default: str) -> None:
    if value is not None and not bounds[0] <= value <= bounds[1]:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"{flag} must be {bounds[0]}..{bounds[1]}, got {value}",
            f"The default is {default}.")


def _canvas_scrcpy_server(args: argparse.Namespace) -> str | None:
    """The explicit scrcpy-server file as an absolute path, or None.

    Discovery without the flag (environment, an installed scrcpy) and the
    version check belong to the node server; only what the flags say is
    checked here, so a typo fails before node starts.
    """
    version = args.scrcpy_version
    if version is not None:
        if not SCRCPY_VERSION_TEXT.fullmatch(version):
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--scrcpy-version must look like 4.1, got {version!r}",
                "Pass the scrcpy release the server file came from.")
        if args.scrcpy_server is None:
            raise errors.AutonomError(
                errors.INVALID_VALUE, "--scrcpy-version names the version of --scrcpy-server",
                "Pass --scrcpy-server PATH as well, or neither to use the installed scrcpy.")
    if args.scrcpy_server is None:
        return None
    server = Path(args.scrcpy_server).expanduser()
    if not server.is_file():
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--scrcpy-server is not a file: {server}",
            doctor_mod.SCRCPY_INSTALL_HINT, path=str(server))
    # Absolute but not resolved: a link such as scrcpy-server-v4.1 -> scrcpy-server keeps
    # the name the node server reads the version from.
    return str(server.absolute())


def _canvas_executable_file(path: str) -> bool:
    # A directory passes the execute check too: it must be a file.
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _canvas_idb_lookup(args: argparse.Namespace) -> dict[str, Any]:
    """The idb_companion binary of the Canvas, found as the node server finds it.

    `--idb-companion`, else AUTONOM_IDB_COMPANION_BIN when set, else the first
    executable `idb_companion` file in PATH; a flag or variable that names no
    executable file is reported, never skipped. Returns `available`, `path`
    (absolute for the flag), `source` and `reason`, with the reasons of
    resolveIdbCompanionBinary in ios-idb-companion.mjs.
    """
    if args.idb_companion is not None:
        source, path = "--idb-companion", str(Path(args.idb_companion).expanduser())
    elif os.environ.get(CANVAS_IDB_BIN_ENV):
        source, path = CANVAS_IDB_BIN_ENV, os.environ[CANVAS_IDB_BIN_ENV]
    else:
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(directory, "idb_companion") if directory else ""
            if candidate and _canvas_executable_file(candidate):
                return {"available": True, "path": candidate, "source": "PATH", "reason": None}
        return {"available": False, "path": None, "source": None,
                "reason": f"idb_companion was not found (--idb-companion, {CANVAS_IDB_BIN_ENV} or PATH)"}
    if _canvas_executable_file(path):
        return {"available": True, "path": os.path.abspath(path), "source": source, "reason": None}
    return {"available": False, "path": path, "source": source,
            "reason": f"{source} is not an executable file: {path}"}


import contextlib  # noqa: E402
import signal  # noqa: E402
import urllib.parse  # noqa: E402

CANVAS_SCRIPT = (ROOT.parent / "plugins/autonom/skills/android-emulator-browser/scripts/"
                 "android-emulator-browser.mjs")
# A pid the CLI may signal runs this file; a reused pid running anything else never is.
CANVAS_MARKER = "android-emulator-browser.mjs"
CANVAS_DISCOVERY_SCHEMA = "autonom-canvas/v1"
CANVAS_DEFAULT_WORKSPACE = "default"
CANVAS_MAX_DEVICES = 8
CANVAS_MAX_BOOTABLE = 8
CANVAS_DEVICE_SPEC = re.compile(r"^(android|ios):([A-Za-z0-9._:-]{1,128})$")
CANVAS_WORKSPACE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
CANVAS_BOOTABLE_AVD = re.compile(r"^avd:([A-Za-z0-9._-]{1,100})(?:@([0-9]{4}))?$")
CANVAS_BOOTABLE_SIMULATOR = re.compile(r"^simulator:[0-9A-Fa-f-]{36}$")
CANVAS_STOP_WAIT_S = 10.0
CANVAS_WORKSPACE_FLAGS_HINT = "Drop --single: every other Canvas is a workspace."


def _canvas_device_specs(values: list[str] | None) -> list[tuple[str, str]]:
    """`--device PLATFORM:ID` entries as (platform, target), checked as node checks them."""
    specs: list[tuple[str, str]] = []
    for raw in values or []:
        match = CANVAS_DEVICE_SPEC.fullmatch(raw)
        if not match:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--device must be android:<serial> or ios:<udid>, got {raw!r}",
                "For example --device android:emulator-5580 (letters, digits, . _ : - up to 128).")
        spec = (match.group(1), match.group(2))
        if spec in specs:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--device names {raw} twice", "Name each device once.")
        specs.append(spec)
    if len(specs) > CANVAS_MAX_DEVICES:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--device may be given at most {CANVAS_MAX_DEVICES} times",
            "A Canvas shows at most 8 devices (4 per tab).")
    return specs


def _canvas_bootable(values: list[str] | None) -> list[str]:
    entries = list(values or [])
    if len(entries) > CANVAS_MAX_BOOTABLE:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--bootable may be given at most {CANVAS_MAX_BOOTABLE} times",
            "List the AVDs and Simulators the page may boot, at most 8.")
    for raw in entries:
        avd = CANVAS_BOOTABLE_AVD.fullmatch(raw)
        if avd:
            port = avd.group(2)
            if port is not None and not (5554 <= int(port) <= 5682 and int(port) % 2 == 0):
                raise errors.AutonomError(
                    errors.INVALID_VALUE, f"--bootable {raw}: the port must be even, 5554..5682",
                    "For example --bootable avd:Pixel_9@5586.")
            continue
        if CANVAS_BOOTABLE_SIMULATOR.fullmatch(raw):
            continue
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--bootable must be avd:NAME[@PORT] or simulator:UDID, got {raw!r}",
            "For example --bootable avd:Pixel_9@5586 or --bootable simulator:<36-character UDID>.")
    return entries


def _canvas_install_roots(values: list[str] | None) -> list[str]:
    roots: list[str] = []
    for raw in values or []:
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--install-root is not a directory: {path}",
                "Name a folder that holds the builds the page may install, for example ~/src/app/build.",
                path=str(path))
        roots.append(str(path.absolute()))
    return roots


def _canvas_state_base() -> Path:
    """The Canvas files' base (contract 0): the process registry's base, not the sessions'."""
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        return Path(explicit)
    state = os.environ.get("XDG_STATE_HOME")
    return Path(state) / "autonom" if state else Path.home() / ".local/state/autonom"


def _canvas_pid_is_canvas(pid: int) -> bool:
    """A live pid that runs the Canvas server (ps cannot answer: trust the pid)."""
    if not session_mod.pid_alive(pid):
        return False
    command = processes_mod.command_of(pid)
    return command is None or CANVAS_MARKER in command


def _canvas_discovery() -> list[dict[str, Any]]:
    """Every live Canvas from its discovery file, port order; files of dead ones are removed."""
    directory = _canvas_state_base() / "canvas"
    live: list[dict[str, Any]] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return live
    for path in files:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict) or document.get("schema") != CANVAS_DISCOVERY_SCHEMA:
            continue
        pid, port = document.get("pid"), document.get("port")
        if not isinstance(pid, int) or not isinstance(port, int) or isinstance(pid, bool):
            continue
        if not _canvas_pid_is_canvas(pid):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        live.append(document)
    live.sort(key=lambda item: item["port"])
    return live


def _canvas_url(document: dict[str, Any]) -> str:
    url = document.get("url") or f"http://127.0.0.1:{document['port']}/"
    token = document.get("token")
    return f"{url}#token={urllib.parse.quote(token, safe='')}" if token else url


def _canvas_select(args: argparse.Namespace, live: list[dict[str, Any]]) -> dict[str, Any]:
    """`--port`, else `--workspace`, else the only live Canvas (contract 6.1)."""
    port = getattr(args, "canvas_port", None)
    workspace = getattr(args, "canvas_workspace", None)
    if port is not None:
        matches = [item for item in live if item["port"] == port]
        wanted = f"on port {port}"
    elif workspace is not None:
        matches = [item for item in live if item.get("workspace") == workspace]
        wanted = f"for workspace {workspace}"
    else:
        matches, wanted = live, "running"
    if not matches:
        raise errors.AutonomError(
            errors.CANVAS_NOT_FOUND, f"no Canvas is {wanted}" if wanted != "running" else "no Canvas is running",
            "Start one with `autonom canvas`, or run `autonom canvas list`.")
    if len(matches) > 1:
        raise errors.AutonomError(
            errors.CANVAS_AMBIGUOUS, f"{len(matches)} Canvases are running",
            "Pass --port N or --workspace NAME; `autonom canvas list` shows them.",
            canvases=[{"port": item["port"], "workspace": item.get("workspace")} for item in matches])
    return matches[0]


def _canvas_api(document: dict[str, Any], method: str, path: str,
                body: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    """One authenticated call to a Canvas API; its error answers are raised with their code."""
    import urllib.error
    import urllib.request

    headers = {"Accept": "application/json", "X-Autonom-Origin": "agent"}
    token = document.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if method == "POST":
        data = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{document['port']}{path}", data=data, headers=headers, method=method)
    # A configured HTTP proxy (http_proxy, system settings) must never see the Bearer token:
    # the Canvas is loopback only, so this opener skips every proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - loopback
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # The API's extras ride along; names the error object itself uses never do.
        extra = {key: value for key, value in payload.items()
                 if key not in ("ok", "error", "error_code", "hint", "code", "message")}
        extra.update(status=exc.code, port=document["port"])
        code = payload.get("error_code")
        raise errors.AutonomError(
            code if isinstance(code, str) and code else errors.BACKEND_FAILED,
            str(payload.get("error") or f"the Canvas answered HTTP {exc.code}"),
            payload.get("hint") if isinstance(payload.get("hint"), str) else None, **extra) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"the Canvas on port {document['port']} did not answer: {reason}",
            "Check it with `autonom canvas list`; stop a stuck one with `autonom canvas stop`.",
            port=document["port"]) from exc
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"the Canvas on port {document['port']} answered no JSON",
            port=document["port"]) from exc
    return payload if isinstance(payload, dict) else {}


def _canvas_workspace_lock(name: str) -> dict[str, Any] | None:
    """The live owner {pid, port} of a workspace lock (contract 3.3), or None."""
    path = _canvas_state_base() / "canvas" / "workspaces" / f"{name}.lock"
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = holder.get("pid") if isinstance(holder, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or not _canvas_pid_is_canvas(pid):
        return None
    return {"pid": pid, "port": holder.get("port")}


def _canvas_check_values(args: argparse.Namespace) -> None:
    # validated here: the node bridge answered a bad value with a stack trace
    if not 0 <= args.port <= 65535:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--port must be 0..65535, got {args.port}",
            "The default is 3277; 0 picks a free port.")
    if args.fps is not None and not 1 <= args.fps <= 60:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--fps must be 1..60, got {args.fps}",
            "The default is 15 for multipart, 30 for software scrcpy encoders, and 60 for hardware/unknown scrcpy encoders and idb.")
    if args.max_size not in (None, 0) and not (
            CANVAS_MAX_SIZE[0] <= args.max_size <= CANVAS_MAX_SIZE[1]):
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--max-size must be 0 (native size) or {CANVAS_MAX_SIZE[0]}..{CANVAS_MAX_SIZE[1]}, "
            f"got {args.max_size}", CANVAS_MAX_SIZE_HINT)
    _canvas_bounded("--bit-rate", args.bit_rate, CANVAS_BIT_RATE, "12000000")
    _canvas_control_idle(args)


def _canvas_control_idle(args: argparse.Namespace) -> int | None:
    """`--control-idle SECONDS`: an integer 15..3600, refused before node starts."""
    raw = getattr(args, "control_idle", None)
    if raw is None:
        return None
    text = str(raw).strip()
    low, high = CANVAS_CONTROL_IDLE
    if not re.fullmatch(r"[0-9]{1,5}", text) or not low <= int(text) <= high:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--control-idle must be a whole number of seconds from {low} to {high}, got {raw!r}",
            "Omit it for the default of 120 seconds.", option="control_idle")
    return int(text)


def _canvas_control_args(args: argparse.Namespace) -> list[str]:
    """`--control-idle N` for node, only when given: the node argv of before stays
    byte for byte without it."""
    idle = _canvas_control_idle(args)
    return [] if idle is None else ["--control-idle", str(idle)]


def _canvas_shared_args(args: argparse.Namespace, scrcpy_server: str | None) -> list[str]:
    """Video and auth flags of both modes, in the order of the single-device command."""
    command: list[str] = []
    # Only explicit values are forwarded: an explicit --fps also caps scrcpy,
    # which otherwise caps software encoders at 30 fps and others at 60; multipart keeps 15.
    for flag, value in (("--fps", args.fps), ("--max-size", args.max_size),
                        ("--bit-rate", args.bit_rate)):
        if value is not None:
            command += [flag, str(value)]
    if scrcpy_server:
        command += ["--scrcpy-server", scrcpy_server]
        if args.scrcpy_version:
            command += ["--scrcpy-version", args.scrcpy_version]
    return command


def _canvas_action_args(args: argparse.Namespace, install_roots: list[str]) -> list[str]:
    command: list[str] = []
    for root in install_roots:
        command += ["--install-root", root]
    if args.captures_dir:
        command += ["--captures-dir", str(Path(args.captures_dir).expanduser().absolute())]
    return command


def _canvas_node() -> str:
    node = shutil.which("node")
    if not node:
        raise errors.AutonomError(
            errors.TOOL_MISSING, "node is required to serve Mobile Canvas",
            hint="Install Node.js or run the browser bridge on a host that has it.",
            tool="node")
    return node


def cmd_canvas(args: argparse.Namespace) -> int:
    """`autonom canvas` alone: `canvas serve` with its defaults (a workspace Canvas)."""
    defaults = (_ROOT_PARSER or build_parser()).parse_args(["canvas", "serve"])
    for key, value in vars(args).items():
        if key not in ("func", "canvas_command"):
            setattr(defaults, key, value)
    return cmd_canvas_serve(defaults)


def cmd_canvas_serve(args: argparse.Namespace) -> int:
    _canvas_check_values(args)
    devices = _canvas_device_specs(args.device)
    bootable = _canvas_bootable(args.bootable)
    if args.workspace is not None and not CANVAS_WORKSPACE_NAME.fullmatch(args.workspace):
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--workspace must be 1-40 letters, digits, . _ or -, got {args.workspace!r}",
            "The default workspace is `default`.")
    explicit = _explicit_target_flags(args)
    if devices and explicit:
        raise errors.AutonomError(
            errors.USAGE_ERROR, "--device cannot be combined with --target, --serial or --udid",
            "Name every device with --device PLATFORM:ID, or one target with the target flags.")
    # Every Canvas is a workspace (tabs, attach, boot, split); a named target is its first
    # device. `--single` alone keeps the one-device page of before.
    workspace_mode = not args.single
    install_roots = _canvas_install_roots(args.install_root)
    if not workspace_mode:
        if args.split or args.workspace is not None or len(devices) >= 2:
            raise errors.AutonomError(
                errors.USAGE_ERROR, "--single shows one device; it cannot be combined with "
                "--split, --workspace or several --device",
                "Drop --single for a workspace Canvas.")
        if not explicit and not devices:
            raise errors.AutonomError(
                errors.USAGE_ERROR, "--single needs one target",
                "Name it with --serial, --udid, --target or one --device.")
        for flag, given in (("--ephemeral", args.ephemeral), ("--bootable", bool(bootable)),
                            ("--bootable-only", args.bootable_only),
                            ("--shutdown-booted", args.shutdown_booted)):
            if given:
                raise errors.AutonomError(
                    errors.INVALID_VALUE, f"{flag} needs a workspace Canvas",
                    CANVAS_WORKSPACE_FLAGS_HINT)
        if devices:
            platform, target_id = devices[0]
            given_platform = getattr(args, "platform", None)
            if given_platform and given_platform != platform:
                raise errors.AutonomError(
                    errors.USAGE_ERROR, f"--platform {given_platform} does not match --device {platform}:{target_id}",
                    "Drop --platform; --device names the platform.")
            args.platform, args.target = platform, target_id
        return _canvas_serve_single(args, install_roots)
    if explicit:
        # `canvas serve --serial X`: the named target is the workspace's first device.
        target = _target(args)
        devices = [(target.platform, target.target_id)]
    return _canvas_serve_workspace(args, devices, bootable, install_roots)


def _canvas_serve_single(args: argparse.Namespace, install_roots: list[str]) -> int:
    """The single-device Canvas of before: same node command, same process owner."""
    scrcpy_server = _canvas_scrcpy_server(args)
    idb = _canvas_idb_lookup(args)
    target = _target(args)
    if target.platform == ANDROID and (args.transport == "idb" or args.idb_companion is not None):
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            "the idb transport mirrors iOS Simulators only",
            "On Android use --transport auto or scrcpy.",
            capability=CANVAS_IDB_CAPABILITY)
    if args.transport == "idb" and not idb["available"]:
        # The node server refuses with this same object when it is started directly.
        raise errors.AutonomError(
            errors.TOOL_MISSING, f"--transport idb is unavailable: {idb['reason']}",
            CANVAS_IDB_HINT, tool="idb_companion", capability=CANVAS_IDB_CAPABILITY)
    if args.idb_companion is not None and not idb["available"]:
        # A typo in the flag fails before node starts, whatever the transport.
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--idb-companion is not an executable file: {idb['path']}",
            CANVAS_IDB_HINT, path=idb["path"])
    idb_companion = idb["path"] if args.idb_companion is not None else None
    if target.platform != ANDROID and (args.transport == "scrcpy" or scrcpy_server):
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM,
            "the scrcpy transport mirrors Android targets only",
            "On the iOS Simulator use --transport auto.",
            capability=doctor_mod.SCRCPY_CAPABILITY)
    _canvas_require_scrcpy(args, scrcpy_server)
    node = _canvas_node()
    command = [node, str(CANVAS_SCRIPT), "--platform", target.platform,
               "--target", target.target_id, "--port", str(args.port),
               "--transport", args.transport]
    command += _canvas_shared_args(args, scrcpy_server)
    if target.platform == ANDROID:
        command += ["--adb", target.tool]
    else:
        command += ["--simctl", target.tool]
        if getattr(args, "idb", None):
            command += ["--idb", args.idb]
        if idb_companion:
            command += ["--idb-companion", idb_companion]
    if args.no_auth:
        command.append("--no-auth")
    if args.token:
        command += _canvas_token_args(args.token)
    # Additive: without these flags the command is the one of before, byte for byte.
    command += _canvas_action_args(args, install_roots)
    command += _canvas_control_args(args)
    # Supervised in its own process group and registered (supervisor and
    # node child) for its whole life, so `processes` lists the pair, `cleanup
    # --all` and `session stop` can stop it, and killing this CLI takes node
    # and its children down too. Owned by the session on the same target.
    # The command (it may carry --token) is never passed as registry detail.
    current = session_mod.load_current()
    owner = (current.get("session_id")
             if current and current.get("target_id") == target.target_id else None)
    return processes_mod.run_supervised(
        command, kind="canvas", owner=owner,
        artifacts_dir=current.get("artifacts_dir") if owner and current else None,
        target_id=target.target_id, port=args.port)


def _canvas_require_scrcpy(args: argparse.Namespace, scrcpy_server: str | None) -> None:
    if args.transport == "scrcpy" and not scrcpy_server and doctor_mod.scrcpy_source() is None:
        # Nothing at all could provide a server, so the node server would
        # refuse as well; refusing here keeps the JSON error contract. A
        # configured server's version is still checked by the node server.
        raise errors.AutonomError(
            errors.TOOL_MISSING,
            f"--transport scrcpy needs scrcpy-server {doctor_mod.SCRCPY_PROTOCOL_VERSION} "
            "and none is configured",
            doctor_mod.SCRCPY_INSTALL_HINT, tool="scrcpy",
            capability=doctor_mod.SCRCPY_CAPABILITY)


def _canvas_serve_workspace(args: argparse.Namespace, devices: list[tuple[str, str]],
                            bootable: list[str], install_roots: list[str]) -> int:
    """A workspace Canvas (contract 6.1): no device needed, no session owner, open until stopped."""
    name = args.workspace or CANVAS_DEFAULT_WORKSPACE
    scrcpy_server = _canvas_scrcpy_server(args)
    idb = _canvas_idb_lookup(args)
    platforms = {platform for platform, _target_id in devices}
    if len(platforms) > 1 and args.transport != "auto":
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--transport {args.transport} cannot serve Android and iOS devices together",
            "Use --transport auto with mixed platforms.")
    if ANDROID in platforms and (args.transport == "idb" or args.idb_companion is not None):
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM, "the idb transport mirrors iOS Simulators only",
            "On Android use --transport auto or scrcpy.", capability=CANVAS_IDB_CAPABILITY)
    if IOS in platforms and (args.transport == "scrcpy" or scrcpy_server):
        raise errors.AutonomError(
            errors.UNSUPPORTED_ON_PLATFORM, "the scrcpy transport mirrors Android targets only",
            "On the iOS Simulator use --transport auto.", capability=doctor_mod.SCRCPY_CAPABILITY)
    if args.transport == "idb" and not idb["available"]:
        raise errors.AutonomError(
            errors.TOOL_MISSING, f"--transport idb is unavailable: {idb['reason']}",
            CANVAS_IDB_HINT, tool="idb_companion", capability=CANVAS_IDB_CAPABILITY)
    if args.idb_companion is not None and not idb["available"]:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"--idb-companion is not an executable file: {idb['path']}",
            CANVAS_IDB_HINT, path=idb["path"])
    _canvas_require_scrcpy(args, scrcpy_server)
    # One Canvas per workspace: a second `autonom canvas` prints the running one's URL and
    # adds the devices it names to that Canvas.
    for document in _canvas_discovery():
        if document.get("mode") == "workspace" and document.get("workspace") == name:
            result: dict[str, Any] = {"ok": True, "already_running": True, "port": document["port"],
                                      "pid": document["pid"], "workspace": name,
                                      "url": _canvas_url(document)}
            if devices:
                result["devices"] = [_canvas_join_running(document, platform, target_id)
                                     for platform, target_id in devices]
            return emit(result, as_json=True)
    holder = _canvas_workspace_lock(name)
    if holder:
        raise errors.AutonomError(
            errors.WORKSPACE_IN_USE, f"workspace {name} is in use by Canvas pid {holder['pid']}",
            "It is still starting or stopping; retry in a moment, or stop it with "
            "`autonom canvas stop --workspace " + name + "`.",
            pid=holder["pid"], port=holder["port"], workspace=name)
    node = _canvas_node()
    command = [node, str(CANVAS_SCRIPT), "--workspace", name, "--port", str(args.port),
               "--transport", args.transport]
    for platform, target_id in devices:
        command += ["--device", f"{platform}:{target_id}"]
    if args.ephemeral:
        command.append("--ephemeral")
    for entry in bootable:
        command += ["--bootable", entry]
    if args.shutdown_booted:
        command.append("--shutdown-booted")
    if not args.bootable_only:
        # The page lists this machine's other AVDs and shut-down Simulators to boot too.
        command.append("--discover-bootable")
    command += _canvas_shared_args(args, scrcpy_server)
    # Both tools when found: the page attaches Android and iOS devices alike.
    adb_flag = getattr(args, "adb", None)
    with contextlib.suppress(errors.AutonomError):
        command += ["--adb", adb_mod.find_adb(adb_flag)]
    simctl_flag = getattr(args, "simctl", None)
    if simctl_flag or sys.platform == "darwin" or os.environ.get("AUTONOM_SIMCTL"):
        with contextlib.suppress(errors.AutonomError):
            command += ["--simctl", ios_simctl.find_simctl(simctl_flag)]
    if getattr(args, "idb", None):
        command += ["--idb", args.idb]
    if args.idb_companion is not None:
        command += ["--idb-companion", idb["path"]]
    # The automatic sessions run this CLI with this interpreter.
    command += ["--python", sys.executable, "--autonom", str(Path(__file__).resolve())]
    if args.no_auth:
        command.append("--no-auth")
    if args.token:
        command += _canvas_token_args(args.token)
    command += _canvas_action_args(args, install_roots)
    command += _canvas_control_args(args)
    # No session owner: a session stop never reaps a workspace Canvas (contract 3.4).
    return processes_mod.run_supervised(
        command, kind="canvas", owner=None, artifacts_dir=None, port=args.port, workspace=name)


def _canvas_join_running(document: dict[str, Any], platform: str, target_id: str) -> dict[str, Any]:
    """Attach one device to a running workspace Canvas; a refusal is reported, not raised."""
    import http.client

    try:
        answer = _canvas_api(document, "POST", "/api/devices",
                             {"platform": platform, "target": target_id}, timeout=150)
    except errors.AutonomError as exc:
        return {"id": f"{platform}~{target_id}", "attached": False, "error_code": exc.code,
                "error": exc.message, "hint": exc.hint}
    except (http.client.HTTPException, OSError, ValueError) as exc:
        # A Canvas that died mid-answer or sent no JSON: reported like a refusal.
        return {"id": f"{platform}~{target_id}", "attached": False, "error_code": errors.BACKEND_FAILED,
                "error": f"the Canvas answered badly: {type(exc).__name__}",
                "hint": "Check it with `autonom canvas list`, then `autonom canvas attach`."}
    device = answer.get("device") if isinstance(answer, dict) else None
    device = device if isinstance(device, dict) else {}
    return {"id": device.get("id") or f"{platform}~{target_id}", "attached": bool(answer.get("attached")),
            "tab": device.get("tab")}


def _canvas_token_args(token: str) -> list[str]:
    """The node argv items that pass the Canvas token.

    A token starting with "-" goes as one `--token=VALUE` item: as a separate item the
    node parser (and argparse) would read it as an option. Any other token keeps the
    `--token VALUE` pair of before.
    """
    return [f"--token={token}"] if token.startswith("-") else ["--token", token]


def cmd_canvas_list(args: argparse.Namespace) -> int:
    canvases = [{
        "port": item["port"], "pid": item["pid"], "url": item.get("url"),
        "mode": item.get("mode"), "workspace": item.get("workspace"),
        "tabs": [{"id": tab.get("id"), "name": tab.get("name"), "devices": tab.get("devices") or []}
                 for tab in item.get("tabs") or [] if isinstance(tab, dict)],
    } for item in _canvas_discovery()]
    return emit({"ok": True, "canvases": canvases}, as_json=True)


def _canvas_wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not session_mod.pid_alive(pid):
            return True
        time.sleep(0.1)
    return not session_mod.pid_alive(pid)


def cmd_canvas_stop(args: argparse.Namespace) -> int:
    live = _canvas_discovery()
    if args.all:
        chosen = live
    else:
        chosen = [_canvas_select(args, live)]
    stopped: list[dict[str, Any]] = []
    for document in chosen:
        pid = document["pid"]
        with contextlib.suppress(errors.AutonomError):
            _canvas_api(document, "POST", "/api/stop", {}, timeout=5)
        exited = _canvas_wait_exit(pid, CANVAS_STOP_WAIT_S)
        forced = False
        if not exited:
            command = processes_mod.command_of(pid) or ""
            if CANVAS_MARKER in command:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
                    forced = True
                exited = _canvas_wait_exit(pid, 5.0)
        item = {"port": document["port"], "pid": pid, "workspace": document.get("workspace")}
        if forced:
            item["terminated"] = True
        if not exited:
            item["still_running"] = True
        stopped.append(item)
    return emit({"ok": True, "stopped": stopped}, as_json=True)


def _canvas_target_spec(args: argparse.Namespace, document: dict[str, Any]) -> tuple[str, str]:
    """(platform, target) from the target flags; a bare --target is looked up in the Canvas."""
    platform = getattr(args, "platform", None)
    serial, udid, target = (getattr(args, "serial", None), getattr(args, "udid", None),
                            getattr(args, "target", None))
    value = serial or udid or target
    if not value:
        raise errors.AutonomError(
            errors.USAGE_ERROR, "name the device with --serial, --udid or --target",
            "For example `autonom canvas attach --serial emulator-5580`.")
    if serial:
        platform = platform or ANDROID
    elif udid:
        platform = platform or IOS
    if platform:
        return platform, value
    running = _canvas_api(document, "GET", "/api/targets").get("running") or []
    found = sorted({item.get("platform") for item in running
                    if isinstance(item, dict) and item.get("target") == value} - {None})
    if len(found) == 1:
        return found[0], value
    if len(found) > 1:
        raise errors.AutonomError(
            errors.AMBIGUOUS_TARGET, f"{value} names targets on both platforms",
            "Pass --platform android or --platform ios.")
    raise errors.AutonomError(
        errors.NO_TARGET, f"{value} is not a running target",
        "Boot it first, or pass --platform with --target; `autonom devices` lists the running ones.",
        target_id=value)


def cmd_canvas_attach(args: argparse.Namespace) -> int:
    document = _canvas_select(args, _canvas_discovery())
    platform, target = _canvas_target_spec(args, document)
    body: dict[str, Any] = {"platform": platform, "target": target}
    if args.tab:
        tabs = _canvas_api(document, "GET", "/api/workspace").get("tabs") or []
        by_id = [tab for tab in tabs if isinstance(tab, dict) and tab.get("id") == args.tab]
        by_name = [tab for tab in tabs if isinstance(tab, dict) and tab.get("name") == args.tab]
        if len(by_name) > 1 and not by_id:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"{len(by_name)} tabs are named {args.tab!r}",
                "Pass the tab id (t_…) instead; `autonom canvas list` shows them.")
        chosen = (by_id or by_name or [{"id": args.tab}])[0]
        body["tab"] = chosen["id"]
    answer = _canvas_api(document, "POST", "/api/devices", body, timeout=150)
    device = answer.get("device") or {}
    return emit({"ok": True, "port": document["port"], "device": device,
                 "attached": bool(answer.get("attached")), "tab": device.get("tab")}, as_json=True)


def cmd_canvas_detach(args: argparse.Namespace) -> int:
    document = _canvas_select(args, _canvas_discovery())
    if args.device_id:
        device_id = args.device_id
        if _explicit_target_flags(args):
            raise errors.AutonomError(
                errors.USAGE_ERROR, "--device-id cannot be combined with the target flags",
                "Name the device one way only.")
    else:
        value = (getattr(args, "serial", None) or getattr(args, "udid", None)
                 or getattr(args, "target", None))
        if not value:
            raise errors.AutonomError(
                errors.USAGE_ERROR, "name the device with --device-id or the target flags",
                "For example `autonom canvas detach --serial emulator-5580`.")
        platform = getattr(args, "platform", None) or (
            ANDROID if getattr(args, "serial", None) else IOS if getattr(args, "udid", None) else None)
        attached = _canvas_api(document, "GET", "/api/devices").get("devices") or []
        matches = [item.get("id") for item in attached if isinstance(item, dict)
                   and item.get("target") == value and (platform is None or item.get("platform") == platform)]
        if len(matches) > 1:
            raise errors.AutonomError(
                errors.AMBIGUOUS_TARGET, f"{value} names devices on both platforms",
                "Pass --platform, or --device-id.")
        device_id = matches[0] if matches else f"{platform or ANDROID}~{value}"
    answer = _canvas_api(document, "POST",
                         f"/api/devices/{urllib.parse.quote(device_id, safe='')}/detach", {},
                         timeout=120)
    return emit({"ok": True, "port": document["port"], "detached": answer.get("detached", device_id),
                 "session_stopped": bool(answer.get("session_stopped"))}, as_json=True)


# --- control lease -------------------------------------------------------------


def _control_holder(args: argparse.Namespace, verb: str) -> str:
    """`--holder`, else the invocation's claim (`--control-holder` /
    `AUTONOM_CONTROL_HOLDER`)."""
    holder = getattr(args, "holder", None)
    if holder is None:
        claim = control_mod.invocation().get("claim")
        holder = claim.holder_id if claim is not None else None
    if holder is None:
        raise errors.AutonomError(
            errors.USAGE_ERROR, f"control {verb} needs a holder id",
            f"Pass --holder ID (or set AUTONOM_CONTROL_HOLDER), e.g. "
            f"'autonom control {verb} --holder agent:worker-1'.")
    return holder


def _control_generation(args: argparse.Namespace, holder: str) -> Any:
    """`--generation`, else the claim's generation when the claim names the
    same holder."""
    generation = getattr(args, "generation", None)
    if generation is not None:
        return generation
    claim = control_mod.invocation().get("claim")
    if claim is not None and claim.holder_id == holder:
        return claim.generation
    return None


def _control_bound_session(target: Target) -> str | None:
    """The session this invocation is bound to, when it is live on the target."""
    try:
        record = session_mod.load_current()
    except Exception:  # noqa: BLE001 - a broken store binds nothing
        return None
    if (record and not record.get("stopped_at")
            and (record.get("target_id") or record.get("serial")) == target.target_id
            and (record.get("platform") or ANDROID) == target.platform):
        return record.get("session_id")
    return None


def _control_env(holder: str, generation: int) -> dict[str, str]:
    return {control_mod.ENV_HOLDER: holder, control_mod.ENV_GENERATION: str(generation)}


def cmd_control_status(args: argparse.Namespace) -> int:
    """Lock-free view of the leases: the target's (explicit target flags or
    a bound session), else every lease file of this machine."""
    if not getattr(args, "all", False):
        try:
            bound = session_mod.load_current()
        except Exception:  # noqa: BLE001 - status without a session lists every lease
            bound = None
        if _explicit_target_flags(args) or bound:
            target = _target(args)
            leases = control_mod.status(target.platform, target.target_id)
            return emit({"ok": True, "leases": leases, "count": len(leases),
                         "lease": leases[0] if leases else None, **target.identity()},
                        as_json=True)
    leases = control_mod.status()
    return emit({"ok": True, "leases": leases, "count": len(leases)}, as_json=True)


def cmd_control_acquire(args: argparse.Namespace) -> int:
    target = _target(args)
    holder = _control_holder(args, "acquire")
    answer = control_mod.acquire(
        target.platform, target.target_id, holder_id=holder, kind=args.kind,
        label=args.label, ttl_s=args.ttl, takeover=bool(args.takeover),
        session_id=_control_bound_session(target))
    return emit({**answer, **target.identity(),
                 "env": _control_env(holder, answer["generation"])}, as_json=True)


def cmd_control_release(args: argparse.Namespace) -> int:
    target = _target(args)
    holder = _control_holder(args, "release")
    answer = control_mod.release(target.platform, target.target_id, holder_id=holder,
                                 generation=_control_generation(args, holder))
    return emit({**answer, **target.identity()}, as_json=True)


def cmd_control_renew(args: argparse.Namespace) -> int:
    target = _target(args)
    holder = _control_holder(args, "renew")
    answer = control_mod.renew(target.platform, target.target_id, holder_id=holder,
                               generation=_control_generation(args, holder), ttl_s=args.ttl)
    return emit({**answer, **target.identity(),
                 "env": _control_env(holder, answer["generation"])}, as_json=True)


# --- verb classification (contract 5.3) -------------------------------------
# Every verb path printed by walking build_parser() (aliases included; bare
# group forms such as `devices` and `canvas` too) is in exactly one table, so
# a new verb cannot ship unclassified (tests/test_verb_classification.py).
# READONLY, LIFECYCLE and HOST verbs are never admitted; MUTATING verbs are
# admitted once per target before their first device call; FLOW verbs at
# start and before every mutating flow step.

_ATLAS_VERBS = ("update", "show", "coverage", "paths", "export", "diff")

_CONTROL_READONLY: frozenset[str] = frozenset({
    "version", "devices", "devices list", "session outputs", "session show", "session list",
    "ui tree", "ui find", "ui wait", "screenshot", "note add", "note list", "journal",
    "shots list", "shots show", "doctor", "capabilities", "processes", "location get",
    "crash list", "crash show", "file ls", "file pull", "record start", "record stop",
    "flow check", "flow fmt", "flow list", "flow create", "flow import", "flow export",
    "teach start", "teach mark", "teach stop", "teach show", "teach compile",
    "app-skill validate", "app-skill promote",
    *(f"atlas {verb}" for verb in _ATLAS_VERBS),
    *(f"runtime-map {verb}" for verb in _ATLAS_VERBS),
    "report build", "report open", "report export", "report suite", "report model",
    "report bundle", "report verify", "report annotate", "report gate", "report history",
    "report watch", "report pack", "report merge", "report finalize",
    "ci pack", "ci merge", "ci finalize", "ci publish", "agent export", "agent inspect",
    "network status", "network requests list", "network requests follow",
    "network requests show", "network export",
    "metrics snapshot", "metrics series", "metrics memory capture", "metrics memory analyze",
    "metrics frames reset", "metrics frames capture", "metrics frames flutter-summary",
    "metrics trace", "metrics list-presets", "logs tail", "logs follow", "control status",
    # Ticket pre-test: these record pre-test state on the host (checks, findings,
    # reviews, packs) or only read the device (health baselines, crash lists,
    # logs, `a11y`'s one tree read); none sends input.
    "pretest status", "pretest context", "pretest list", "pretest check add",
    "pretest check begin", "pretest check end", "pretest check amend", "pretest check list",
    "pretest finding add", "pretest finding list", "pretest health", "pretest pack",
    "pretest review", "pretest review-status", "pretest kit", "pretest flow-draft",
    "pretest a11y", "pretest events",
})
# `session start` is MUTATING for the new session's target when --install or
# --launch is given (checked before the install).
_CONTROL_LIFECYCLE: frozenset[str] = frozenset({
    "session start", "session stop", "devices boot", "canvas", "canvas serve", "canvas stop",
    "canvas attach", "canvas detach", "canvas list", "cleanup", "control acquire",
    "control release", "control renew", "pretest start", "pretest stop",
})
_CONTROL_HOST: frozenset[str] = frozenset({
    "network start", "network mock add", "network mock list", "network mock show",
    "network mock update", "network mock enable", "network mock disable",
    "network mock remove", "network mock clear",
})
_CONTROL_MUTATING: frozenset[str] = frozenset({
    "devices shutdown", "session launch", "session force-stop", "session clear",
    "session uninstall", "ui tap", "ui swipe", "ui pinch", "ui rotate", "ui shake", "ui type",
    "ui key", "open", "permissions", "location set", "location clear", "media add",
    "network attach", "network detach", "network stop", "metrics memory warn",
})
# `teach approve` and `tour` run flows only with --run (else READONLY);
# `report serve` admits each /replay through the flow executor. The pre-test
# verbs that run flows (`flows`, `repro`, `rerun`, `warmup`) go through the
# same executor, so every mutating step is admitted like `flow run`; the
# warm-up's Runtime Map replay admits each of its taps itself. `ui do` runs
# its stdin steps through the same executor.
_CONTROL_FLOW: frozenset[str] = frozenset({
    "flow run", "proof", "replay", "ci run", "teach approve", "report serve", "tour",
    "pretest flows", "pretest repro", "pretest rerun", "pretest warmup", "ui do",
})
# Verbs whose class depends on their positional action: the READONLY actions
# are listed, every other action is MUTATING.
_CONTROL_BY_ACTION: dict[str, frozenset[str]] = {
    "ui accessibility": frozenset({"status"}),
    "simulator battery": frozenset(),
    "simulator network": frozenset(),
    "simulator push": frozenset(),
    "simulator sms": frozenset(),
    "simulator call": frozenset(),
    "simulator biometric": frozenset(),
    "simulator clipboard": frozenset({"get"}),
    "simulator appearance": frozenset(),
    "simulator text-size": frozenset(),
    "simulator status-bar": frozenset(),
    "simulator keyboard": frozenset({"show"}),
    "simulator animations": frozenset({"show"}),
}
_CONTROL_RUN_FLAG_VERBS = frozenset({"teach approve", "tour"})
_CONTROL_ERROR_CODES = frozenset({
    errors.CONTROL_HELD, errors.CONTROL_GENERATION_STALE, errors.CONTROL_LEASE_EXPIRED,
    errors.CONTROL_NOT_HELD,
})


def _verb_path(args: argparse.Namespace) -> str:
    """The parsed verb path as the parser walk prints it (`network mock add`)."""
    node = _ROOT_PARSER or build_parser()
    parts: list[str] = []
    while True:
        action = next((item for item in node._actions  # noqa: SLF001
                       if isinstance(item, argparse._SubParsersAction)), None)  # noqa: SLF001
        if action is None:
            break
        name = getattr(args, action.dest, None)
        chosen = action.choices.get(name) if name else None
        if chosen is None:
            break
        parts.append(name)
        node = chosen
    return " ".join(parts)


def _control_mode(args: argparse.Namespace) -> str:
    """The control class of the parsed verb (contract 5.3), as an invocation mode."""
    verb = _verb_path(args)
    if verb in _CONTROL_BY_ACTION:
        return ("readonly" if getattr(args, "action", None) in _CONTROL_BY_ACTION[verb]
                else "mutating")
    if verb == "session start":
        driving = getattr(args, "install", None) or getattr(args, "launch", None) is not None
        return "mutating" if driving else "lifecycle"
    if verb in _CONTROL_RUN_FLAG_VERBS:
        return "flow" if getattr(args, "run", False) else "readonly"
    if verb == "flow run" and getattr(args, "dry_run", False):
        return "readonly"  # a dry run plans the steps and touches no device
    if verb in _CONTROL_READONLY:
        return "readonly"
    if verb in _CONTROL_LIFECYCLE:
        return "lifecycle"
    if verb in _CONTROL_HOST:
        return "host"
    if verb in _CONTROL_FLOW:
        return "flow"
    # MUTATING, and anything unclassified: admitted (never silently free).
    return "mutating"


def _begin_control(args: argparse.Namespace) -> None:
    """The invocation's control context: claim (flags over env), operation id
    and the verb's class."""
    claim = control_mod.claim_from(getattr(args, "control_holder", None),
                                   getattr(args, "control_generation", None))
    control_mod.begin_invocation(claim=claim, operation_id=getattr(args, "operation_id", None),
                                 mode=_control_mode(args))


# --- parser ------------------------------------------------------------------


def target_flags_parent() -> argparse.ArgumentParser:
    """Target flags repeated on every leaf subcommand.

    `autonom --platform ios ui tree` and `autonom ui tree --platform ios` must
    both work — the plan's own examples use each form. `SUPPRESS` defaults are
    what make that safe: without them the subparser would overwrite a value the
    top-level parser already set with its own `None`.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--session-id", default=argparse.SUPPRESS,
                        help="bind this command to a session (a stopped one: read-only verbs)")
    parent.add_argument("--ui-backend", choices=("auto", "idb", "xcuitest"), default=argparse.SUPPRESS,
                        help="iOS tree and input backend (default auto: idb, XCUITest on an unusable tree)")
    parent.add_argument("--platform", choices=("android", "ios"), default=argparse.SUPPRESS)
    parent.add_argument("--target", default=argparse.SUPPRESS)
    parent.add_argument("--serial", default=argparse.SUPPRESS)
    parent.add_argument("--udid", default=argparse.SUPPRESS)
    parent.add_argument("--adb", default=argparse.SUPPRESS)
    parent.add_argument("--simctl", default=argparse.SUPPRESS)
    parent.add_argument("--idb", default=argparse.SUPPRESS)
    parent.add_argument("--idb-host", default=argparse.SUPPRESS)
    parent.add_argument("--idb-port", type=int, default=argparse.SUPPRESS)
    return parent


_TRUE_WORDS = frozenset({"1", "true", "yes"})
_FALSE_WORDS = frozenset({"0", "false", "no"})


def _bool_flag(value: str) -> bool:
    """true/false/1/0/yes/no only: `--enabled flase` used to read as False."""
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise argparse.ArgumentTypeError(
        f"expected true/false (or 1/0, yes/no), got {value!r}")


def _add_selector_flags(parser: argparse.ArgumentParser) -> None:
    """The selector set, identical on every verb that selects a node.

    `_selectors()` reads `enabled` for find *and* tap, so it belongs here rather
    than on find alone — while it lived on find only, `ui tap --enabled false`
    was rejected by argparse and the filter silently read `None` on tap.
    """
    parser.add_argument("--text")
    parser.add_argument("--desc")
    parser.add_argument("--resource-id")
    parser.add_argument("--class-name")
    parser.add_argument("--package")
    parser.add_argument("--role")
    parser.add_argument("--clickable", type=_bool_flag, default=None)
    parser.add_argument("--enabled", type=_bool_flag, default=None)
    parser.add_argument("--mode", choices=("exact", "contains", "regex"), default="contains")
    parser.add_argument("--case-sensitive", action="store_true")
    parser.add_argument("--index", type=int)


# The observe flags of the action verbs (AL-11), matched only when spelled
# in full like every flag added after abbreviations were in use.
_OBSERVE_FLAGS = frozenset({"--observe", "--no-observe", "--observe-timeout-ms"})


def _add_observe_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--observe", dest="observe", action="store_const", const=True,
                        default=None,
                        help="after the input, wait for the screen to settle and return it "
                             "(screen, changes, foreground); the default in an "
                             "agent-profile session")
    parser.add_argument("--no-observe", dest="observe", action="store_const", const=False,
                        help="return right after the input, whatever the session profile")
    parser.add_argument("--observe-timeout-ms", type=int, default=observe_mod.DEFAULT_TIMEOUT_MS,
                        metavar="MS",
                        help="how long observe waits for the screen to settle "
                             f"(default {observe_mod.DEFAULT_TIMEOUT_MS})")
    parser.exact_only_options = journal_mod.EXACT_ONLY_OPTIONS | _OBSERVE_FLAGS


# `session outputs`, `journal` and `logs follow` had their own --session-id
# before it became a target flag; its abbreviations keep working there.
_ABBREVIABLE_SESSION_ID = journal_mod.EXACT_ONLY_OPTIONS - {"--session-id"}


class _JsonArgumentParser(argparse.ArgumentParser):
    """argparse that fails the way every other Autonom failure does.

    An unknown verb, a misspelt flag, or `--arg --es` (a value that looks like
    an option) used to print argparse prose on stderr — the one place a host
    agent branching on `error_code` had nothing to read. Subparsers inherit
    this class, so the envelope covers every level.
    """

    # Matched only when spelled in full: these flags came after abbreviations
    # of the older ones were in use (`--u` is `--udid`, `--se` is `--serial`).
    # journal._resolve_option mirrors this when it resolves argv.
    exact_only_options = journal_mod.EXACT_ONLY_OPTIONS

    def _get_option_tuples(self, option_string: str):  # type: ignore[override]
        tuples = super()._get_option_tuples(option_string)
        return [item for item in tuples
                if not set(item[0].option_strings) & self.exact_only_options]

    def error(self, message: str) -> None:  # type: ignore[override]
        payload = {
            "ok": False,
            "error_code": errors.USAGE_ERROR,
            "error": message or "invalid arguments",
            "hint": self.format_usage().strip(),  # already begins with "usage:"
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


# The control claim of an invocation (contract 5.1): option -> namespace dest.
# Spelled in full only (journal.EXACT_ONLY_OPTIONS) and accepted before or
# after the verb.
_CONTROL_CLAIM_FLAGS = {
    "--control-holder": "control_holder",
    "--control-generation": "control_generation",
    "--operation-id": "operation_id",
}
_NEGATIVE_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")


def _split_control_flags(argv: list[str]) -> tuple[dict[str, Any], list[str], str | None]:
    """(claim values by dest, the remaining argv, the flag that lacks a value).

    The claim flags are read wherever they stand before a `--` terminator,
    in their exact spellings only (`--flag value` or `--flag=value`; the last
    one wins). They are not argparse options of any parser: the leaves keep
    exactly the option lists (and abbreviations) they had, and the journal
    and the flow compiler read the remaining argv as before."""
    values: dict[str, Any] = {dest: None for dest in _CONTROL_CLAIM_FLAGS.values()}
    rest: list[str] = []
    missing: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            rest.extend(argv[index:])
            break
        name, equals, inline = token.partition("=")
        if name in _CONTROL_CLAIM_FLAGS and (equals or token == name):
            if equals:
                values[_CONTROL_CLAIM_FLAGS[name]] = inline
                index += 1
                continue
            following = argv[index + 1] if index + 1 < len(argv) else None
            if following is None or following == "--" or (
                    following.startswith("-") and not _NEGATIVE_NUMBER.match(following)):
                missing = missing or name
                index += 1
                continue
            values[_CONTROL_CLAIM_FLAGS[name]] = following
            index += 2
            continue
        rest.append(token)
        index += 1
    return values, rest, missing


def _strip_control_flags(argv: list[str]) -> list[str]:
    """argv without the claim flags (the journal records the operation id)."""
    return _split_control_flags(argv)[1]


class _RootArgumentParser(_JsonArgumentParser):
    """The top-level parser: reads the control claim flags wherever they stand."""

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[override]
        argv = sys.argv[1:] if args is None else list(args)
        values, rest, missing = _split_control_flags(argv)
        if missing is not None:
            self.error(f"argument {missing}: expected one argument")
        parsed, extras = super().parse_known_args(rest, namespace)
        for dest, value in values.items():
            setattr(parsed, dest, value)
        return parsed, extras


def build_parser() -> argparse.ArgumentParser:
    parser = _RootArgumentParser(
        prog="autonom",
        description="Universal mobile test/debug control plane for AI agents (Android + iOS).",
        epilog="control claim, before or after the verb (exact spellings): "
               "--control-holder ID, --control-generation N, --operation-id ID "
               "(env AUTONOM_CONTROL_HOLDER, AUTONOM_CONTROL_GENERATION)",
    )
    parser.add_argument("--session-id",
                        help="bind this command to a session (a stopped one: read-only verbs)")
    parser.add_argument("--ui-backend", choices=("auto", "idb", "xcuitest"),
                        help="iOS tree and input backend (default auto: idb, XCUITest on an unusable tree)")
    parser.add_argument("--platform", choices=("android", "ios"), help="target platform")
    parser.add_argument("--target", help="target id (adb serial or simulator udid)")
    parser.add_argument("--serial", help="adb serial / emulator id (Android alias of --target)")
    parser.add_argument("--udid", help="simulator udid (iOS alias of --target)")
    parser.add_argument("--adb", help="path to adb binary")
    parser.add_argument("--simctl", help="path to xcrun binary")
    parser.add_argument("--idb", help="path to idb binary")
    parser.add_argument("--idb-host", help="remote idb companion host")
    parser.add_argument("--idb-port", type=int, help="remote idb companion port")
    # Global only (before the verb): published as AUTONOM_AXE / AUTONOM_IOS_HID
    # for the process, exactly like the environment variables they mirror.
    parser.add_argument("--axe", metavar="PATH",
                        help="path to the AXe binary (iOS HID fallback; AUTONOM_AXE)")
    parser.add_argument("--ios-hid", choices=("auto", "idb", "axe"),
                        help="iOS input backend: auto (idb, AXe when idb HID is "
                             "broken), idb, or axe (AUTONOM_IOS_HID)")
    # The control claim flags (--control-holder ID, --control-generation N,
    # --operation-id ID) are read by _RootArgumentParser before or after the
    # verb, not declared here: every option list stays the one of before.
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=_JsonArgumentParser)
    target_flags = target_flags_parent()

    p = sub.add_parser("version", help="print CLI version")
    p.set_defaults(func=cmd_version)

    devices = sub.add_parser(
        "devices", help="list, boot, and shut down devices and simulators", parents=[target_flags]
    )
    devices.set_defaults(func=cmd_devices)
    devices_sub = devices.add_subparsers(dest="devices_command", required=False)

    p = devices_sub.add_parser("list", help="unified inventory (the default)",
                               parents=[target_flags])
    p.set_defaults(func=cmd_devices)

    p = devices_sub.add_parser(
        "boot", help="start an Android AVD or boot an iOS simulator", parents=[target_flags]
    )
    p.add_argument("--avd", help="Android AVD name to start ('autonom devices' lists them)")
    p.add_argument("--emulator", help="path to the Android emulator binary")
    p.add_argument("--http-proxy", metavar="HOST:PORT",
                   help="route ALL of the emulator's traffic through this proxy at launch "
                        "(a launch-time flag; needed for transparent 'network attach "
                        "--system-ca' capture). Android emulator only")
    p.add_argument("--timeout", type=float, default=180.0,
                   help="seconds to wait for the boot to complete")
    p.add_argument("--no-wait", action="store_true",
                   help="return right after spawning instead of waiting for boot (Android)")
    p.add_argument("--port", type=int,
                   help="console port of the new emulator (even, 5554-5682; serial "
                        "emulator-<port>). Android, with --avd only")
    p.set_defaults(func=cmd_devices_boot)

    p = devices_sub.add_parser(
        "shutdown", help="shut down an emulator or simulator (never physical hardware)",
        parents=[target_flags],
    )
    p.set_defaults(func=cmd_devices_shutdown)

    session = sub.add_parser("session", help="session lifecycle")
    session_sub = session.add_subparsers(dest="session_command", required=True)

    p = session_sub.add_parser("start", help="start an Autonom session", parents=[target_flags])
    p.add_argument("--app-id", help="application / bundle id")
    p.add_argument("--install", help="path to apk/aab (Android) or .app bundle (iOS)")
    p.add_argument("--launch", nargs="?", const="", help="launch app id (defaults to --app-id)")
    p.add_argument("--activity", help="optional activity component (Android)")
    p.add_argument("--log-stream", action="store_true", help="start a background log stream (iOS)")
    p.add_argument("--alongside", action="store_true",
                   help="start beside the current session without becoming current "
                        "(current.json is left as it is)")
    p.add_argument("--started-by", metavar="canvas:PORT:PID",
                   help="record who started the session (with --alongside only)")
    p.add_argument("--label", metavar="TEXT",
                   help="a name for the session, shown by 'session list' (1-64 printable characters)")
    p.add_argument("--profile", choices=session_mod.PROFILES,
                   help="default (the default): replies as always; agent: 'ui tree' prints "
                        "the outline and tap/type/swipe/key return the new screen")
    # `--la` keeps reaching --launch and `--p` --platform: --label and
    # --profile are matched only when spelled in full.
    p.exact_only_options = journal_mod.EXACT_ONLY_OPTIONS | {"--label", "--profile"}
    p.set_defaults(func=cmd_session_start)

    p = session_sub.add_parser("list", help="list the live sessions of this machine (all with --all)",
                               parents=[target_flags])
    p.add_argument("--all", action="store_true",
                   help="include stopped sessions")
    p.set_defaults(func=cmd_session_list)

    p = session_sub.add_parser("stop", help="stop current session metadata", parents=[target_flags])
    p.set_defaults(func=cmd_session_stop)

    p = session_sub.add_parser("outputs", help="catalog followable session streams",
                               parents=[target_flags])
    p.exact_only_options = _ABBREVIABLE_SESSION_ID  # its --session-id predates the rule
    p.set_defaults(func=cmd_session_outputs)
    p = session_sub.add_parser("show", help="show current session", parents=[target_flags])
    p.set_defaults(func=cmd_session_show)

    p = session_sub.add_parser("launch", help="launch an app", parents=[target_flags])
    p.add_argument("app_id")
    p.add_argument("--activity")
    p.add_argument("--arg", action="append", help="launch argument (repeatable, iOS)")
    p.add_argument("--setenv", action="append", help="KEY=VALUE child environment (iOS)")
    p.add_argument("--fresh", action="store_true",
                   help="start on a cleared task (Android) / after terminate (iOS) "
                        "instead of resuming where the app was")
    p.set_defaults(func=cmd_session_launch)

    p = session_sub.add_parser("force-stop", help="force-stop an app", parents=[target_flags])
    p.add_argument("app_id")
    p.set_defaults(func=cmd_session_stop_app)

    p = session_sub.add_parser("clear", help="clear app data", parents=[target_flags])
    p.add_argument("app_id")
    p.add_argument("--strategy", choices=("auto", "reinstall", "privacy"), default="auto")
    p.set_defaults(func=cmd_session_clear)

    p = session_sub.add_parser("uninstall", help="uninstall an app", parents=[target_flags])
    p.add_argument("app_id")
    p.set_defaults(func=cmd_session_uninstall)

    ui = sub.add_parser("ui", help="UI tree and interaction")
    ui_sub = ui.add_subparsers(dest="ui_command", required=True)

    p = ui_sub.add_parser("tree", help="compact UI tree", parents=[target_flags])
    p.add_argument("--dump", help="parse an offline UI dump instead of a live target")
    p.add_argument("--all", action="store_true", help="include non-meaningful nodes")
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--max-nodes", type=int, default=200)
    p.add_argument("--format", choices=("json", "outline"), default=None,
                   help="outline: one indented line per node instead of the nodes array "
                        "(default json; outline in an agent-profile session)")
    p.add_argument("--interactable", action="store_true",
                   help="only nodes an agent can act on (buttons, fields, switches, ...)")
    p.add_argument("--no-accessibility-recovery", action="store_true",
                   help="inspect a sparse Android tree without enabling Accessibility Menu")
    p.set_defaults(func=cmd_ui_tree)

    p = ui_sub.add_parser("accessibility", help="inspect or manage Android emulator "
                          "accessibility recovery", parents=[target_flags])
    p.add_argument("action", choices=("status", "enable", "reset"))
    p.set_defaults(func=cmd_ui_accessibility)

    p = ui_sub.add_parser("find", help="find nodes by selector", parents=[target_flags])
    p.add_argument("--dump", help="parse an offline UI dump")
    _add_selector_flags(p)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_ui_find)

    p = ui_sub.add_parser("wait", help="wait until the screen stops changing",
                          parents=[target_flags])
    p.add_argument("--settled", action="store_true",
                   help="wait until the screen holds still for --quiet-ms (iOS idb: "
                        "'idb ui quiet', then one read; otherwise poll until two "
                        "consecutive trees match)")
    p.add_argument("--timeout-ms", type=int, default=5000,
                   help="give up after N ms (exit 1, settled: false)")
    p.add_argument("--quiet-ms", type=int, default=500,
                   help="how long the tree must hold still (default 500)")
    p.set_defaults(func=cmd_ui_wait)

    p = ui_sub.add_parser("tap", help="tap by selector or coordinates", parents=[target_flags])
    _add_selector_flags(p)
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    p.add_argument("--duration", type=int, metavar="MS",
                   help="hold for MS milliseconds (a long press)")
    _add_observe_flags(p)
    p.set_defaults(func=cmd_ui_tap)

    p = ui_sub.add_parser("swipe", help="swipe between two points", parents=[target_flags])
    p.add_argument("--from", dest="start", required=True, metavar="X,Y")
    p.add_argument("--to", dest="end", required=True, metavar="X,Y")
    p.add_argument("--duration", type=float, default=0.3, metavar="SECONDS",
                   help=f"seconds, e.g. 0.4 (default 0.3, at most {SWIPE_MAX_SECONDS:g})")
    _add_observe_flags(p)
    p.set_defaults(func=cmd_ui_swipe)

    # Only pinch takes an anchor and a scale; `idb ui rotate|shake` accept
    # neither, and offering the flags there advertised an argument that was
    # parsed, ignored, and never reached the device.
    for name in ("pinch", "rotate", "shake"):
        p = ui_sub.add_parser(name, help=f"{name} gesture (no backend: refused on Android and iOS)", parents=[target_flags])
        if name == "pinch":
            p.add_argument("--at", metavar="X,Y", help="anchor point")
            p.add_argument("--scale", type=float, default=2.0)
        p.set_defaults(func=cmd_ui_gesture, gesture=name)

    p = ui_sub.add_parser("type", help="type text into the focused field", parents=[target_flags])
    p.add_argument("text")
    p.add_argument("--sensitive", action="store_true",
                   help="a credential: keep only its length in every artifact")
    _add_observe_flags(p)
    p.set_defaults(func=cmd_ui_type)

    p = ui_sub.add_parser("key", help="send a key or hardware button", parents=[target_flags])
    p.add_argument("keycode", help="Android: KEYCODE_BACK; iOS: HOME, LOCK, SIRI, SIDE_BUTTON")
    _add_observe_flags(p)
    p.set_defaults(func=cmd_ui_key)

    p = ui_sub.add_parser(
        "do", help="run Flow v1 steps from stdin (a JSON array or a YAML step list) in "
                   "one call; stops at the first failing step and returns the final screen",
        parents=[target_flags])
    p.add_argument("--observe-timeout-ms", type=int, default=observe_mod.DEFAULT_TIMEOUT_MS,
                   metavar="MS",
                   help="how long the final screen may take to settle "
                        f"(default {observe_mod.DEFAULT_TIMEOUT_MS})")
    p.exact_only_options = journal_mod.EXACT_ONLY_OPTIONS | {"--observe-timeout-ms"}
    p.set_defaults(func=cmd_ui_do)

    p = sub.add_parser("screenshot", help="capture PNG screenshot", parents=[target_flags])
    p.add_argument("--out", help="extra copy at this exact path (the session keeps one too)")
    p.add_argument("--label", help="what this shows, e.g. 'preorders with mock'")
    p.add_argument("--task", help="group shots under a sub-directory")
    p.set_defaults(func=cmd_screenshot)

    note = sub.add_parser("note", help="record an agent note in the session journal")
    note_sub = note.add_subparsers(dest="note_command", required=True)
    p = note_sub.add_parser("add", help="append a note", parents=[target_flags])
    p.add_argument("text", help="what you observed or concluded")
    p.add_argument("--task", help="group under a task label")
    p.add_argument("--tag", action="append", help="a searchable tag (repeatable)")
    p.add_argument("--author", default="agent", help="who wrote it (default: agent)")
    p.set_defaults(func=cmd_note_add)
    p = note_sub.add_parser("list", help="read notes back", parents=[target_flags])
    p.add_argument("--task")
    p.add_argument("--grep", help="regex over the note")
    p.add_argument("--max", type=int, default=50)
    p.set_defaults(func=cmd_note_list)

    p = sub.add_parser("journal", help="read the session timeline of actions and notes",
                       parents=[target_flags])
    p.exact_only_options = _ABBREVIABLE_SESSION_ID  # its --session-id predates the rule
    p.add_argument("--kind", choices=("action", "note"), help="only actions or only notes")
    p.add_argument("--verb", help="only this verb, e.g. 'ui tap'")
    p.add_argument("--task", help="only entries under this task label")
    p.add_argument("--grep", help="regex over every field")
    p.add_argument("--max", type=int, default=100)
    p.add_argument("--follow", action="store_true",
                   help="stream new journal lines as NDJSON instead of listing")
    p.add_argument("--from-start", action="store_true",
                   help="with --follow: replay the whole journal first")
    p.add_argument("--max-seconds", type=float, default=0,
                   help="with --follow: stop after N seconds")
    p.add_argument("--max-lines", type=int, default=0,
                   help="with --follow: stop after N emitted lines")
    p.set_defaults(func=cmd_journal)

    shots = sub.add_parser("shots", help="browse captured screenshots")
    shots_sub = shots.add_subparsers(dest="shots_command", required=True)
    p = shots_sub.add_parser("list", parents=[target_flags])
    p.add_argument("--task")
    p.add_argument("--grep", help="regex over every metadata field")
    p.add_argument("--mocked-only", action="store_true",
                   help="only shots taken while a mock was active")
    p.add_argument("--max", type=int, default=50)
    p.set_defaults(func=cmd_shots_list)
    p = shots_sub.add_parser("show", parents=[target_flags])
    p.add_argument("path")
    p.set_defaults(func=cmd_shots_show)

    p = sub.add_parser(
        "tour", help="what Autonom has, how to use it, and a guided walk on your own device",
        parents=[target_flags])
    p.add_argument("--run", action="store_true",
                   help="perform the walk (without it: overview + offer; a TTY is asked)")
    p.add_argument("--avd", help="the AVD to boot when no emulator is running")
    p.add_argument("--flow", help="walk this Flow v1 file instead of the built-in Settings tour")
    p.add_argument("--human", action="store_true",
                   help="print the Markdown account to stdout instead of JSON")
    p.add_argument("--shutdown", action="store_true",
                   help="power off a device the tour booted, when the walk is over")
    p.set_defaults(func=cmd_tour)

    p = sub.add_parser("doctor", help="report toolchain, capabilities, session, and orphans",
                       parents=[target_flags])
    p.add_argument("--mitmdump", help="path to mitmdump binary")
    p.add_argument("--strict", action="store_true", help="exit 1 when any tool is missing")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("capabilities", help="immutable semantic capability snapshot",
                       parents=[target_flags])
    p.add_argument("--probe", action="store_true", help="read the current UI to measure backend readiness")
    # `--p` keeps meaning --platform here
    p.exact_only_options = journal_mod.EXACT_ONLY_OPTIONS | {"--probe"}
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("processes", help="list Autonom-spawned processes machine-wide")
    p.set_defaults(func=cmd_processes)

    p = sub.add_parser("cleanup", help="terminate leftover Autonom processes")
    p.add_argument("--dry-run", action="store_true", help="report without terminating")
    p.add_argument("--all", action="store_true",
                   help="also stop healthy processes, not just orphans")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("open", help="open a deep link or URL", parents=[target_flags])
    p.add_argument("url")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("permissions", help="grant/revoke/reset app permissions",
                       parents=[target_flags])
    p.add_argument("action", choices=("grant", "revoke", "reset"))
    p.add_argument("service", help="iOS privacy service, or an Android permission name")
    p.add_argument("app_id", nargs="?")
    p.set_defaults(func=cmd_permissions)

    location = sub.add_parser("location", help="simulated location (iOS + Android emulator)")
    location_sub = location.add_subparsers(dest="location_command", required=True)
    p = location_sub.add_parser("set", parents=[target_flags])
    p.add_argument("coordinates", metavar="LAT,LON")
    p.set_defaults(func=cmd_location)
    p = location_sub.add_parser("get", help="read the current location (Android)",
                                parents=[target_flags])
    p.set_defaults(func=cmd_location)
    p = location_sub.add_parser("clear", parents=[target_flags])
    p.set_defaults(func=cmd_location)

    simulator = sub.add_parser(
        "simulator", help="battery, network, push, telephony, biometric, UI state, "
                          "status-bar pin, keyboard/locale pin, and animations")
    simulator_sub = simulator.add_subparsers(dest="simulator_command", required=True)
    for control in ("battery", "network", "push", "sms", "call", "biometric",
                    "clipboard", "appearance", "text-size", "status-bar", "keyboard",
                    "animations"):
        valid = simulator_mod.CONTROL_ACTIONS.get(control)
        actions = ("one of: " + ", ".join(valid) if valid
                   else "the iOS content size, e.g. large or accessibility-extra-large")
        p = simulator_sub.add_parser(control, parents=[target_flags],
                                     help=f"{control} ({actions})")
        p.add_argument("action", help=actions)
        p.add_argument("--value", action="append", metavar="KEY=VALUE")
        p.add_argument("--json", help="JSON object of control values")
        p.set_defaults(func=cmd_simulator, control=control)

    canvas = sub.add_parser("canvas", help="shared human/agent mobile control surface "
                                           "(alone: a workspace Canvas, as `canvas serve`)")
    # `autonom canvas` alone serves the workspace with the defaults (contract 6.1).
    canvas.set_defaults(func=cmd_canvas)
    canvas_sub = canvas.add_subparsers(dest="canvas_command", required=False)
    p = canvas_sub.add_parser("serve", parents=[target_flags])
    p.add_argument("--port", type=int, default=3277, help="0 picks a free port")
    p.add_argument("--transport", choices=CANVAS_TRANSPORTS, default="auto",
                   help="auto prefers scrcpy on Android when a 4.1 server is found, "
                        "and idb on the iOS Simulator when idb_companion is found")
    p.add_argument("--fps", type=int,
                   help="frame rate cap (default 15 multipart; 30 software scrcpy; 60 other scrcpy and idb)")
    p.add_argument("--max-size", type=int, metavar="PX",
                   help="longest video side in pixels on scrcpy (video width on screenrecord), "
                        "0 for native size; default on scrcpy: 1280 with a software-only "
                        "H.264 encoder, native with a hardware one, 1280 when the encoders "
                        "cannot be read; 1280 otherwise")
    p.add_argument("--bit-rate", type=int, metavar="BPS",
                   help="H.264 bit rate in bits per second (default 12000000)")
    p.add_argument("--scrcpy-server", metavar="PATH",
                   help="scrcpy-server file to push (default: discovered, see doctor)")
    p.add_argument("--scrcpy-version", metavar="X.Y",
                   help="version of --scrcpy-server when its file name does not say")
    p.add_argument("--idb-companion", metavar="PATH",
                   help="idb_companion of the iOS idb transport "
                        "(default: AUTONOM_IDB_COMPANION_BIN, then PATH)")
    p.add_argument("--token")
    p.add_argument("--no-auth", action="store_true")
    # Workspace and multi-device flags. None may start with --to: the abbreviations
    # --to and --tok must keep reaching --token (journal redaction relies on it).
    p.add_argument("--device", action="append", metavar="PLATFORM:ID",
                   help="a device to show, android:<serial> or ios:<udid> (repeatable, at most 8)")
    p.add_argument("--single", action="store_true",
                   help="the one-device page (no tabs, attach or boot) for one named target")
    p.add_argument("--split", action="store_true",
                   help="kept for older scripts: every Canvas is a workspace now")
    p.add_argument("--workspace", metavar="NAME",
                   help="workspace name (default: default); its tabs persist across restarts")
    p.add_argument("--ephemeral", action="store_true",
                   help="a workspace that neither reads nor saves its tabs")
    p.add_argument("--bootable", action="append", metavar="SPEC",
                   help="avd:NAME[@PORT] or simulator:UDID the page may boot (repeatable, at most 8)")
    p.add_argument("--bootable-only", action="store_true",
                   help="the page boots only the --bootable devices, not every AVD and Simulator "
                        "of this machine")
    p.add_argument("--shutdown-booted", action="store_true",
                   help="shut down the devices this Canvas booted when it stops")
    p.add_argument("--install-root", action="append", metavar="DIR",
                   help="a folder the page may install builds from (repeatable; default none)")
    p.add_argument("--captures-dir", metavar="DIR",
                   help="screenshots and recordings (default: AUTONOM_CAPTURES_DIR, "
                        "else ~/Downloads/Autonom)")
    p.add_argument("--control-idle", metavar="SECONDS",
                   help="release the human control lease after this many seconds without "
                        f"human input ({CANVAS_CONTROL_IDLE[0]}-{CANVAS_CONTROL_IDLE[1]}; "
                        "default 120)")
    # Exact-only: `--c`/`--ca` keep reaching --captures-dir.
    p.exact_only_options = journal_mod.EXACT_ONLY_OPTIONS | {"--control-idle"}
    p.set_defaults(func=cmd_canvas_serve)

    def canvas_selector(parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--port", dest="canvas_port", type=int, metavar="N",
                           help="the Canvas on this port")
        group.add_argument("--workspace", dest="canvas_workspace", metavar="NAME",
                           help="the Canvas of this workspace")
        return group

    p = canvas_sub.add_parser("stop", help="stop a running Canvas")
    canvas_selector(p).add_argument("--all", action="store_true", help="every running Canvas")
    p.set_defaults(func=cmd_canvas_stop)
    p = canvas_sub.add_parser("attach", parents=[target_flags],
                              help="attach a device to a running workspace Canvas")
    canvas_selector(p)
    p.add_argument("--tab", metavar="ID|NAME", help="the tab to place it in (default: the active tab)")
    p.set_defaults(func=cmd_canvas_attach)
    p = canvas_sub.add_parser("detach", parents=[target_flags],
                              help="detach a device; a session the Canvas started ends with it")
    canvas_selector(p)
    p.add_argument("--device-id", metavar="ID", help="the Canvas device id, e.g. android~emulator-5580")
    p.set_defaults(func=cmd_canvas_detach)
    p = canvas_sub.add_parser("list", help="the running Canvases, their tabs and devices")
    p.set_defaults(func=cmd_canvas_list)

    media = sub.add_parser("media", help="device media library")
    media_sub = media.add_subparsers(dest="media_command", required=True)
    p = media_sub.add_parser("add", parents=[target_flags])
    p.add_argument("path")
    p.set_defaults(func=cmd_media_add)

    crash = sub.add_parser("crash", help="crash reports")
    crash_sub = crash.add_subparsers(dest="crash_command", required=True)
    p = crash_sub.add_parser("list", parents=[target_flags])
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_crash_list)
    p = crash_sub.add_parser("show", parents=[target_flags])
    p.add_argument("name")
    p.set_defaults(func=cmd_crash_show)

    files = sub.add_parser("file", help="app-container files")
    files_sub = files.add_subparsers(dest="file_command", required=True)
    p = files_sub.add_parser("ls", parents=[target_flags])
    p.add_argument("remote", nargs="?", default=".")
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_file_ls)
    p = files_sub.add_parser("pull", parents=[target_flags])
    p.add_argument("remote")
    p.add_argument("--app-id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_file_pull)

    record = sub.add_parser("record", help="screen recording")
    record_sub = record.add_subparsers(dest="record_command", required=True)
    p = record_sub.add_parser("start", parents=[target_flags])
    p.add_argument("--name", default="recording")
    p.set_defaults(func=cmd_record_start)
    p = record_sub.add_parser("stop", parents=[target_flags])
    p.set_defaults(func=cmd_record_stop)

    flow = sub.add_parser("flow", help="Flow v1: strict, repeatable flow files")
    flow_sub = flow.add_subparsers(dest="flow_command", required=True)
    p = flow_sub.add_parser("check",
                            help="validate flow files, including the whole runFlow graph")
    p.add_argument("path", help="a flow file or a directory of flows")
    p.set_defaults(func=cmd_flow_check)
    p = flow_sub.add_parser("fmt", help="canonical formatting")
    p.add_argument("path", help="a flow file or a directory of flows")
    p.add_argument("--write", action="store_true", help="rewrite files in place")
    p.add_argument("--check", action="store_true",
                   help="exit 1 when reformatting is needed")
    p.add_argument("--diff", action="store_true",
                   help="include a unified diff per changed file")
    p.add_argument("--drop-comments", action="store_true",
                   help="with --write: rewrite even though comments would be lost")
    p.set_defaults(func=cmd_flow_fmt)
    p = flow_sub.add_parser("list", help="list flows: file, id, name, tags, platforms")
    p.add_argument("path", nargs="?", help="directory to scan (default .autonom/flows)")
    p.set_defaults(func=cmd_flow_list)
    p = flow_sub.add_parser("create",
                            help="compile a recorded session into a flow file")
    p.add_argument("--from-session", required=True, metavar="ID",
                   help="a session id, or 'current'")
    p.add_argument("--out", help="write the flow here (else in the JSON)")
    p.add_argument("--name", help="flow name (default: Recorded <task>)")
    p.add_argument("--task", help="tag the flow and name it after this task")
    p.set_defaults(func=cmd_flow_create)

    teach = sub.add_parser("teach", help="record, compile, validate, and approve journeys")
    teach_sub = teach.add_subparsers(dest="teach_command", required=True)
    p = teach_sub.add_parser("start", help="start a marked recording range")
    p.add_argument("name")
    p.set_defaults(func=cmd_teach_start)
    p = teach_sub.add_parser("mark", help="add a named range marker")
    p.add_argument("name")
    p.set_defaults(func=cmd_teach_mark)
    p = teach_sub.add_parser("stop", help="finish the active recording range")
    p.set_defaults(func=cmd_teach_stop)
    p = teach_sub.add_parser("show", help="show recordings, ranges, and review state")
    p.set_defaults(func=cmd_teach_show)
    p = teach_sub.add_parser("compile", help="compile a recording or marker range")
    p.add_argument("--out", required=True)
    p.add_argument("--recording")
    p.add_argument("--from", dest="from_marker")
    p.add_argument("--to", dest="to_marker")
    p.set_defaults(func=cmd_teach_compile)
    p = teach_sub.add_parser("approve", help="approve after consecutive clean replays",
                             parents=[target_flags])
    p.add_argument("flow")
    p.add_argument("--minimum-runs", type=int, default=3)
    p.add_argument("--run", action="store_true",
                   help="perform the replays now instead of counting past ones")
    p.add_argument("--env", action="append", metavar="KEY=VALUE",
                   help="with --run: non-secret variable override (repeatable)")
    p.add_argument("--secret", action="append", metavar="NAME",
                   help="with --run: read NAME from the process environment as a "
                        "secret (repeatable)")
    p.set_defaults(func=cmd_teach_approve)

    app_skill = sub.add_parser("app-skill", help="validate and promote portable app knowledge")
    app_skill_sub = app_skill.add_subparsers(dest="app_skill_command", required=True)
    p = app_skill_sub.add_parser("validate")
    p.add_argument("app_id")
    p.add_argument("--workspace", default=".")
    p.set_defaults(func=cmd_app_skill_validate)
    p = app_skill_sub.add_parser("promote")
    p.add_argument("app_id")
    p.add_argument("flow")
    p.add_argument("--approval")
    p.add_argument("--workspace", default=".")
    p.set_defaults(func=cmd_app_skill_promote)
    p = flow_sub.add_parser("import",
                            help="convert a Maestro Core Profile flow to Flow v1")
    p.add_argument("path", help="a Maestro flow file")
    p.add_argument("--out", help="write the canonical flow here (else in the JSON)")
    p.set_defaults(func=cmd_flow_import)
    p = flow_sub.add_parser("export", help="convert a Flow v1 file to Maestro YAML")
    p.add_argument("path", help="a Flow v1 file")
    p.add_argument("--format", choices=("maestro",), default="maestro")
    p.add_argument("--out", help="write the Maestro flow here (else in the JSON)")
    p.set_defaults(func=cmd_flow_export)
    p = flow_sub.add_parser("run", help="execute flows against the active session",
                            parents=[target_flags])
    p.add_argument("path", help="a flow file, or a directory for a tag-filtered suite")
    p.add_argument("--include-tag", action="append", metavar="TAG",
                   help="directory runs: only flows carrying at least one included tag")
    p.add_argument("--exclude-tag", action="append", metavar="TAG",
                   help="directory runs: skip flows carrying any excluded tag")
    p.add_argument("--env", action="append", metavar="KEY=VALUE",
                   help="non-secret variable override (repeatable)")
    p.add_argument("--secret", action="append", metavar="NAME",
                   help="read NAME from the process environment as a secret "
                        "(repeatable; the value never enters artifacts)")
    p.add_argument("--default-timeout-ms", type=int, default=10_000,
                   help="assertion poll timeout when a step sets none")
    p.add_argument("--events", action="store_true",
                   help="stream NDJSON events to stdout instead of one summary doc")
    p.add_argument("--dry-run", action="store_true",
                   help="validate and pre-flight against the target; run nothing")
    p.add_argument("--until-step", type=int, metavar="N",
                   help="replay through runtime step N, preserve that device state, "
                        "and skip cleanup hooks")
    p.add_argument("--evidence",
                   choices=("flow", "minimal", "on-failure", "always"),
                   default="flow",
                   help="override the flow evidence mode (default: flow header)")
    p.add_argument("--collect", action="append",
                   choices=("screenshot", "hierarchy", "logs", "crashes", "network"),
                   help="override evidence kinds (repeatable)")
    p.set_defaults(func=cmd_flow_run)

    p = sub.add_parser("proof",
                       help="verify a code diff with the covering flow suite",
                       parents=[target_flags])
    p.add_argument("--base", required=True, help="git ref to diff against")
    p.add_argument("--head", help="git ref (default: the working tree)")
    p.add_argument("--repo", help="repository root (default: cwd)")
    p.add_argument("--flows", help="flow directory (default: <repo>/.autonom/flows)")
    p.add_argument("--out", help="write proof.json + proof.md into this directory")
    p.add_argument("--env", action="append", metavar="KEY=VALUE")
    p.add_argument("--secret", action="append", metavar="NAME")
    p.set_defaults(func=cmd_proof)

    pretest = sub.add_parser(
        "pretest", help="pre-test one ticket and its diff on the session's device")
    pretest_sub = pretest.add_subparsers(dest="pretest_command", required=True)
    p = pretest_sub.add_parser("start", help="start a pre-test of one ticket and its diff",
                               parents=[target_flags])
    p.add_argument("--ticket", required=True, metavar="FILE|-",
                   help="the ticket text (Markdown or plain text); '-' reads stdin")
    p.add_argument("--base", required=True, help="git ref the change is compared with")
    p.add_argument("--head", help="git ref of the change (default: the working tree)")
    p.add_argument("--repo", help="repository root (default: cwd)")
    p.add_argument("--flows", help="flow directory (default: <repo>/.autonom/flows)")
    p.add_argument("--title", help="pre-test title (default: the ticket's first line)")
    p.add_argument("--ticket-ref", metavar="KEY", help="the ticket's key, e.g. WB-123")
    p.add_argument("--app-id", help="the app under test (default: the session's)")
    p.add_argument("--max-actions", metavar="N",
                   help="mutating device actions allowed (default 300)")
    p.add_argument("--max-minutes", metavar="N",
                   help="minutes the pre-test may run (default 60)")
    p.add_argument("--forbid-text", action="append", metavar="PATTERN",
                   help="case-insensitive regex; a tap on matching text is refused "
                        "(repeatable; replaces the default list)")
    p.add_argument("--allow-app", action="append", metavar="ID",
                   help="app id open/launch may reach (repeatable; default: the session's app)")
    p.set_defaults(func=cmd_pretest_start)
    for verb, func, help_text in (
        ("status", cmd_pretest_status, "the active pre-test: budget, open check, counts "
                                       "(status 'damaged' when its record is unreadable)"),
        ("context", cmd_pretest_context, "the diff context: files, hunks, flows, screen hints"),
        ("stop", cmd_pretest_stop, "close the active pre-test without a pack"),
        ("health", cmd_pretest_health, "device-health deltas since the baseline"),
        ("list", cmd_pretest_list, "this session's pre-tests"),
    ):
        p = pretest_sub.add_parser(verb, help=help_text, parents=[target_flags])
        p.set_defaults(func=func)
        if verb == "stop":
            p.add_argument("--force", action="store_true",
                           help="also stop a pre-test whose record is damaged (kept as "
                                "pretest.json.damaged)")

    check = pretest_sub.add_parser("check", help="plan, open and close checks")
    check_sub = check.add_subparsers(dest="pretest_check_command", required=True)
    p = check_sub.add_parser("add", help="add a check", parents=[target_flags])
    p.add_argument("title", help="what the check verifies")
    p.add_argument("--kind", required=True, metavar="KIND",
                   help="ticket|edge|regression|network|a11y|analytics")
    p.add_argument("--case", metavar="KIT-ID", help="edge-case kit id ('pretest kit')")
    p.add_argument("--why", help="why this check matters for the ticket")
    p.add_argument("--flow", metavar="PATH", help="the flow file that runs this check")
    p.set_defaults(func=cmd_pretest_check_add)
    p = check_sub.add_parser("begin", help="open a check (one at a time)",
                             parents=[target_flags])
    p.add_argument("check_id")
    p.set_defaults(func=cmd_pretest_check_begin)
    p = check_sub.add_parser("end", help="close the open check with a verdict",
                             parents=[target_flags])
    p.add_argument("check_id")
    p.add_argument("--verdict", required=True, metavar="V",
                   help="pass|fail|blocked|not_covered|inconclusive")
    p.add_argument("--note", help="what was seen")
    p.add_argument("--observed-count", metavar="N",
                   help="requests counted (network requests); a pass of the double-tap "
                        "case needs it")
    p.set_defaults(func=cmd_pretest_check_end)
    p = check_sub.add_parser("amend", help="correct the verdict of an ended check",
                             parents=[target_flags])
    p.add_argument("check_id")
    p.add_argument("--verdict", required=True, metavar="V",
                   help="pass|fail|blocked|not_covered|inconclusive")
    p.add_argument("--note", required=True, help="why the verdict changes")
    p.add_argument("--observed-count", metavar="N",
                   help="requests counted; a pass of the double-tap case needs one "
                        "(given here or at check end)")
    p.set_defaults(func=cmd_pretest_check_amend)
    p = check_sub.add_parser("list", help="every check with state, verdict and time",
                             parents=[target_flags])
    p.set_defaults(func=cmd_pretest_check_list)

    p = pretest_sub.add_parser("flows", help="run the selected flows as regression checks",
                               parents=[target_flags])
    p.add_argument("--env", action="append", metavar="KEY=VALUE")
    p.add_argument("--secret", action="append", metavar="NAME")
    p.set_defaults(func=cmd_pretest_flows)

    finding = pretest_sub.add_parser("finding", help="record and list findings")
    finding_sub = finding.add_subparsers(dest="pretest_finding_command", required=True)
    p = finding_sub.add_parser("add", help="record a finding with evidence",
                               parents=[target_flags])
    p.add_argument("--check", required=True, metavar="ID", help="the check it belongs to")
    p.add_argument("--severity", required=True, metavar="S",
                   help="blocker|major|minor|info")
    p.add_argument("--title", required=True)
    p.add_argument("--actual", required=True, help="what happened")
    p.add_argument("--expected", help="what should have happened")
    p.add_argument("--evidence", action="append", metavar="REF",
                   help="shot:<id>|tree:<file>|action:<id>|request:<id>|log:<from>..<to>"
                        "|run:<run_id>|crash:<name> (repeatable)")
    p.set_defaults(func=cmd_pretest_finding_add)
    p = finding_sub.add_parser("list", help="every finding", parents=[target_flags])
    p.set_defaults(func=cmd_pretest_finding_list)

    p = pretest_sub.add_parser("pack", help="finish the pre-test and write its pack",
                               parents=[target_flags])
    p.add_argument("--out", metavar="DIR",
                   help="an empty or new directory (default: <pre-test>/pack)")
    p.set_defaults(func=cmd_pretest_pack)

    p = pretest_sub.add_parser("review", help="record a QA decision beside a pack",
                               parents=[target_flags])
    p.add_argument("pack", help="the pack directory")
    p.add_argument("decision", choices=("approve", "more-checks", "return-to-dev"))
    p.add_argument("--note", help="the reviewer's note")
    p.add_argument("--check", action="append", metavar="TITLE",
                   help="more-checks: a check the child pre-test starts with (repeatable)")
    p.add_argument("--finding", action="append", metavar="ID",
                   help="return-to-dev: a finding to fix (repeatable)")
    p.add_argument("--confirm", action="append", metavar="ID",
                   help="confirm an automatic finding (repeatable)")
    p.add_argument("--dismiss", action="append", metavar="ID",
                   help="dismiss an automatic finding (repeatable)")
    p.add_argument("--author", default="qa", metavar="NAME", help="who decided (default qa)")
    p.add_argument("--app-id",
                   help="more-checks: the child's app (default: this session's, never the pack's)")
    p.add_argument("--max-actions", metavar="N",
                   help="more-checks: the child's action budget (default 300)")
    p.add_argument("--max-minutes", metavar="N",
                   help="more-checks: the child's time budget (default 60)")
    p.add_argument("--forbid-text", action="append", metavar="PATTERN",
                   help="more-checks: replaces the child's default forbidden-text list "
                        "(repeatable)")
    p.add_argument("--allow-app", action="append", metavar="ID",
                   help="more-checks: an app the child may open (repeatable; default: "
                        "the child's app)")
    p.set_defaults(func=cmd_pretest_review)
    p = pretest_sub.add_parser("review-status", help="the review decisions of a pack")
    p.add_argument("pack", help="the pack directory")
    p.set_defaults(func=cmd_pretest_review_status)
    p = pretest_sub.add_parser("kit", help="the edge-case kit (and its coverage)")
    p.set_defaults(func=cmd_pretest_kit)
    p = pretest_sub.add_parser("flow-draft", help="compile a finding's check into a draft flow",
                               parents=[target_flags])
    p.add_argument("finding_id", help="the finding, e.g. f-1")
    p.add_argument("--pack", metavar="PATH",
                   help="read the finding from this pack instead of the active pre-test "
                        "(nothing is recorded)")
    p.add_argument("--out", metavar="PATH",
                   help="the draft file (default: <repo>/.autonom/flows/drafts/"
                        "<ticket-slug>-<finding-id>.yaml)")
    p.set_defaults(func=cmd_pretest_flow_draft)
    p = pretest_sub.add_parser("repro", help="replay a finding's draft N times",
                               parents=[target_flags])
    p.add_argument("finding_id", help="the finding, e.g. f-1")
    p.add_argument("--times", required=True, type=int, metavar="N", help="runs, 1 to 10")
    p.add_argument("--pack", metavar="PATH",
                   help="a pack is final: refused with pretest_finalized (use rerun)")
    p.add_argument("--env", action="append", metavar="KEY=VALUE")
    p.add_argument("--secret", action="append", metavar="NAME")
    p.set_defaults(func=cmd_pretest_repro)
    p = pretest_sub.add_parser("rerun", help="rerun a pack's checks in a linked child pre-test",
                               parents=[target_flags])
    p.add_argument("pack", help="the pack directory")
    p.add_argument("--check", action="append", metavar="ID",
                   help="a parent check to rerun (repeatable; default: every check with "
                        "a flow or draft)")
    p.add_argument("--env", action="append", metavar="KEY=VALUE")
    p.add_argument("--secret", action="append", metavar="NAME")
    p.set_defaults(func=cmd_pretest_rerun)
    p = pretest_sub.add_parser("warmup", help="run the app profile's login and warm-up "
                               "flows, then walk the Runtime Map to the hinted screens",
                               parents=[target_flags])
    p.add_argument("--env", action="append", metavar="KEY=VALUE")
    p.add_argument("--secret", action="append", metavar="NAME")
    p.set_defaults(func=cmd_pretest_warmup)
    p = pretest_sub.add_parser("a11y", help="accessibility rules on the current screen's tree",
                               parents=[target_flags])
    p.add_argument("--check", metavar="ID",
                   help="the check the findings belong to (default: the open check)")
    p.set_defaults(func=cmd_pretest_a11y)
    p = pretest_sub.add_parser("events", help="match the profile's analytics events against "
                               "captured requests", parents=[target_flags])
    p.add_argument("--check", metavar="ID",
                   help="the check whose window is searched (default: the open check, "
                        "else the whole pre-test)")
    p.set_defaults(func=cmd_pretest_events)

    atlas = sub.add_parser("atlas", aliases=["runtime-map"],
                           help="the observed Runtime Map application graph")
    atlas_sub = atlas.add_subparsers(dest="atlas_command", required=True)
    p = atlas_sub.add_parser("update", help="ingest a session's runs and actions")
    p.add_argument("--session", default="current", help="session id (default: current)")
    p.add_argument("--app-id", help="override the session's app id")
    p.set_defaults(func=cmd_atlas_update)
    p = atlas_sub.add_parser("show", help="screens, variants, transitions")
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_atlas_show)
    p = atlas_sub.add_parser("coverage", help="observed edges with evidence refs")
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_atlas_coverage)
    p = atlas_sub.add_parser("paths", help="observed routes between two screens")
    p.add_argument("--from", required=True, metavar="SCREEN",
                   help="screen id or label substring")
    p.add_argument("--to", required=True, metavar="SCREEN")
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_atlas_paths)
    p = atlas_sub.add_parser("export", help="write a graph snapshot")
    p.add_argument("--out", required=True)
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_atlas_export)
    p = atlas_sub.add_parser("diff", help="compare two graph snapshots")
    p.add_argument("--base", required=True, help="a snapshot file")
    p.add_argument("--head", help="a snapshot file (default: the live graph)")
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_atlas_diff)

    report = sub.add_parser("report", help="evidence reports for flow runs")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    for verb, func, extra in (
        ("build", cmd_report_build, "render report.html + report.xml into the run dir"),
        ("open", cmd_report_open, "open the HTML report in a browser"),
        ("export", cmd_report_export, "write one report format to a path"),
    ):
        p = report_sub.add_parser(verb, help=extra)
        p.add_argument("--session", default="current",
                       help="session id (default: current)")
        p.add_argument("--run", help="run id (default: the latest run)")
        if verb == "export":
            p.add_argument("--format",
                           choices=("html", "junit", "allure", "agent", "csv", "metrics"),
                           default="html")
            p.add_argument("--out", help="destination path")
        p.set_defaults(func=func)

    p = report_sub.add_parser(
        "serve", help="serve a local report with replay-to-step controls",
        parents=[target_flags])
    p.add_argument("--session", default="current",
                   help="session id (default: current)")
    p.add_argument("--run", help="run id (default: the latest run)")
    p.add_argument("--port", type=int, default=0,
                   help="loopback port (default: choose a free port)")
    p.add_argument("--open", action="store_true", help="open it in a browser")
    p.set_defaults(func=cmd_report_serve)

    p = report_sub.add_parser(
        "suite", help="one report over every flow run in the session")
    p.add_argument("--session", default="current",
                   help="session id (default: current)")
    p.add_argument("--last", type=int,
                   help="only the latest run of each of the N most recently run "
                        "flows (default: every run)")
    p.add_argument("--out", help="destination directory")
    p.add_argument("--relative-to", metavar="DIR",
                   help="strip this directory from paths (share a report "
                        "without leaking local paths)")
    p.add_argument("--detailed", action="store_true",
                   help="also write one page per flow with its steps")
    p.add_argument("--screenshots", choices=("none", "failed", "all"),
                   default="failed",
                   help="which runs' frames to copy next to the report "
                        "(default: failed)")
    p.add_argument("--open", action="store_true", help="open it in a browser")
    p.set_defaults(func=cmd_report_suite)

    for verb, func, help_text in (
        ("model", cmd_report_model, "compile canonical Report Model v2"),
        ("bundle", cmd_report_bundle, "finalize a portable Report Bundle v2"),
    ):
        p = report_sub.add_parser(verb, help=help_text)
        p.add_argument("--session", default="current")
        p.add_argument("--run")
        p.add_argument("--out")
        p.set_defaults(func=func)
    p = report_sub.add_parser("verify", help="verify every Report Bundle digest")
    p.add_argument("bundle")
    p.set_defaults(func=cmd_report_verify)
    p = report_sub.add_parser("annotate", help="add a mutable note beside finalized evidence")
    p.add_argument("bundle")
    p.add_argument("text")
    p.add_argument("--author", default="human")
    p.add_argument("--step")
    p.set_defaults(func=cmd_report_annotate)
    p = report_sub.add_parser("gate", help="evaluate explicit report quality gates")
    p.add_argument("--session", default="current")
    p.add_argument("--run")
    p.add_argument("--rules", help="JSON gate rules")
    p.add_argument("--out")
    p.set_defaults(func=cmd_report_gate)
    p = report_sub.add_parser("history", help="aggregate retries and flaky history")
    p.add_argument("--session", default="current")
    p.add_argument("--last", type=int)
    p.add_argument("--out")
    p.set_defaults(func=cmd_report_history)
    p = report_sub.add_parser("watch", help="stream newly finalized summaries as NDJSON")
    p.add_argument("--session", default="current")
    p.add_argument("--interval-ms", type=int, default=250)
    p.add_argument("--max-seconds", type=float, default=0)
    p.add_argument("--max-runs", type=int, default=0)
    p.set_defaults(func=cmd_report_watch)
    p = report_sub.add_parser("pack", help="pack one verified bundle for a CI shard")
    p.add_argument("bundle")
    p.add_argument("--out", required=True)
    p.add_argument("--shard-id", default="shard-1")
    p.set_defaults(func=cmd_report_pack)
    p = report_sub.add_parser("merge", help="idempotently merge CI shard packs")
    p.add_argument("packs", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--expected-shards", type=int)
    p.set_defaults(func=cmd_report_merge)
    p = report_sub.add_parser("finalize", help="evaluate campaign gates and publish")
    p.add_argument("campaign")
    p.add_argument("--rules")
    p.add_argument("--publish", help="empty directory or HTTP PUT endpoint")
    p.set_defaults(func=cmd_report_finalize)

    p = sub.add_parser("replay", help="replay a pinned attempt or restore a selected step",
                       parents=[target_flags])
    source = p.add_mutually_exclusive_group()
    source.add_argument("--bundle")
    source.add_argument("--run")
    p.add_argument("--session", default="current")
    p.add_argument("--to-step", help="runtime index, stable step id, or checkpoint name")
    p.set_defaults(func=cmd_replay)

    ci = sub.add_parser("ci", help="supervised run, spool, shard, merge, finalize, publish")
    ci_sub = ci.add_subparsers(dest="ci_command", required=True)
    p = ci_sub.add_parser("run", parents=[target_flags])
    p.add_argument("path")
    p.add_argument("--out", required=True)
    p.add_argument("--campaign-id")
    p.add_argument("--shard-id", default="shard")
    p.add_argument("--expected-shards", type=int)
    p.add_argument("--env", action="append", metavar="KEY=VALUE")
    p.add_argument("--secret", action="append", metavar="NAME")
    p.add_argument("--rules")
    p.add_argument("--publish")
    p.set_defaults(func=cmd_ci_run)
    p = ci_sub.add_parser("pack")
    p.add_argument("bundle")
    p.add_argument("--out", required=True)
    p.add_argument("--shard-id", default="shard-1")
    p.set_defaults(func=cmd_report_pack)
    p = ci_sub.add_parser("merge")
    p.add_argument("packs", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--expected-shards", type=int)
    p.set_defaults(func=cmd_report_merge)
    p = ci_sub.add_parser("finalize")
    p.add_argument("campaign")
    p.add_argument("--rules")
    p.add_argument("--publish")
    p.set_defaults(func=cmd_report_finalize)
    p = ci_sub.add_parser("publish")
    p.add_argument("campaign")
    p.add_argument("destination")
    p.set_defaults(func=cmd_ci_publish)

    agent = sub.add_parser("agent", help="compact machine report access")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    p = agent_sub.add_parser("export")
    p.add_argument("--bundle")
    p.add_argument("--session", default="current")
    p.add_argument("--run")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_agent_export)
    p = agent_sub.add_parser("inspect")
    p.add_argument("--bundle")
    p.add_argument("--session", default="current")
    p.add_argument("--run")
    p.add_argument("--step", required=True)
    p.set_defaults(func=cmd_agent_inspect)

    network = sub.add_parser("network", help="HTTP(S) capture and mocking")
    network_sub = network.add_subparsers(dest="network_command", required=True)

    p = network_sub.add_parser("start", help="start the MITM proxy", parents=[target_flags])
    p.add_argument("--port", type=int, help="listen port (auto when omitted)")
    p.add_argument("--capture-bodies", action="store_true",
                   help="persist full bodies (off by default: they carry credentials)")
    p.add_argument("--mitmdump", help="path to mitmdump binary")
    p.add_argument("--ignore-hosts", help="regex of hosts to tunnel without interception")
    p.add_argument("--intercept-connectivity-checks", action="store_true",
                   help="also intercept captive-portal probes; doing so makes the OS "
                        "mark the network unvalidated and apps then go idle")
    p.add_argument("--i-understand-mitm", action="store_true",
                   help="acknowledge that traffic will be decrypted and recorded")
    p.set_defaults(func=cmd_network_start)

    p = network_sub.add_parser("stop", help="stop the proxy", parents=[target_flags])
    p.set_defaults(func=cmd_network_stop)

    p = network_sub.add_parser("status", help="proxy and attachment status",
                               parents=[target_flags])
    p.set_defaults(func=cmd_network_status)

    p = network_sub.add_parser("attach", help="point the target at the proxy",
                               parents=[target_flags])
    p.add_argument("--i-understand-mitm", action="store_true")
    p.add_argument("--no-network-cycle", action="store_true",
                   help="skip the Wi-Fi cycle that makes Android adopt the proxy; "
                        "without it the setting is stored but never applied")
    p.add_argument("--install-ca", action="store_true",
                   help="also seed the CA certificate into the target's trust store")
    p.add_argument("--system-ca", action="store_true",
                   help="transparent capture (Android emulator): install the MITM CA into "
                        "the SYSTEM trust store on a rooted emulator that was booted with "
                        "'devices boot --http-proxy'. Captures Flutter/pinned-store traffic "
                        "with no app change; refuses when the emulator is not proxy-routed")
    p.set_defaults(func=cmd_network_attach)

    p = network_sub.add_parser("detach", help="restore the target's previous proxy",
                               parents=[target_flags])
    p.set_defaults(func=cmd_network_detach)

    requests = network_sub.add_parser("requests", help="recorded flows")
    requests_sub = requests.add_subparsers(dest="requests_command", required=True)
    p = requests_sub.add_parser("list", parents=[target_flags])
    p.add_argument("--host")
    p.add_argument("--method")
    p.add_argument("--status", type=int)
    p.add_argument("--path", help="glob over path or url")
    p.add_argument("--since", type=float, help="seconds")
    p.add_argument("--mocked", nargs="?", const=True, default=None, type=_bool_flag)
    # None means the store's own DEFAULT_MAX, read when the verb runs: the
    # parser is built for every call and must not import the network store.
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--since-id", help="only flows recorded after this id")
    p.set_defaults(func=cmd_network_requests_list)
    p = requests_sub.add_parser("follow", parents=[target_flags],
                                help="poll the store, stream new flows as NDJSON")
    p.add_argument("--host")
    p.add_argument("--method")
    p.add_argument("--status", type=int)
    p.add_argument("--path", help="glob over path or url")
    p.add_argument("--mocked", nargs="?", const=True, default=None, type=_bool_flag)
    p.add_argument("--interval", type=float, default=1.0, help="poll seconds")
    p.add_argument("--max", type=int, default=0, help="stop after N new flows")
    p.add_argument("--max-seconds", type=float, default=0,
                   help="stop after N seconds (0 = until interrupted)")
    p.add_argument("--from-start", action="store_true",
                   help="emit already-recorded flows first, then follow")
    p.set_defaults(func=cmd_network_requests_follow)
    p = requests_sub.add_parser("show", parents=[target_flags])
    p.add_argument("id")
    p.add_argument("--full", action="store_true", help="include full bodies when captured")
    p.set_defaults(func=cmd_network_requests_show)

    p = network_sub.add_parser("export", help="write a HAR 1.2 file", parents=[target_flags])
    p.add_argument("--har", default="network/session.har")
    p.set_defaults(func=cmd_network_export)

    mock = network_sub.add_parser(
        "mock", help="persistent response mock registry (survives restarts)")
    mock_sub = mock.add_subparsers(dest="mock_command", required=True)

    def mock_shape(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """Flags shared by add and update, so the two can never drift apart."""
        parser.add_argument("--url", help="exact endpoint, e.g. "
                                          "'https://api.example.net/post/update/12341' "
                                          "(query string ignored)")
        parser.add_argument("--match", help="URL glob, e.g. '*/v1/login'")
        parser.add_argument("--method")
        parser.add_argument("--host")
        parser.add_argument("--status", type=int)
        parser.add_argument("--header", action="append", help="'Name: value' (repeatable)")
        parser.add_argument("--json", help="inline response body; sets Content-Type "
                                           "automatically when it looks like JSON")
        parser.add_argument("--body-file", help="response body file; without a Content-Type "
                                                "--header its type follows the extension")
        parser.add_argument("--note", help="free-text label, shown in list")
        parser.add_argument("--delay", metavar="SECONDS",
                            help="answer after SECONDS (0 < SECONDS <= 120, e.g. 2.5); "
                                 "other requests are not held")
        parser.add_argument("--hang", action="store_true", default=None,
                            help="hold the request until the mock is removed or disabled "
                                 "(then it goes upstream) or 120 s pass (then it is "
                                 "recorded incomplete); not with --delay")
        return parser

    p = mock_shape(mock_sub.add_parser("add", parents=[target_flags]))
    p.set_defaults(func=cmd_network_mock_add)
    p = mock_sub.add_parser("list", parents=[target_flags])
    p.add_argument("--all", action="store_true", help="include disabled rules")
    p.set_defaults(func=cmd_network_mock_list)
    p = mock_sub.add_parser("show", parents=[target_flags])
    p.add_argument("id")
    p.set_defaults(func=cmd_network_mock_show)
    p = mock_shape(mock_sub.add_parser("update", parents=[target_flags]))
    p.add_argument("id")
    p.set_defaults(func=cmd_network_mock_update)
    p = mock_sub.add_parser("enable", parents=[target_flags])
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_network_mock_enable)
    p = mock_sub.add_parser("disable", parents=[target_flags])
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_network_mock_disable)
    p = mock_sub.add_parser("remove", parents=[target_flags])
    p.add_argument("id")
    p.set_defaults(func=cmd_network_mock_remove)
    p = mock_sub.add_parser("clear", parents=[target_flags])
    p.set_defaults(func=cmd_network_mock_clear)

    metrics = sub.add_parser("metrics", help="load metrics: snapshot, series, presets")
    metrics_sub = metrics.add_subparsers(dest="metrics_command", required=True)
    p = metrics_sub.add_parser("snapshot", parents=[target_flags],
                               help="one memory/CPU summary of the app right now")
    p.add_argument("--app-id", help="package / bundle id (defaults to the session's)")
    p.add_argument("--label", help="short name used in artifact filenames")
    p.add_argument("--task", help="task label recorded with the artifact")
    p.add_argument("--out", help="artifact dir or .json file (default: session metrics/)")
    p.set_defaults(func=cmd_metrics_snapshot)
    p = metrics_sub.add_parser("series", parents=[target_flags],
                               help="N snapshots + deltas and directional-growth leads")
    p.add_argument("--app-id")
    p.add_argument("--label")
    p.add_argument("--task")
    p.add_argument("--out")
    p.add_argument("--count", type=int, default=5, help="snapshots to take")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between snapshots")
    p.add_argument("--min-growth-kb", type=int, default=1024,
                   help="minimum first→last growth to flag a lead")
    p.add_argument("--from-dir", help="summarize existing snapshot files instead of capturing")
    p.add_argument("--glob", default="*-snapshot.json",
                   help="pattern applied under --from-dir")
    p.set_defaults(func=cmd_metrics_series)
    memory = metrics_sub.add_parser("memory", help="Android evidence pack + iOS stimulus")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    p = memory_sub.add_parser("capture", parents=[target_flags],
                              help="meminfo + proc + gfxinfo + optional HPROF")
    p.add_argument("--app-id")
    p.add_argument("--label", default="capture")
    p.add_argument("--out", help="artifact dir (default: session metrics/)")
    p.add_argument("--no-hprof", action="store_true",
                   help="skip the Java/Kotlin heap dump")
    p.set_defaults(func=cmd_metrics_memory_capture)
    p = memory_sub.add_parser("analyze", parents=[target_flags],
                              help="series math over captured meminfo files")
    p.add_argument("--dir", help="capture dir (default: session metrics/)")
    p.add_argument("--glob", default="*-meminfo.txt")
    p.add_argument("--min-growth-kb", type=int, default=1024)
    p.set_defaults(func=cmd_metrics_memory_analyze)
    p = memory_sub.add_parser("warn", parents=[target_flags],
                              help="inject a simulated memory warning (iOS Simulator)")
    p.set_defaults(func=cmd_metrics_memory_warn)

    frames = metrics_sub.add_parser("frames", help="frame statistics")
    frames_sub = frames.add_subparsers(dest="frames_command", required=True)
    p = frames_sub.add_parser("reset", parents=[target_flags],
                              help="zero gfxinfo counters before a flow")
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_metrics_frames_reset)
    p = frames_sub.add_parser("capture", parents=[target_flags],
                              help="gfxinfo framestats after the flow")
    p.add_argument("--app-id")
    p.add_argument("--label", default="frames")
    p.add_argument("--out")
    p.set_defaults(func=cmd_metrics_frames_capture)
    p = frames_sub.add_parser("flutter-summary", parents=[target_flags],
                              help="summarize a Flutter frame-timings JSON file")
    p.add_argument("file")
    p.add_argument("--budget-ms", type=float, default=16.67)
    p.set_defaults(func=cmd_metrics_frames_flutter)

    p = metrics_sub.add_parser("trace", parents=[target_flags],
                               help="heavy profile with an explicit duration")
    p.add_argument("--preset", required=True,
                   choices=("simpleperf", "gfxinfo-flow", "allocations",
                            "time-profiler", "leaks", "hitches"))
    p.add_argument("--duration", type=float, default=30.0, help="seconds")
    p.add_argument("--app-id")
    p.add_argument("--label", default="trace")
    p.add_argument("--out")
    p.set_defaults(func=cmd_metrics_trace)

    p = metrics_sub.add_parser("list-presets", parents=[target_flags],
                               help="which heavy profilers this host can run")
    p.set_defaults(func=cmd_metrics_list_presets)

    logs = sub.add_parser("logs", help="device logs")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    p = logs_sub.add_parser("tail", help="tail recent logs", parents=[target_flags])
    p.add_argument("--package", help="filter by package / bundle id")
    p.add_argument("--since", type=float, default=30, help="seconds of recent logs (best-effort)")
    p.add_argument("--max-lines", type=int, default=200)
    p.add_argument("--grep", help="regex filter")
    p.set_defaults(func=cmd_logs_tail)
    p = logs_sub.add_parser("follow", help="stream a session file or the device "
                                           "log as NDJSON", parents=[target_flags])
    p.exact_only_options = _ABBREVIABLE_SESSION_ID  # its --session-id predates the rule
    p.add_argument("--source", help="'device', a stream id, or output:<name> / "
                                    "logs:<name> / network:<name>")
    p.add_argument("--path", help="file to tail, relative to the session artifacts dir")
    p.add_argument("--package", help="device mode: filter to this package / bundle id")
    p.add_argument("--from-start", action="store_true",
                   help="replay the existing file, then follow (default: start at end)")
    p.add_argument("--max-seconds", type=float, default=0,
                   help="stop after N seconds (0 = until interrupted; CI must bound)")
    p.add_argument("--max-lines", type=int, default=0, help="stop after N emitted lines")
    p.add_argument("--grep", help="regex filter; only matching lines are emitted")
    p.add_argument("--poll-ms", type=int, default=250,
                   help="file poll interval in milliseconds")
    p.set_defaults(func=cmd_logs_follow)

    control = sub.add_parser(
        "control", help="the host-wide control lease of a target: status, acquire, "
                        "renew, release")
    control_sub = control.add_subparsers(dest="control_command", required=True)
    p = control_sub.add_parser("status", parents=[target_flags],
                               help="the lease of the target (every lease without a "
                                    "target or session, or with --all)")
    p.add_argument("--all", action="store_true", help="every lease file of this machine")
    p.set_defaults(func=cmd_control_status)
    p = control_sub.add_parser("acquire", parents=[target_flags],
                               help="take the target's lease (renews your own live lease)")
    p.add_argument("--holder", metavar="ID",
                   help="holder id (default: --control-holder / AUTONOM_CONTROL_HOLDER)")
    p.add_argument("--kind", choices=control_mod.KINDS, default="agent")
    p.add_argument("--label", metavar="TEXT", help="shown to people (default: the holder id)")
    p.add_argument("--ttl", type=int, metavar="SECONDS",
                   help=f"lease lifetime ({control_mod.MIN_TTL_S}-{control_mod.MAX_TTL_S}; "
                        f"default {control_mod.DEFAULT_AGENT_TTL_S} for an agent)")
    p.add_argument("--takeover", action="store_true",
                   help="replace a live holder (a human only)")
    p.set_defaults(func=cmd_control_acquire)
    p = control_sub.add_parser("release", parents=[target_flags],
                               help="give the lease back (the generation is kept)")
    p.add_argument("--holder", metavar="ID",
                   help="holder id (default: --control-holder / AUTONOM_CONTROL_HOLDER)")
    p.add_argument("--generation", metavar="N",
                   help="the generation your acquire returned (checked when given)")
    p.set_defaults(func=cmd_control_release)
    p = control_sub.add_parser("renew", parents=[target_flags],
                               help="extend your live lease at the same generation")
    p.add_argument("--holder", metavar="ID",
                   help="holder id (default: --control-holder / AUTONOM_CONTROL_HOLDER)")
    p.add_argument("--generation", metavar="N",
                   help="the generation your acquire returned (default: "
                        "--control-generation / AUTONOM_CONTROL_GENERATION)")
    p.add_argument("--ttl", type=int, metavar="SECONDS",
                   help=f"new lifetime from now ({control_mod.MIN_TTL_S}-{control_mod.MAX_TTL_S}; "
                        "default: the lease's own lifetime)")
    p.set_defaults(func=cmd_control_renew)

    return parser


# Verbs whose own handler writes the journal, or that only read it — never
# journaled by the choke point, to avoid double entries and read-noise.
_JOURNAL_SKIP = {"note", "journal", "version", "teach"}

# Sub-command dests, in priority order, for reconstructing a verb string.
_SUBCOMMAND_DESTS = (
    "session_command", "ui_command", "flow_command", "atlas_command",
    "report_command", "network_command", "requests_command", "mock_command",
    "location_command", "media_command", "crash_command", "file_command",
    "record_command", "shots_command", "devices_command", "note_command",
    "logs_command", "metrics_command", "teach_command", "app_skill_command",
    "simulator_command", "canvas_command", "ci_command", "agent_command",
    "pretest_command", "pretest_check_command", "pretest_finding_command",
    "control_command",
)


def _verb_string(args: argparse.Namespace) -> str:
    parts = [getattr(args, "command", "") or ""]
    for dest in _SUBCOMMAND_DESTS:
        value = getattr(args, dest, None)
        if value:
            parts.append(value)
            break
    return " ".join(part for part in parts if part)


def _journal_command(args: argparse.Namespace, argv: list[str], ok: bool,
                     error_code: str | None, before_session: dict | None = None) -> None:
    command = getattr(args, "command", None)
    if command in _JOURNAL_SKIP:
        return
    try:
        # Never a stopped session: one named with --session-id is only read.
        session = session_mod.journal_session()
    except Exception:  # noqa: BLE001
        session = None
    if getattr(args, "session_command", None) == "stop":
        session = before_session
    if not session:
        return
    argv, args = _redact_pair_values(args, argv)
    # The invocation's operation id, only when the verb was admitted under a
    # lease or a claim (its `control` object was emitted) or a lease refused
    # it: entries of verbs on targets without a lease keep their shape.
    emitted = _LAST_EMIT if isinstance(_LAST_EMIT, dict) else {}
    operation_id = None
    if isinstance(emitted.get("control"), dict) or error_code in _CONTROL_ERROR_CODES:
        operation_id = control_mod.invocation().get("operation_id")
    journal_mod.record_action(
        session, verb=_verb_string(args), argv=argv,
        payload=_LAST_EMIT, ok=ok, error_code=error_code,
        args=args, parser=_ROOT_PARSER, operation_id=operation_id,
    )
    if ok:
        _journal_mock_state(session, args)


# `network mock` sub-commands that change the registry.
_MOCK_CHANGES = frozenset({"add", "update", "enable", "disable", "remove", "clear"})


def _journal_mock_state(session: dict[str, Any], args: argparse.Namespace) -> None:
    """While a pre-test is active, journal the mock registry as it stands
    after a `network mock` change and at `pretest start` (a `mock_state`
    entry: ids, URL globs, methods, hosts, statuses, enabled flags), so the
    pack can tell which mocks were active at each step. Nothing is written
    without an active pre-test; a failure here never fails the command."""
    command = getattr(args, "command", None)
    changed = (command == "network" and getattr(args, "network_command", None) == "mock"
               and getattr(args, "mock_command", None) in _MOCK_CHANGES)
    started = command == "pretest" and getattr(args, "pretest_command", None) == "start"
    if not (changed or started):
        return
    try:
        from autonom_lib.network import mocks as mocks_mod
        from autonom_lib.pretest import repro as pretest_repro
        from autonom_lib.pretest import store as pretest_store

        if not pretest_store.attribution(session):
            return
        journal_mod.append(session, pretest_repro.mock_state_entry(mocks_mod.load()))
    except Exception:  # noqa: BLE001 — journaling is best-effort
        pass


_PAIR_FLAGS = ("--env", "--setenv")


def _pair_dests(args: argparse.Namespace) -> list[str]:
    """Dests of this verb's KEY=VALUE options (--env, --setenv, --value ...),
    found by walking the parser the verb was parsed with."""
    if _ROOT_PARSER is None:
        return ["env", "setenv"]
    leaf = _leaf_parser(_ROOT_PARSER, args)
    return sorted({action.dest for action in leaf._actions  # noqa: SLF001
                   if any(flag in _PAIR_FLAGS for flag in action.option_strings)
                   or action.metavar == "KEY=VALUE"})


def _redact_pair_values(args: argparse.Namespace,
                        argv: list[str]) -> tuple[list[str], argparse.Namespace]:
    """`--env K=V` (any spelling: `--env=K=V`, `--en K=V`) as `K=<redacted>`.

    Whether a value is secret is not knowable from its key: `--env CODE=...`
    feeding a `sensitive: true` slot looked harmless. So every KEY=VALUE
    option value is masked in the journal, the key kept, argv kept 1:1; the
    values are located through the parsed args, not by guessing spellings.
    """
    values: set[str] = set()
    redacted = argparse.Namespace(**vars(args))
    for dest in _pair_dests(args):
        items = getattr(args, dest, None)
        if not isinstance(items, list):
            continue
        values.update(str(item) for item in items)
        setattr(redacted, dest, [_masked_pair(str(item)) for item in items])
    if not values:
        return argv, args
    out: list[str] = []
    for token in argv:
        if token in values:
            out.append(_masked_pair(token))
        elif token.startswith("--") and "=" in token and token.split("=", 1)[1] in values:
            flag, value = token.split("=", 1)
            out.append(f"{flag}={_masked_pair(value)}")
        else:
            out.append(token)
    return out, redacted


def _masked_pair(value: str) -> str:
    key, sep, _ = value.partition("=")
    return f"{key}=<redacted>" if sep else "<redacted>"


def _leaf_parser(parser: argparse.ArgumentParser,
                 args: argparse.Namespace) -> argparse.ArgumentParser:
    """The deepest sub-parser the parsed verb path reaches."""
    node = parser
    while True:
        action = next((item for item in node._actions  # noqa: SLF001
                       if isinstance(item, argparse._SubParsersAction)), None)  # noqa: SLF001
        if action is None:
            return node
        chosen = action.choices.get(getattr(args, action.dest, None) or "")
        if chosen is None:
            return node
        node = chosen


def _parse(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """`parse_args`, except that an unknown flag is reported by the verb it
    was given to: the hint is `ui tap`'s usage, not the root's 60-verb list."""
    args, extras = parser.parse_known_args(argv)
    if extras:
        _leaf_parser(parser, args).error(
            "unrecognized arguments: " + " ".join(extras))
    return args


def _apply_ios_overrides(args: argparse.Namespace) -> None:
    """`--axe` / `--ios-hid` become the environment variables ui_ios reads."""
    axe = getattr(args, "axe", None)
    if axe:
        os.environ["AUTONOM_AXE"] = str(axe)
    hid = getattr(args, "ios_hid", None)
    if hid:
        os.environ["AUTONOM_IOS_HID"] = hid


def _os_error(exc: OSError) -> errors.AutonomError:
    """An OSError that escaped a verb: an unwritable output, or a backend."""
    where = exc.filename or ""
    if isinstance(exc, (PermissionError, IsADirectoryError, NotADirectoryError)) or \
            exc.errno in (errno.EACCES, errno.EROFS, errno.ENOSPC, errno.EISDIR):
        return errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"cannot write {where}: {exc.strerror or exc}" if where
            else f"cannot write: {exc.strerror or exc}",
            "Pass an output path in a directory you can write to.",
            **({"path": str(where)} if where else {}),
        )
    return errors.AutonomError(
        errors.BACKEND_FAILED, f"{exc.strerror or exc}" + (f": {where}" if where else ""),
        "Check the path and the tool it names; 'autonom doctor' lists the toolchain.",
    )


# Verbs that only read what a session recorded: a stopped session may be
# named with --session-id for these, and for nothing that drives a device.
_READONLY_COMMANDS = {"journal", "shots", "logs", "report"}
_READONLY_SESSION_COMMANDS = {"show", "outputs", "list"}
# `report serve` replays flow steps on the device through /replay, so it is
# not a read-only verb for a stopped session named with --session-id.
_DRIVING_REPORT_COMMANDS = {"serve"}


def _bind_to_target_session(args: argparse.Namespace) -> None:
    """With explicit target flags, bind to the live session on that target
    when it is not the current one: an agent's `--serial X` commands journal
    into the session a Canvas started on X. `session start` creates sessions
    and is never bound. Never fails: a broken store binds nothing."""
    if args.command == "session" and getattr(args, "session_command", None) == "start":
        return
    value = next((getattr(args, flag, None) for flag in ("target", "serial", "udid")
                  if getattr(args, flag, None)), None)
    if not value:
        return
    try:
        record = session_mod.live_for_target(str(value), getattr(args, "platform", None))
        current_id = session_mod._current_id(None)
    except Exception:  # noqa: BLE001 - binding is a convenience, never a failure
        return
    if record and record.get("session_id") != current_id:
        session_mod.select(record["session_id"])


def _select_session(args: argparse.Namespace) -> None:
    """Bind this invocation to a session. First match wins:

    1. `--session-id X` (X other than `current`): that session. X must
       exist; a stopped one is accepted only by the read-only verbs, and
       `session start` never takes one (it creates a session).
    2. Explicit target flags (any verb but `session start`): the live session
       on that target, when it is not the current one.
    3. Otherwise (and always with `--session-id current`) the machine's
       current session (`current.json`)."""
    # A new invocation: no earlier one's journal restriction carries over.
    session_mod.keep_journal_in(None)
    selected = getattr(args, "session_id", None)
    if not selected:
        _bind_to_target_session(args)
        return
    if selected == "current":
        return
    if args.command == "session" and getattr(args, "session_command", None) == "start":
        raise errors.AutonomError(
            errors.USAGE_ERROR, "session start creates a new session; omit --session-id",
            "Start the session without --session-id, then name it on later verbs.")
    record = session_mod.load_by_id(selected)
    session_mod.select(selected)
    readonly = (args.command in _READONLY_COMMANDS and not (
        args.command == "report"
        and getattr(args, "report_command", None) in _DRIVING_REPORT_COMMANDS)) or (
        args.command == "session"
        and getattr(args, "session_command", None) in _READONLY_SESSION_COMMANDS) or (
        args.command == "control" and getattr(args, "control_command", None) == "status")
    if record.get("stopped_at") and not readonly:
        raise errors.AutonomError(
            errors.SESSION_STOPPED, f"session {selected} has stopped",
            "Only read-only verbs (journal, shots, logs, report except report serve, "
            "session show/outputs) accept a stopped session; start a new session to "
            "drive the device.",
            session_id=selected)


def main(argv: list[str] | None = None) -> int:
    global _ROOT_PARSER, _LAST_EMIT, _OBSERVE_INTERRUPTED
    _LAST_EMIT = None
    _OBSERVE_INTERRUPTED = False
    parser = build_parser()
    _ROOT_PARSER = parser
    # The claim flags are not options of any parser: the journal resolves the
    # argv the parsers saw (the entry carries the operation id instead).
    argv_used = _strip_control_flags(list(argv) if argv is not None else sys.argv[1:])
    args = _parse(parser, argv)
    context_token = session_mod.select(None)
    before_session = None
    ok, error_code = True, None
    try:
        code = None
        try:
            platform_mod.apply_tool_overrides(args)
            _apply_ios_overrides(args)
            _select_session(args)
            # Control lease context (contract 5.4): claim, operation id and the
            # verb's class; MUTATING/FLOW verbs are admitted in `_target`.
            _begin_control(args)
            # Only `session stop` journals through this snapshot. A broken
            # current.json must not fail session-independent verbs (version,
            # processes, devices); verbs that need the session fail on their own.
            try:
                before_session = session_mod.load_current()
            except Exception:  # noqa: BLE001
                before_session = None
            from autonom_lib import ui_ios
            ui_ios.set_preference(getattr(args, "ui_backend", None))
            code = args.func(args)
            if code == 0 and _OBSERVE_INTERRUPTED:
                # The one place an interrupt during observe (AL-11) ends:
                # the input was sent once and its reply (ok: true,
                # observe_error interrupted) is on stdout; `code` stays 0 so
                # the finally below journals the action ok, and only then
                # the process ends with the interrupt's status. Nothing is
                # read or sent again.
                return EXIT_INTERRUPTED
            return code
        finally:
            # A handler that returns nonzero (doctor --strict, flow run's
            # test failures, fmt --check) did not succeed — journal it so.
            if code not in (0, None):
                ok = False
    except errors.AutonomError as exc:
        ok, error_code = False, exc.code
        return fail_error(exc)
    except BrokenPipeError:
        # The consumer closed the stream (`… follow | head`): that is a clean
        # end for NDJSON output, not an error. Point stdout at devnull so the
        # interpreter's shutdown flush cannot raise a second time.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    except subprocess.TimeoutExpired as exc:
        # Long device calls (dumpheap, simpleperf, a wedged adb) must fail as
        # one envelope, never a traceback.
        ok, error_code = False, errors.BACKEND_FAILED
        return fail_error(errors.AutonomError(
            errors.BACKEND_FAILED,
            f"backend timed out: {' '.join(map(str, exc.cmd or []))[:200]}",
            "The device or tool stopped responding; check 'autonom doctor' "
            "and retry.",
        ))
    except FileNotFoundError as exc:
        # used to be the one envelope without an error_code
        ok, error_code = False, errors.INVALID_VALUE
        where = exc.filename or ""
        return fail_error(errors.AutonomError(
            errors.INVALID_VALUE,
            f"no such file or directory: {where}" if where else str(exc),
            "Check the path argument; relative paths resolve against the "
            "current directory.",
            **({"path": str(where)} if where else {}),
        ))
    except OSError as exc:
        wrapped = _os_error(exc)
        ok, error_code = False, wrapped.code
        return fail_error(wrapped)
    except re.error as exc:
        # re.error is not a ValueError: a bad --grep used to be a traceback
        ok, error_code = False, errors.INVALID_VALUE
        return fail_error(errors.AutonomError(
            errors.INVALID_VALUE, f"invalid regular expression: {exc}",
            "The filter is a Python regular expression; escape literal "
            "characters such as ( [ * with a backslash.",
        ))
    except (IndexError, ValueError) as exc:
        # A bare ValueError used to leave the envelope without an error_code.
        ok, error_code = False, errors.INVALID_VALUE
        return fail_error(errors.AutonomError(
            errors.INVALID_VALUE, str(exc) or exc.__class__.__name__,
            "Check the argument values; run the verb with --help for the expected forms.",
        ))
    except KeyboardInterrupt:
        ok, error_code = False, errors.INTERRUPTED
        return fail_error(_interrupted_error(), code=EXIT_INTERRUPTED)
    finally:
        try:
            _journal_command(args, argv_used, ok, error_code, before_session)
        finally:
            control_mod.end_invocation()
            session_mod._SELECTED.reset(context_token)


if __name__ == "__main__":
    raise SystemExit(main())
