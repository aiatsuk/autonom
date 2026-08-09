#!/usr/bin/env python3
"""Autonom CLI — portable control plane for AI agents (Android + iOS Simulator)."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autonom_lib import __version__  # noqa: E402
from autonom_lib import adb as adb_mod  # noqa: E402
from autonom_lib import consent as consent_mod  # noqa: E402
from autonom_lib import device_state  # noqa: E402
from autonom_lib import doctor as doctor_mod  # noqa: E402
from autonom_lib import emulator as emulator_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import ios_idb  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import ios_simctl  # noqa: E402
from autonom_lib import logs as logs_mod  # noqa: E402
from autonom_lib import platform as platform_mod  # noqa: E402
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
        record["background"]["log_stream_pid"] = logs_mod.start_log_stream(
            target, stream, bundle_id=record.get("app_id")
        )

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


def cmd_journal(args: argparse.Namespace) -> int:
    session = session_mod.load_current()
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
    if args.x is not None and args.y is not None:
        x, y = args.x, args.y
    else:
        matches = selector_mod.select(
            ui_mod.snapshot(target),
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
    ui_mod.tap(target, x, y)
    return emit({"ok": True, "x": x, "y": y, "ref": ref, **target.identity()}, as_json=True)


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
    return emit({"ok": True, "typed": args.text, **target.identity()}, as_json=True)


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
    )
    return emit({"ok": True, **payload}, as_json=True)


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
    p.set_defaults(func=cmd_network_requests_list)
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

    logs = sub.add_parser("logs", help="device logs")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    p = logs_sub.add_parser("tail", help="tail recent logs", parents=[target_flags])
    p.add_argument("--package", help="filter by package / bundle id")
    p.add_argument("--since", type=float, default=30, help="seconds of recent logs (best-effort)")
    p.add_argument("--max-lines", type=int, default=200)
    p.add_argument("--grep", help="regex filter")
    p.set_defaults(func=cmd_logs_tail)

    return parser


# Verbs whose own handler writes the journal, or that only read it — never
# journaled by the choke point, to avoid double entries and read-noise.
_JOURNAL_SKIP = {"note", "journal", "version"}

# Sub-command dests, in priority order, for reconstructing a verb string.
_SUBCOMMAND_DESTS = (
    "session_command", "ui_command", "network_command", "requests_command",
    "mock_command", "location_command", "media_command", "crash_command",
    "file_command", "record_command", "shots_command", "devices_command",
    "note_command",
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
        platform_mod.apply_tool_overrides(args)
        return args.func(args)
    except errors.AutonomError as exc:
        ok, error_code = False, exc.code
        return fail_error(exc)
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
