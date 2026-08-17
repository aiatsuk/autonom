#!/usr/bin/env python3
"""Autonom CLI — portable control plane for AI agents (Android + iOS Simulator)."""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonom_lib import __version__  # noqa: E402
from autonom_lib import actions as actions_mod  # noqa: E402
from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib import consent as consent_mod  # noqa: E402
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
from autonom_lib.flow import report as flow_report  # noqa: E402
from autonom_lib.flow import validator as flow_validator  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import ios_simctl  # noqa: E402
from autonom_lib.metrics import android_memory as metrics_memory  # noqa: E402
from autonom_lib.metrics import artifacts as metrics_artifacts  # noqa: E402
from autonom_lib.metrics import frames as metrics_frames  # noqa: E402
from autonom_lib.metrics import presets as metrics_presets  # noqa: E402
from autonom_lib.metrics import series as metrics_series  # noqa: E402
from autonom_lib.metrics import snapshot as metrics_snapshot  # noqa: E402
from autonom_lib.metrics import trace as metrics_trace  # noqa: E402
from autonom_lib import logs as logs_mod  # noqa: E402
from autonom_lib import platform as platform_mod  # noqa: E402
from autonom_lib import proof as proof_mod  # noqa: E402
from autonom_lib import processes as processes_mod  # noqa: E402
from autonom_lib import screenshot as shot_mod  # noqa: E402
from autonom_lib import selector as selector_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui as ui_mod  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402
from autonom_lib.network import device_proxy_android, har as har_mod  # noqa: E402
from autonom_lib.network import mocks as mocks_mod  # noqa: E402
from autonom_lib.network import proxy as proxy_mod, store as store_mod  # noqa: E402
from autonom_lib.network import redact as redact_mod  # noqa: E402
from autonom_lib.atlas import fingerprint as atlas_fingerprint  # noqa: E402
from autonom_lib.atlas import graph as atlas_graph  # noqa: E402


_LAST_EMIT: dict[str, Any] | None = None


def emit(data: Any, *, as_json: bool) -> int:
    global _LAST_EMIT
    _LAST_EMIT = data if isinstance(data, dict) else None
    if as_json or isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(data)
    return 0


def fail(message: str, code: int = 2) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return code


def fail_error(exc: errors.AutonomError, code: int = 2) -> int:
    print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
    return code


def _target(args: argparse.Namespace) -> Target:
    return platform_mod.resolve(args, session_record=session_mod.load_current())


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


def cmd_version(_: argparse.Namespace) -> int:
    return emit({"name": "autonom", "version": __version__}, as_json=True)


def cmd_devices(args: argparse.Namespace) -> int:
    devices, warnings = platform_mod.list_all(args, only=args.platform)
    payload: dict[str, Any] = {"ok": True, "devices": devices, "warnings": warnings}
    if args.platform in (None, ANDROID):
        # Bootable-but-not-running AVDs are part of the inventory too; a
        # missing emulator binary just means the key is absent.
        try:
            adb_path = adb_mod.find_adb(getattr(args, "adb", None))
            emulator_bin = emulator_mod.find_emulator(
                getattr(args, "emulator", None), adb_path=adb_path
            )
            payload["avds"] = emulator_mod.list_avds(emulator_bin)
        except errors.AutonomError:
            pass
    if warnings and not devices:
        payload["next_action"] = "run 'autonom doctor' to see what is missing"
    return emit(payload, as_json=True)


def cmd_devices_boot(args: argparse.Namespace) -> int:
    avd = getattr(args, "avd", None)
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
    if target.platform == IOS:
        ios_simctl.shutdown(target.tool, target.target_id)
        return emit({"ok": True, "stopped": True, **target.identity()}, as_json=True)
    detail = emulator_mod.kill_emulator(target.tool, target.target_id)
    return emit({"ok": True, "platform": ANDROID, **detail}, as_json=True)


# --- session -----------------------------------------------------------------


def cmd_session_start(args: argparse.Namespace) -> int:
    target = platform_mod.resolve(args)
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

    record = session_mod.start_session(
        target.tool,
        serial=target.serial,
        app_id=args.app_id,
        platform=target.platform,
        target_id=target.target_id,
        tooling=tooling,
    )

    warnings: list[dict[str, Any]] = []
    if target.platform == IOS and tooling.get("idb", {}).get("state") != "ready":
        warnings.append({
            "code": errors.IDB_REQUIRED,
            "error": "idb is not available; 'ui' verbs will fail",
            "hint": "simctl-backed verbs (screenshot, logs, open, permissions) still work.",
        })

    if args.install:
        install_path = Path(args.install)
        if target.platform == IOS:
            ios_simctl.install(target.tool, target.target_id, install_path)
        else:
            if not install_path.exists():
                raise errors.AutonomError(
                    errors.INSTALL_PATH_NOT_FOUND, f"package not found: {install_path}"
                )
            session_mod.install_app(target.tool, target.target_id, install_path)
        record["install_path"] = str(install_path.expanduser().resolve())

    if args.launch is not None:
        app_id = args.app_id or args.launch or None
        if not app_id:
            raise errors.AutonomError(
                errors.NO_TARGET,
                "--launch requires --app-id or an explicit package id",
                "Pass --app-id com.example.app, or --launch com.example.app.",
            )
        if target.platform == IOS:
            ios_simctl.launch(target.tool, target.target_id, app_id)
        else:
            session_mod.launch_app(target.tool, target.target_id, app_id, activity=args.activity)
        record["app_id"] = app_id

    if target.platform == IOS and getattr(args, "log_stream", False):
        stream = session_mod.artifact_path(record, "logs", "stream.ndjson")
        pid = logs_mod.start_log_stream(
            target, stream, bundle_id=record.get("app_id")
        )
        record["background"]["log_stream_pid"] = pid
        if pid:
            session_mod.register_stream(
                record, stream_id="log_stream", kind="device_log",
                path="logs/stream.ndjson", label="ios log stream", pid=pid)

    session_mod.save(record)
    payload: dict[str, Any] = {"ok": True, "session": record}
    if target.platform == IOS:
        payload["booted"] = booted
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_session_stop(_: argparse.Namespace) -> int:
    record = session_mod.load_current()
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION,
            "no active session",
            "Start one with 'autonom session start'.",
        )

    background = record.get("background") or {}

    def _detach() -> Any:
        if not (record.get("network") or {}).get("attached"):
            return {"was_attached": False}
        target = _target(argparse.Namespace())
        if target.platform == ANDROID:
            return device_proxy_android.detach(target, record)
        from autonom_lib.network import device_proxy_ios

        return device_proxy_ios.detach(target, record)

    # Order matters: restore the device's network before killing the proxy, or the
    # device is briefly pointed at a dead listener (INV-07, INV-10).
    actions: list[tuple[str, Any]] = [
        ("log_stream", lambda: session_mod.terminate_pid(background.get("log_stream_pid"))),
        ("recorder", lambda: session_mod.terminate_pid(background.get("recorder_pid"))),
        ("network_detach", _detach),
        ("network_stop", lambda: proxy_mod.stop(record)),
    ]
    teardown = session_mod.run_teardown(actions)

    stopped = session_mod.stop_session()
    payload = {"ok": True, "session": stopped, "teardown": teardown}
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
    if target.platform == IOS:
        env = dict(pair.split("=", 1) for pair in (args.setenv or []) if "=" in pair)
        # When the session is attached to a proxy, launching without the proxy
        # environment would silently produce an uncaptured run.
        from autonom_lib.network import device_proxy_ios as _ios_proxy

        current = session_mod.load_current()
        if current:
            for key, value in _ios_proxy.launch_environment(current).items():
                env.setdefault(key, value)
        pid = ios_simctl.launch(
            target.tool, target.target_id, args.app_id, args=args.arg or [], env=env
        )
        return emit({"ok": True, "launched": args.app_id, "pid": pid, **target.identity()}, as_json=True)
    session_mod.launch_app(target.tool, target.target_id, args.app_id, activity=args.activity)
    return emit({"ok": True, "launched": args.app_id, **target.identity()}, as_json=True)


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
        session_mod.clear_data(target.tool, target.target_id, args.app_id)
        return emit({"ok": True, "cleared": args.app_id, **target.identity()}, as_json=True)

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
        ios_simctl.uninstall(target.tool, target.target_id, args.app_id)
        ios_simctl.install(target.tool, target.target_id, Path(install_path))
        return emit(
            {"ok": True, "cleared": args.app_id, "strategy": "reinstall", **target.identity()},
            as_json=True,
        )
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
        ios_simctl.uninstall(target.tool, target.target_id, args.app_id)
    else:
        adb_mod.run_adb(target.tool, ["uninstall", args.app_id], serial=target.target_id, check=False)
    return emit({"ok": True, "uninstalled": args.app_id, **target.identity()}, as_json=True)


# --- ui ----------------------------------------------------------------------


def _snapshot(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str, str, Target | None]:
    """All on-screen nodes plus provenance: (nodes, source, target label, target)."""
    dump = getattr(args, "dump", None)
    if dump:
        text = Path(dump).read_text(encoding="utf-8")
        if text.lstrip()[:1] in "{[":
            from autonom_lib import ui_ios

            return ui_ios.parse_all(text), "file", dump, None
        from autonom_lib import ui_android

        return ui_android.parse_all(text), "file", dump, None
    target = _target(args)
    return ui_mod.snapshot(target), "device", target.target_id, target


def cmd_ui_tree(args: argparse.Namespace) -> int:
    dump = getattr(args, "dump", None)
    warnings: list[dict[str, Any]] = []
    if dump:
        text = Path(dump).read_text(encoding="utf-8")
        if text.lstrip()[:1] in "{[":
            from autonom_lib import ui_ios

            nodes, warnings = ui_ios.parse_tree(
                text, meaningful_only=not args.all, max_depth=args.max_depth,
                max_nodes=args.max_nodes,
            )
            identity = {"platform": IOS}
        else:
            nodes = ui_mod.parse_compact_tree(
                text, meaningful_only=not args.all, max_depth=args.max_depth,
                max_nodes=args.max_nodes,
            )
            identity = {"platform": ANDROID}
        source, label, target = "file", dump, None
    else:
        target = _target(args)
        nodes, warnings = ui_mod.tree(
            target, meaningful_only=not args.all, max_depth=args.max_depth, max_nodes=args.max_nodes
        )
        source, label = "device", target.target_id
        identity = target.identity()

    payload: dict[str, Any] = {
        "ok": True,
        "source": source,
        "target": label,
        "count": len(nodes),
        "nodes": nodes,
        **identity,
    }
    if warnings:
        payload["warnings"] = warnings
    current = session_mod.load_current()
    if current and not dump:
        blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        # Keep the full history, not just the latest: a run's trees are evidence
        # of how the screen changed. `latest.json` stays as a stable pointer.
        stamp = time.strftime("%H%M%S", time.gmtime())
        existing = sorted(session_mod.artifact_path(current, "trees").glob("[0-9]*.json"))
        seq = len(existing) + 1
        history = session_mod.artifact_path(current, "trees", f"{seq:04d}_{stamp}.json")
        history.write_text(blob, encoding="utf-8")
        session_mod.artifact_path(current, "trees", "latest.json").write_text(
            blob, encoding="utf-8"
        )
        payload["saved"] = str(history)
    return emit(payload, as_json=True)


def cmd_note_add(args: argparse.Namespace) -> int:
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
        argv = [target.tool, "-s", target.target_id, "logcat", "-v", "time"]
        if args.package:
            pid = logs_mod.pid_for_package(
                target.tool, target.target_id, args.package)
            if pid:
                argv.append(f"--pid={pid}")
        return follow_mod.follow_process(
            argv, source="device", emit=_stream_print,
            max_seconds=args.max_seconds, max_lines=args.max_lines,
            grep=args.grep)

    # iOS: a named past session can only replay its recorded stream file; the
    # current session's file is preferred only while its writer is alive —
    # a dead stream must not shadow live device logs.
    record = (_session_by_id(args.session_id) if args.session_id
              else session_mod.load_current())
    stream = (Path(record["artifacts_dir"]) / "logs" / "stream.ndjson"
              if record else None)
    package_filter = ((lambda line: args.package in line)
                      if args.package else None)
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
    else:
        pid = (record or {}).get("background", {}).get("log_stream_pid")
        writer_alive = session_mod.pid_alive(pid)
    if stream is not None and stream.exists() and writer_alive:
        return follow_mod.follow_file(
            stream, source="device", emit=_stream_print,
            from_start=args.from_start, max_seconds=args.max_seconds,
            max_lines=args.max_lines, grep=args.grep,
            poll_ms=args.poll_ms, line_filter=package_filter)
    argv = [target.tool, "simctl", "spawn", target.target_id,
            "log", "stream", "--style", "ndjson", "--level", "info"]
    if args.package:
        argv += ["--predicate", logs_mod._ios_predicate(args.package)]  # noqa: SLF001
    return follow_mod.follow_process(
        argv, source="device", emit=_stream_print,
        max_seconds=args.max_seconds, max_lines=args.max_lines, grep=args.grep)


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
    else:
        raise errors.AutonomError(
            errors.STREAM_NOT_FOUND,
            "nothing to follow: pass --source or --path",
            "List followable streams with 'autonom session outputs', or "
            "use --source device for the device log.",
        )
    return _finish_stream(follow_mod.follow_file(
        path, source=source, emit=_stream_print,
        from_start=args.from_start, max_seconds=args.max_seconds,
        max_lines=args.max_lines, grep=args.grep, poll_ms=args.poll_ms))


def cmd_journal(args: argparse.Namespace) -> int:
    session = session_mod.load_current()
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
    nodes, source, label, target = _snapshot(args)
    matches = selector_mod.select(
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
    if target:
        payload.update(target.identity())
        detail = actions_mod.record_detail(session_mod.load_current(), "find", {
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
    return emit(payload, as_json=True)


def cmd_ui_tap(args: argparse.Namespace) -> int:
    target = _target(args)
    ref = None
    node = None
    nodes: list[dict[str, Any]] = []
    if args.x is not None and args.y is not None:
        x, y = args.x, args.y
    else:
        nodes = ui_mod.snapshot(target)
        matches = selector_mod.select(
            nodes,
            _selectors(args),
            mode=args.mode,
            case_sensitive=args.case_sensitive,
            index=args.index,
        )
        if not matches:
            raise errors.AutonomError(
                errors.NO_MATCHING_NODE,
                "no matching node to tap",
                "Run 'autonom ui tree' to see what is on screen.",
            )
        node = matches[0]
        x, y = ui_mod.center_of(node)
        ref = node.get("ref")
    duration = getattr(args, "duration", None)
    if duration:
        ui_mod.long_press(target, x, y, duration)
    else:
        ui_mod.tap(target, x, y)
    detail = actions_mod.record_detail(session_mod.load_current(), "tap", {
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
    if duration:
        payload["duration_ms"] = duration
    if detail:
        payload["detail"] = detail
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_ui_swipe(args: argparse.Namespace) -> int:
    target = _target(args)
    x1, y1 = (int(part) for part in args.start.split(","))
    x2, y2 = (int(part) for part in args.end.split(","))
    ui_mod.swipe(target, x1, y1, x2, y2, args.duration)
    return emit(
        {"ok": True, "from": [x1, y1], "to": [x2, y2], "duration": args.duration, **target.identity()},
        as_json=True,
    )


def cmd_ui_gesture(args: argparse.Namespace) -> int:
    target = _target(args)
    kwargs: dict[str, Any] = {}
    if args.gesture == "pinch":
        if args.at is None:
            raise errors.AutonomError(
                errors.INVALID_COORDINATES, "pinch requires --at X,Y", "Example: --at 200,400"
            )
        x, y = (int(part) for part in args.at.split(","))
        kwargs = {"x": x, "y": y, "scale": args.scale}
    ui_mod.gesture(target, args.gesture, **kwargs)
    return emit({"ok": True, "gesture": args.gesture, **target.identity()}, as_json=True)


def cmd_ui_type(args: argparse.Namespace) -> int:
    target = _target(args)
    ui_mod.type_text(target, args.text)
    sensitive = bool(getattr(args, "sensitive", False))
    detail = actions_mod.record_detail(session_mod.load_current(), "type", {
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
    if detail:
        payload["detail"] = detail
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_ui_key(args: argparse.Namespace) -> int:
    target = _target(args)
    ui_mod.press_key(target, args.keycode)
    return emit({"ok": True, "keycode": args.keycode, **target.identity()}, as_json=True)


# --- evidence ----------------------------------------------------------------


def cmd_screenshot(args: argparse.Namespace) -> int:
    target = _target(args)
    detail = shot_mod.capture_evidence(
        target,
        session_mod.load_current(),
        label=getattr(args, "label", None),
        task=getattr(args, "task", None),
        out=Path(args.out) if args.out else None,
    )
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_shots_list(args: argparse.Namespace) -> int:
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
    limit = getattr(args, "max", 50) or 50
    return emit({"ok": True, "count": min(total, limit), "total_matched": total,
                 "truncated": total > limit, "shots": entries[-limit:],
                 "index": str(shot_mod.index_path(record))}, as_json=True)


def cmd_shots_show(args: argparse.Namespace) -> int:
    """Read the metadata back out of the PNG itself."""
    path = Path(args.path).expanduser()
    if not path.exists():
        raise errors.AutonomError(
            errors.BODY_FILE_NOT_FOUND, f"no such file: {path}",
            "Pass a path from 'autonom shots list'.",
        )
    return emit({"ok": True, "path": str(path),
                 "metadata": shot_mod.read_metadata(path)}, as_json=True)


def cmd_logs_tail(args: argparse.Namespace) -> int:
    target = _target(args)
    current = session_mod.load_current()
    stream_path = None
    if current and target.platform == IOS:
        candidate = Path(current["artifacts_dir"]) / "logs" / "stream.ndjson"
        stream_path = candidate if candidate.exists() else None
    entries, warnings = logs_mod.tail(
        target,
        stream_path=stream_path,
        package=args.package,
        since_seconds=args.since,
        max_lines=args.max_lines,
        grep=args.grep,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "count": len(entries),
        "lines": entries,
        **target.identity(),
    }
    if warnings:
        payload["warnings"] = warnings
    if current:
        out = session_mod.artifact_path(current, "logs", "latest.json")
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        payload["saved"] = str(out)
    return emit(payload, as_json=True)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Always exits 0 unless --strict: a diagnostic that fails is useless in a pipe."""
    report = doctor_mod.collect(args)
    healthy = doctor_mod.is_healthy(report)
    emit(report, as_json=True)
    return 0 if (healthy or not args.strict) else 1


# --- metrics -----------------------------------------------------------------


def _safe_label(label: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", label or "").strip("_") or "capture"


def _snapshot_base_dir(out: str | None) -> Path | None:
    """Artifact destination for snapshots: --out (dir, or a .json file's
    parent), else the session metrics/ dir, else None — no destination is
    allowed, the stdout payload still carries every metric."""
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
    target = _target(args)
    _require_android(target, "the memory pack reads Android dumpsys",
                     "On iOS use 'metrics snapshot' and "
                     "'metrics trace --preset allocations'.")
    payload = metrics_memory.capture(
        target, _app_id(args), out_dir=_metrics_out_dir(args),
        label=_safe_label(args.label), want_hprof=not args.no_hprof)
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_metrics_memory_analyze(args: argparse.Namespace) -> int:
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


_FRAMES_ANDROID_ONLY = ("gfxinfo frame stats are Android-only",
                        "On iOS use 'metrics trace --preset hitches'.")


def cmd_metrics_frames_reset(args: argparse.Namespace) -> int:
    target = _target(args)
    _require_android(target, *_FRAMES_ANDROID_ONLY)
    app_id = _app_id(args)
    metrics_frames.reset(target, app_id)
    return emit({"ok": True, "reset": app_id, **target.identity()}, as_json=True)


def cmd_metrics_frames_capture(args: argparse.Namespace) -> int:
    target = _target(args)
    _require_android(target, *_FRAMES_ANDROID_ONLY)
    app_id = _app_id(args)
    raw, summary = metrics_frames.capture(target, app_id)
    payload: dict[str, Any] = {"ok": True, "summary": summary,
                               **target.identity()}
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
    target = _target(args)
    payload = metrics_trace.run_preset(
        target, _app_id(args), args.preset, duration=max(args.duration, 1.0),
        out_dir=_metrics_out_dir(args), label=_safe_label(args.label))
    return emit({**payload, **target.identity()}, as_json=True)


def cmd_metrics_list_presets(args: argparse.Namespace) -> int:
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


def cmd_open(args: argparse.Namespace) -> int:
    target = _target(args)
    device_state.open_url(target, args.url)
    return emit({"ok": True, "opened": args.url, **target.identity()}, as_json=True)


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
    if args.location_command == "get":
        detail = device_state.get_location(target)
        return emit({"ok": True, **detail, **target.identity()}, as_json=True)
    detail = device_state.set_location(target, args.coordinates)
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
    target = _target(args)
    if args.out:
        destination = Path(args.out)
    else:
        current = session_mod.require_current()
        destination = session_mod.artifact_path(
            current, "files", *device_state.safe_relative(args.remote).split("/")
        )
    detail = device_state.file_pull(target, _app_id(args), args.remote, destination)
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_record_start(args: argparse.Namespace) -> int:
    target = _target(args)
    record = session_mod.require_current()
    if session_mod.pid_alive((record.get("background") or {}).get("recorder_pid")):
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
    destination = Path(background.get("recorder_path") or
                       session_mod.artifact_path(record, "recordings", "latest.mp4"))
    detail = device_state.record_stop(target, background.get("recorder_pid"), destination)
    background["recorder_pid"] = None
    background["recorder_path"] = None
    session_mod.save(record)
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


# --- network -----------------------------------------------------------------


def cmd_network_start(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    entry = consent_mod.require(
        consent_mod.Operation(
            kind="mitm_proxy",
            target=f"127.0.0.1:{args.port or 'auto'}",
            effect=("start a man-in-the-middle proxy that decrypts and records this "
                    "session's HTTP(S) traffic to disk"),
            flags=("--i-understand-mitm",),
        ),
        acknowledged=args.i_understand_mitm,
    )
    state = proxy_mod.start(
        record, port=args.port, capture_bodies=args.capture_bodies,
        mitmdump=getattr(args, "mitmdump", None),
        ignore_hosts=getattr(args, "ignore_hosts", None),
        intercept_connectivity_checks=getattr(args, "intercept_connectivity_checks", False),
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

    warnings: list[dict[str, str]] = []
    if args.capture_bodies:
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
    if mocks_state["active"]:
        warnings.append({
            "code": "persistent_mocks_active",
            "error": f"{mocks_state['active']} mock rule(s) loaded from the persistent "
                     f"registry — matching responses WILL be faked",
            "hint": "Review with 'autonom network mock list', switch off with "
                    "'autonom network mock disable --all'.",
        })
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Reap Autonom's leftover processes, from anywhere on the machine."""
    detail = processes_mod.cleanup(dry_run=args.dry_run, include_live=args.all)
    payload: dict[str, Any] = {"ok": True, **detail}
    if detail["failed"]:
        payload["warnings"] = [{
            "code": "termination_failed",
            "error": f"{detail['failed']} process(es) would not terminate",
            "hint": "They may belong to another user; inspect them with "
                    "'ps -p <pid>' before escalating.",
        }]
    return emit(payload, as_json=True)


def cmd_processes(_: argparse.Namespace) -> int:
    """Read-only view of every process Autonom started on this machine."""
    return emit({"ok": True, **processes_mod.scan()}, as_json=True)


def cmd_network_stop(_: argparse.Namespace) -> int:
    record = session_mod.require_current()
    detail = proxy_mod.stop(record)
    record.setdefault("network", {})["enabled"] = False
    session_mod.save(record)
    return emit({"ok": True, **detail}, as_json=True)


def cmd_network_status(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    state = proxy_mod.status(record)
    network = record.get("network") or {}
    payload: dict[str, Any] = {"ok": True, "proxy": state}

    attached: Any = False
    evidence = "not_attached"
    if network.get("attached"):
        flows, _warnings = store_mod.read_all(record)
        recent = store_mod.filter_flows(flows, since_seconds=60)
        if recent:
            attached, evidence = True, "recent_flows"
        elif network.get("platform_manual"):
            attached, evidence = "unknown", "manual_attach_unverified"
        else:
            try:
                target = _target(args)
                observed = (device_proxy_android.observed_setting(target)
                            if target.platform == ANDROID else None)
            except errors.AutonomError:
                observed = None
            if observed and observed == network.get("device_proxy"):
                attached, evidence = True, "device_setting"
            elif observed is None and target_platform_is_android(record):
                attached, evidence = False, "device_proxy_cleared_externally"
            else:
                attached, evidence = "unknown", "no_traffic_and_no_readable_setting"

    payload["attached"] = attached
    payload["evidence"] = evidence
    payload["recent_flow_count"] = len(
        store_mod.filter_flows(store_mod.read_all(record)[0], since_seconds=60)
    )
    mocks_state = mocks_mod.summary()
    idle = _annotate_hits(mocks_mod.active())
    payload["mocks"] = mocks_state
    if idle:
        payload.setdefault("warnings", []).extend(idle)
    if attached == "unknown":
        payload["next_action"] = "exercise the app, then re-run 'autonom network status'"
    return emit(payload, as_json=True)


def target_platform_is_android(record: dict[str, Any]) -> bool:
    return (record.get("platform") or ANDROID) == ANDROID


def cmd_network_attach(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    state = proxy_mod.status(record)
    if not state["running"]:
        raise errors.AutonomError(
            errors.PROXY_NOT_RUNNING,
            "no proxy is running for this session",
            "Start one first: 'autonom network start --i-understand-mitm'.",
        )
    target = _target(args)
    if target.platform == ANDROID:
        ca_detail = None
        if args.install_ca:
            ca_detail = device_proxy_android.install_ca_certificate(
                target, record, acknowledged=args.i_understand_mitm
            )
        detail = device_proxy_android.attach(
            target, record, port=state["port"], acknowledged=args.i_understand_mitm,
            network_cycle=not getattr(args, "no_network_cycle", False),
        )
        if ca_detail:
            detail["ca_installed"] = ca_detail
        session_mod.save(record)
        return emit({"ok": True, "mode": "automated", **detail, **target.identity()},
                    as_json=True)

    from autonom_lib.network import device_proxy_ios

    detail = device_proxy_ios.attach(
        target, record, port=state["port"],
        acknowledged=args.i_understand_mitm, install_ca=args.install_ca,
    )
    session_mod.save(record)
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_network_detach(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    target = _target(args)
    if target.platform == ANDROID:
        detail = device_proxy_android.detach(target, record)
    else:
        from autonom_lib.network import device_proxy_ios

        detail = device_proxy_ios.detach(target, record)
    session_mod.save(record)
    return emit({"ok": True, **detail, **target.identity()}, as_json=True)


def cmd_network_requests_list(args: argparse.Namespace) -> int:
    record = session_mod.require_current()
    payload = store_mod.listing(
        record,
        max_items=args.max,
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
    record = session_mod.require_current()
    filters = {"host": args.host, "method": args.method, "status": args.status,
               "path_glob": args.path, "mocked": args.mocked}
    flows_file = store_mod.flows_path(record)
    seen: set[str] = set()
    # The store is append-only JSONL (its own contract), so the follow tails
    # by byte offset and parses only what was appended — never the whole
    # history again on every tick. A shrink means the store was replaced:
    # start over from byte 0; `seen` keeps replayed flows from re-emitting.
    state = {"offset": 0, "buffer": b""}

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
                parsed.append(redact_mod.scrub_flow(json.loads(line)))
            except json.JSONDecodeError:
                continue  # a torn write; the next append completes nothing
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

    return _finish_stream(follow_mod.follow_poll(
        fetch_new, emit=_stream_print, interval=args.interval,
        max_seconds=args.max_seconds, max_items=args.max))


def cmd_network_requests_show(args: argparse.Namespace) -> int:
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
    record = session_mod.require_current()
    flows, warnings = store_mod.read_all(record)
    state = proxy_mod.status(record)
    document = har_mod.build(flows, bodies_captured=bool(state.get("capture_bodies")))
    destination = Path(args.har)
    if not destination.is_absolute():
        destination = Path(record["artifacts_dir"]) / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    payload: dict[str, Any] = {"ok": True, "path": str(destination),
                               "entries": len(document["log"]["entries"])}
    if warnings:
        payload["warnings"] = warnings
    return emit(payload, as_json=True)


def _mock_headers(args: argparse.Namespace) -> dict[str, str] | None:
    """`--header 'Name: value'`, repeatable. None when the flag was not used."""
    if not getattr(args, "header", None):
        return None
    return dict(
        (part.split(":", 1)[0].strip(), part.split(":", 1)[1].strip())
        for part in args.header if ":" in part
    )


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
    counts = _mock_hit_counts()
    for rule in rules:
        rule["hits"] = counts.get(rule.get("id"), 0)
    idle = [r["id"] for r in rules if r.get("enabled", True) and not r["hits"]]
    if idle and counts:
        # Only worth saying once some rule HAS fired: otherwise the proxy simply
        # has not seen traffic yet, which is a different problem.
        return [{
            "code": "mock_never_matched",
            "error": "enabled rule(s) that have not matched a single request: "
                     + ", ".join(idle),
            "hint": "Check the glob against a real URL — 'network requests list' "
                    "shows the full URL, and a query string defeats a glob that "
                    "ends at the path.",
        }]
    return []


def cmd_network_mock_add(args: argparse.Namespace) -> int:
    selector = _mock_selector(args)
    if not selector.get("url_glob"):
        raise errors.AutonomError(
            errors.MOCK_NOT_FOUND,
            "no target given",
            "Pass --url <full URL> or --match <glob>.",
        )
    body_file, body_text = _mock_body(args)
    default_headers = {}
    if body_text is not None and body_text.lstrip()[:1] in "{[":
        default_headers = {"Content-Type": "application/json"}
    rule = mocks_mod.add(
        url_glob=selector["url_glob"],
        method=selector.get("method"),
        host=selector.get("host"),
        ignore_query=selector.get("ignore_query", False),
        status=args.status if args.status is not None else 200,
        headers=_mock_headers(args) or default_headers,
        body_file=body_file,
        body_text=body_text,
        note=getattr(args, "note", None),
    )
    return emit({"ok": True, "mock": rule, "registry": str(mocks_mod.registry_file())},
                as_json=True)


def cmd_network_mock_list(args: argparse.Namespace) -> int:
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
    )
    return emit({"ok": True, "mock": rule}, as_json=True)


def cmd_network_mock_enable(args: argparse.Namespace) -> int:
    detail = mocks_mod.set_enabled(args.id, True, all_rules=args.all)
    return emit({"ok": True, **detail}, as_json=True)


def cmd_network_mock_disable(args: argparse.Namespace) -> int:
    detail = mocks_mod.set_enabled(args.id, False, all_rules=args.all)
    return emit({"ok": True, **detail}, as_json=True)


def cmd_network_mock_remove(args: argparse.Namespace) -> int:
    return emit({"ok": True, **mocks_mod.remove(args.id)}, as_json=True)


def cmd_network_mock_clear(_: argparse.Namespace) -> int:
    return emit({"ok": True, **mocks_mod.clear()}, as_json=True)


# --- flow --------------------------------------------------------------------


def _flow_summary(flow) -> dict[str, Any]:
    return {
        "file": flow.path,
        "id": flow.flow_id,
        "name": flow.name,
        "tags": flow.tags,
        "platforms": flow.requires_platforms or ["android", "ios"],
    }


def _flow_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = flow_validator.discover(path)
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
    files = _flow_files(path)
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
        )
    return emit({"ok": True, "checked": len(files), "flows": flows}, as_json=True)


def cmd_flow_fmt(args: argparse.Namespace) -> int:
    path = Path(args.path)
    files = _flow_files(path)
    results: list[dict[str, Any]] = []
    changed_any = False
    for file in files:
        flow = flow_validator.load_flow(file)
        canonical_text = flow_canonical.emit_flow(flow)
        original = file.read_text(encoding="utf-8")
        changed = canonical_text != original
        entry: dict[str, Any] = {"file": str(file), "changed": changed}
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
    files = _flow_files(base)
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
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
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
        Path(args.out).write_text(canonical_text, encoding="utf-8")
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
        Path(args.out).write_text(exported, encoding="utf-8")
        payload["out"] = args.out
    else:
        payload["maestro"] = exported
    return emit(payload, as_json=True)


def _flow_env_overrides(args: argparse.Namespace) -> dict[str, str]:
    """One owner of --env parsing for every verb that runs flows."""
    env_overrides: dict[str, str] = {}
    for pair in args.env or []:
        if "=" not in pair:
            raise errors.AutonomError(
                errors.FLOW_COMMAND_INVALID, f"--env takes KEY=VALUE, got {pair!r}")
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


def cmd_flow_run(args: argparse.Namespace) -> int:
    path = Path(args.path)
    include = args.include_tag or []
    exclude = args.exclude_tag or []
    if path.is_dir():
        candidates = _flow_files(path)
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

    flows = [flow_validator.validate_tree(file) for file in files]

    record = session_mod.load_current()
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION,
            "flow run needs an active session",
            "Start one with 'autonom session start'.",
        )
    target = _target(args)

    env_overrides = _flow_env_overrides(args)
    secrets = _flow_secrets(args)

    config = flow_executor.RunConfig(
        env=env_overrides, secrets=secrets,
        default_timeout_ms=args.default_timeout_ms, dry_run=args.dry_run,
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
        if flow.converted_from:
            summary["converted_from"] = flow.converted_from
        if result.failure:
            summary["failure"] = result.failure
        if result.hook_failures:
            summary["hook_failures"] = result.hook_failures
        return summary

    if len(flows) == 1:
        summary = {"ok": True, **run_one(flows[0]), **target.identity()}
        exit_code = 0 if summary["status"] == "passed" else 1
    else:
        runs = []
        for flow in flows:  # a test failure moves on; an AutonomError aborts
            runs.append(run_one(flow))
        overall = "passed" if all(r["status"] == "passed" for r in runs) else "failed"
        summary = {"ok": True, "status": overall,
                   "flows": len(runs),
                   "failed": sum(1 for r in runs if r["status"] != "passed"),
                   "runs": runs, **target.identity()}
        exit_code = 0 if overall == "passed" else 1
    if not args.events:
        emit(summary, as_json=True)
    else:
        global _LAST_EMIT
        _LAST_EMIT = summary  # the journal summary stays useful in events mode
    return exit_code


# --- proof -------------------------------------------------------------------


def cmd_proof(args: argparse.Namespace) -> int:
    repo = Path(args.repo or ".").resolve()
    # A malformed --env is the caller's CLI mistake, exactly as in flow run —
    # a hard refusal, never a silently dropped override or a blocked verdict.
    env_overrides = _flow_env_overrides(args)
    changed = proof_mod.changed_files(repo, args.base, args.head)
    flows_dir = Path(args.flows) if args.flows else repo / ".autonom/flows"
    selected: list[dict[str, Any]] = []
    covered: list[str] = []
    if flows_dir.is_dir():
        selected, covered = proof_mod.select_flows(flows_dir, repo, changed)
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

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "proof.json").write_text(
            json.dumps(result_payload, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (out_dir / "proof.md").write_text(
            proof_mod.render_markdown(result_payload), encoding="utf-8")
        result_payload["out"] = str(out_dir)

    emit(result_payload, as_json=True)
    if status == "pass":
        return 0
    if status in ("fail", "not_covered"):
        return 1  # a CI gate must go red; not_covered is never upgraded
    return 2  # blocked / inconclusive: verification itself did not happen


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
    app_id = _atlas_app_id(args, session_mod.load_current())
    return emit({"ok": True,
                 **atlas_graph.summary(atlas_graph.load(app_id))}, as_json=True)


def cmd_atlas_coverage(args: argparse.Namespace) -> int:
    app_id = _atlas_app_id(args, session_mod.load_current())
    return emit({"ok": True, "app_id": app_id,
                 **atlas_graph.coverage(atlas_graph.load(app_id))}, as_json=True)


def cmd_atlas_paths(args: argparse.Namespace) -> int:
    app_id = _atlas_app_id(args, session_mod.load_current())
    graph = atlas_graph.load(app_id)
    return emit({"ok": True, "app_id": app_id,
                 **atlas_graph.paths(graph, getattr(args, "from"), args.to)},
                as_json=True)


def cmd_atlas_export(args: argparse.Namespace) -> int:
    app_id = _atlas_app_id(args, session_mod.load_current())
    graph = atlas_graph.load(app_id)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return emit({"ok": True, "app_id": app_id, "out": str(out),
                 "screens": len(graph.get("screens", {})),
                 "transitions": len(graph.get("transitions", {}))}, as_json=True)


def cmd_atlas_diff(args: argparse.Namespace) -> int:
    def load_snapshot(path_text: str) -> dict[str, Any]:
        path = Path(path_text)
        if not path.is_file():
            raise errors.AutonomError(
                errors.FLOW_FILE_NOT_FOUND, f"no atlas snapshot at {path}",
                hint="Create one with 'autonom atlas export --out <file>'.",
                file=str(path),
            )
        return json.loads(path.read_text(encoding="utf-8"))

    base = load_snapshot(args.base)
    if args.head:
        head = load_snapshot(args.head)
    else:
        head = atlas_graph.load(_atlas_app_id(args, session_mod.load_current()))
    return emit({"ok": True, **atlas_graph.diff(base, head)}, as_json=True)


# --- report ------------------------------------------------------------------


def _resolve_run_dir(record: dict[str, Any], run_id: str | None) -> Path:
    flows_dir = Path(record["artifacts_dir"]) / "flows"
    if run_id:
        run_dir = flows_dir / run_id
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


def cmd_report_suite(args: argparse.Namespace) -> int:
    """One page for the whole session — the suite view CI and humans read."""
    record = _session_by_id(args.session)
    manifests = _suite_manifests(record, args.last)
    out_dir = Path(args.out) if args.out else Path(record["artifacts_dir"]) / "flows"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(args.relative_to).resolve() if args.relative_to else None
    html_path = out_dir / "suite.html"
    html_path.write_text(flow_report.render_suite_html(manifests, base=base),
                         encoding="utf-8")
    os.chmod(html_path, 0o600)
    junit_path = out_dir / "suite.xml"
    junit_path.write_text(flow_report.render_suite_junit(manifests),
                          encoding="utf-8")
    os.chmod(junit_path, 0o600)
    failed = [m for m in manifests if m.get("status") != "passed"]
    payload = {
        "ok": True,
        "flows": len(manifests),
        "passed": len(manifests) - len(failed),
        "failed": len(failed),
        "html": str(html_path),
        "junit": str(junit_path),
        "sensitive": any(m.get("sensitive") for m in manifests),
    }
    if failed:
        payload["failures"] = [
            {"flow": m.get("flow_name"), "flow_id": m.get("flow_id"),
             "error_code": (m.get("primary_error") or {}).get("error_code")}
            for m in failed
        ]
    if args.open:
        import webbrowser
        payload["opened"] = bool(webbrowser.open(html_path.as_uri()))
    emit(payload, as_json=True)
    return 1 if failed else 0


def cmd_report_build(args: argparse.Namespace) -> int:
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
    return emit({
        "ok": True,
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "html": str(html_path),
        "junit": str(junit_path),
        "sensitive": manifest.get("sensitive", False),
    }, as_json=True)


def cmd_report_open(args: argparse.Namespace) -> int:
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
    record = _session_by_id(args.session)
    run_dir = _resolve_run_dir(record, args.run)
    manifest = flow_report.load_manifest(run_dir)
    if args.format == "junit":
        rendered = flow_report.render_junit(manifest)
        default_name = "report.xml"
    else:
        rendered = flow_report.render_html(manifest,
                                           Path(record["artifacts_dir"]))
        default_name = "report.html"
    out = Path(args.out) if args.out else run_dir / default_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    os.chmod(out, 0o600)
    return emit({"ok": True, "format": args.format, "out": str(out),
                 "sensitive": manifest.get("sensitive", False)}, as_json=True)


# --- parser ------------------------------------------------------------------


def target_flags_parent() -> argparse.ArgumentParser:
    """Target flags repeated on every leaf subcommand.

    `autonom --platform ios ui tree` and `autonom ui tree --platform ios` must
    both work — the plan's own examples use each form. `SUPPRESS` defaults are
    what make that safe: without them the subparser would overwrite a value the
    top-level parser already set with its own `None`.
    """
    parent = argparse.ArgumentParser(add_help=False)
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


def _bool_flag(value: str) -> bool:
    return value.lower() in {"1", "true", "yes"}


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonom",
        description="Universal mobile test/debug control plane for AI agents (Android + iOS).",
    )
    parser.add_argument("--platform", choices=("android", "ios"), help="target platform")
    parser.add_argument("--target", help="target id (adb serial or simulator udid)")
    parser.add_argument("--serial", help="adb serial / emulator id (Android alias of --target)")
    parser.add_argument("--udid", help="simulator udid (iOS alias of --target)")
    parser.add_argument("--adb", help="path to adb binary")
    parser.add_argument("--simctl", help="path to xcrun binary")
    parser.add_argument("--idb", help="path to idb binary")
    parser.add_argument("--idb-host", help="remote idb companion host")
    parser.add_argument("--idb-port", type=int, help="remote idb companion port")
    sub = parser.add_subparsers(dest="command", required=True)
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
    p.add_argument("--timeout", type=float, default=180.0,
                   help="seconds to wait for the boot to complete")
    p.add_argument("--no-wait", action="store_true",
                   help="return right after spawning instead of waiting for boot (Android)")
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
    p.set_defaults(func=cmd_session_start)

    p = session_sub.add_parser("stop", help="stop current session metadata", parents=[target_flags])
    p.set_defaults(func=cmd_session_stop)

    p = session_sub.add_parser("outputs", help="catalog followable session streams",
                               parents=[target_flags])
    p.add_argument("--session-id", help="a past session instead of the current one")
    p.set_defaults(func=cmd_session_outputs)
    p = session_sub.add_parser("show", help="show current session", parents=[target_flags])
    p.set_defaults(func=cmd_session_show)

    p = session_sub.add_parser("launch", help="launch an app", parents=[target_flags])
    p.add_argument("app_id")
    p.add_argument("--activity")
    p.add_argument("--arg", action="append", help="launch argument (repeatable, iOS)")
    p.add_argument("--setenv", action="append", help="KEY=VALUE child environment (iOS)")
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
    p.set_defaults(func=cmd_ui_tree)

    p = ui_sub.add_parser("find", help="find nodes by selector", parents=[target_flags])
    p.add_argument("--dump", help="parse an offline UI dump")
    _add_selector_flags(p)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_ui_find)

    p = ui_sub.add_parser("tap", help="tap by selector or coordinates", parents=[target_flags])
    _add_selector_flags(p)
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    p.add_argument("--duration", type=int, metavar="MS",
                   help="hold for MS milliseconds (a long press)")
    p.set_defaults(func=cmd_ui_tap)

    p = ui_sub.add_parser("swipe", help="swipe between two points", parents=[target_flags])
    p.add_argument("--from", dest="start", required=True, metavar="X,Y")
    p.add_argument("--to", dest="end", required=True, metavar="X,Y")
    p.add_argument("--duration", type=float, default=0.3)
    p.set_defaults(func=cmd_ui_swipe)

    # Only pinch takes an anchor and a scale; `idb ui rotate|shake` accept
    # neither, and offering the flags there advertised an argument that was
    # parsed, ignored, and never reached the device.
    for name in ("pinch", "rotate", "shake"):
        p = ui_sub.add_parser(name, help=f"{name} gesture (iOS)", parents=[target_flags])
        if name == "pinch":
            p.add_argument("--at", metavar="X,Y", help="anchor point")
            p.add_argument("--scale", type=float, default=2.0)
        p.set_defaults(func=cmd_ui_gesture, gesture=name)

    p = ui_sub.add_parser("type", help="type text into the focused field", parents=[target_flags])
    p.add_argument("text")
    p.add_argument("--sensitive", action="store_true",
                   help="a credential: keep only its length in every artifact")
    p.set_defaults(func=cmd_ui_type)

    p = ui_sub.add_parser("key", help="send a key or hardware button", parents=[target_flags])
    p.add_argument("keycode", help="Android: KEYCODE_BACK; iOS: HOME, LOCK, SIRI, SIDE_BUTTON")
    p.set_defaults(func=cmd_ui_key)

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
    p.add_argument("--session-id", help="with --follow: a past session's journal")
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

    p = sub.add_parser("doctor", help="report toolchain, capabilities, session, and orphans",
                       parents=[target_flags])
    p.add_argument("--mitmdump", help="path to mitmdump binary")
    p.add_argument("--strict", action="store_true", help="exit 1 when any tool is missing")
    p.set_defaults(func=cmd_doctor)

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

    atlas = sub.add_parser("atlas", help="the observed application graph")
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
            p.add_argument("--format", choices=("html", "junit"), default="html")
            p.add_argument("--out", help="destination path")
        p.set_defaults(func=func)

    p = report_sub.add_parser(
        "suite", help="one report over every flow run in the session")
    p.add_argument("--session", default="current",
                   help="session id (default: current)")
    p.add_argument("--last", type=int,
                   help="only the N most recent runs (default: all)")
    p.add_argument("--out", help="destination directory")
    p.add_argument("--relative-to", metavar="DIR",
                   help="strip this directory from paths (share a report "
                        "without leaking local paths)")
    p.add_argument("--open", action="store_true", help="open it in a browser")
    p.set_defaults(func=cmd_report_suite)

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
    p.add_argument("--mocked", type=lambda v: v.lower() in {"1", "true", "yes"}, default=None)
    p.add_argument("--max", type=int, default=store_mod.DEFAULT_MAX)
    p.add_argument("--since-id", help="only flows recorded after this id")
    p.set_defaults(func=cmd_network_requests_list)
    p = requests_sub.add_parser("follow", parents=[target_flags],
                                help="poll the store, stream new flows as NDJSON")
    p.add_argument("--host")
    p.add_argument("--method")
    p.add_argument("--status", type=int)
    p.add_argument("--path", help="glob over path or url")
    p.add_argument("--mocked", type=lambda v: v.lower() in {"1", "true", "yes"}, default=None)
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
        parser.add_argument("--body-file")
        parser.add_argument("--note", help="free-text label, shown in list")
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
    p.add_argument("--source", help="'device', a stream id, or output:<name> / "
                                    "logs:<name> / network:<name>")
    p.add_argument("--path", help="file to tail, relative to the session artifacts dir")
    p.add_argument("--session-id", help="a past session instead of the current one")
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

    return parser


# Verbs whose own handler writes the journal, or that only read it — never
# journaled by the choke point, to avoid double entries and read-noise.
_JOURNAL_SKIP = {"note", "journal", "version"}

# Sub-command dests, in priority order, for reconstructing a verb string.
_SUBCOMMAND_DESTS = (
    "session_command", "ui_command", "flow_command", "atlas_command",
    "report_command", "network_command", "requests_command", "mock_command",
    "location_command", "media_command", "crash_command", "file_command",
    "record_command", "shots_command", "devices_command", "note_command",
    "logs_command", "metrics_command",
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
                     error_code: str | None) -> None:
    command = getattr(args, "command", None)
    if command in _JOURNAL_SKIP:
        return
    try:
        session = session_mod.load_current()
    except Exception:  # noqa: BLE001
        session = None
    if not session:
        return
    journal_mod.record_action(
        session, verb=_verb_string(args), argv=argv,
        payload=_LAST_EMIT, ok=ok, error_code=error_code,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv_used = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(argv)
    ok, error_code = True, None
    try:
        code = None
        try:
            platform_mod.apply_tool_overrides(args)
            code = args.func(args)
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
        ok = False
        return fail(str(exc))
    except (IndexError, ValueError) as exc:
        ok = False
        return fail(str(exc))
    except KeyboardInterrupt:
        ok = False
        return fail("interrupted", code=130)
    finally:
        _journal_command(args, argv_used, ok, error_code)


if __name__ == "__main__":
    raise SystemExit(main())
