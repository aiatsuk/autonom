"""`autonom doctor` — one answer to "what can this machine actually do?" (CAP-DOC).

Reports each backing tool, what capability it unlocks, and the exact command to
fix it when missing. Always exits 0 unless `--strict`, because a diagnostic that
fails is useless in a shell pipeline.

Two install traps found while bringing idb up on this machine are detected
explicitly, since neither error message points at its own cause:

- `fb-idb` calls `asyncio.get_event_loop()`, which became a hard error in
  Python 3.14 — the client dies with a traceback before doing any work.
- Homebrew refuses to load formulae from an untrusted tap.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import adb as adb_mod
from . import errors, ios_idb, ios_simctl, session as session_mod

PYTHON_314_HINT = (
    "fb-idb is incompatible with Python 3.14 (asyncio.get_event_loop() raises). "
    "Reinstall it under an older interpreter: "
    "pipx reinstall fb-idb --python /opt/homebrew/opt/python@3.12/bin/python3.12"
)
BREW_TRUST_HINT = (
    "Homebrew refuses untrusted taps: brew tap facebook/fb && "
    "brew trust --formula facebook/fb/idb-companion && brew install idb-companion"
)

# Environment overrides the resolvers honour. A host-wide override is the
# classic invisible trap: a stale AUTONOM_ADB makes every probe report adb as
# missing while `which adb` in the same shell finds it, and nothing in the old
# report said why. Doctor now names every active override, and warns when a
# binary override points at nothing.
BINARY_OVERRIDES = (
    "AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_EMULATOR", "AUTONOM_MITMDUMP",
)
OVERRIDE_VARS = BINARY_OVERRIDES + (
    "AUTONOM_IDB_COMPANION", "AUTONOM_HOME", "AUTONOM_CORESIMULATOR_DEVICES",
)


def _overrides() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    active: dict[str, Any] = {}
    warnings: list[dict[str, Any]] = []
    for name in OVERRIDE_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        entry: dict[str, Any] = {"value": value}
        if name in BINARY_OVERRIDES:
            exists = Path(value).expanduser().is_file() or shutil.which(value) is not None
            entry["exists"] = exists
            if not exists:
                warnings.append({
                    "code": "override_path_missing",
                    "variable": name,
                    "error": f"{name}={value} points at no executable",
                    "hint": f"unset {name} (or the matching flag) or point it at an "
                            "existing binary; the tool it overrides reads as missing "
                            "until then.",
                })
        active[name] = entry
    return active, warnings


def _probe_binary(name: str, resolver, version_fn) -> dict[str, Any]:
    entry: dict[str, Any] = {"state": "missing", "path": None, "version": None}
    try:
        path = resolver()
    except errors.AutonomError as exc:
        entry["error"] = exc.message
        entry["install_hint"] = exc.hint
        return entry
    entry["path"] = path
    try:
        entry["version"] = version_fn(path)
        entry["state"] = "ok" if entry["version"] else "error"
    except Exception as exc:  # noqa: BLE001 - a probe must never take doctor down
        entry["state"] = "error"
        entry["error"] = str(exc)
    return entry


def _adb_version(path: str) -> str | None:
    completed = subprocess.run(
        [path, "version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False, timeout=20,
    )
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip().splitlines()[0] if completed.stdout else None


def _xcrun_version(path: str) -> str | None:
    completed = subprocess.run(
        [path, "simctl", "help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=30,
    )
    if completed.returncode != 0:
        return None
    developer = subprocess.run(
        ["xcode-select", "-p"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, check=False, timeout=20,
    )
    return (developer.stdout or "").strip() or "simctl available"


def _mitmdump_version(path: str) -> str | None:
    completed = subprocess.run(
        [path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False, timeout=30,
    )
    if completed.returncode != 0:
        return None
    for line in (completed.stdout or "").splitlines():
        if line.lower().startswith("mitmproxy"):
            return line.strip()
    return (completed.stdout or "").strip().splitlines()[0] if completed.stdout else None


def _idb_entry(explicit: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"state": "missing", "path": None, "version": None}
    try:
        path = ios_idb.find_idb(explicit)
    except errors.AutonomError as exc:
        entry["error"] = exc.message
        entry["install_hint"] = f"{BREW_TRUST_HINT}; then: pipx install fb-idb"
        return entry
    entry["path"] = path

    completed = subprocess.run(
        [path, "list-targets", "--json"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False, timeout=40,
    )
    output = completed.stdout or ""
    if completed.returncode == 0:
        entry["state"] = "ok"
        entry["version"] = f"client ok ({len([x for x in output.splitlines() if x.strip()])} target(s))"
        return entry

    entry["state"] = "error"
    entry["error"] = output.strip()[-400:] or f"idb list-targets exited {completed.returncode}"
    if "get_event_loop" in output or "no current event loop" in output.lower():
        entry["install_hint"] = PYTHON_314_HINT
    else:
        entry["install_hint"] = "Check 'idb list-targets' by hand; ensure idb_companion is installed."
    return entry


def _companion_entry() -> dict[str, Any]:
    path = shutil.which("idb_companion")
    if not path:
        return {
            "state": "missing", "path": None, "version": None,
            "install_hint": BREW_TRUST_HINT,
        }
    return {"state": "ok", "path": path, "version": ios_idb.companion_version(path)}


def collect(args: Any = None) -> dict[str, Any]:
    explicit = lambda name: getattr(args, name, None) if args else None  # noqa: E731

    tools = {
        "adb": _probe_binary("adb", lambda: adb_mod.find_adb(explicit("adb")), _adb_version),
        "simctl": _probe_binary(
            "simctl", lambda: ios_simctl.find_simctl(explicit("simctl")), _xcrun_version
        ),
        "idb": _idb_entry(explicit("idb")),
        "idb_companion": _companion_entry(),
        "mitmdump": _probe_binary(
            "mitmdump", lambda: _find_mitmdump(explicit("mitmdump")), _mitmdump_version
        ),
    }

    capabilities = {
        "android": {
            "ready": tools["adb"]["state"] == "ok",
            "needs": "adb",
        },
        "ios_session": {
            "ready": tools["simctl"]["state"] == "ok",
            "needs": "xcrun (Xcode)",
        },
        "ios_ui": {
            "ready": tools["idb"]["state"] == "ok" and tools["idb_companion"]["state"] == "ok",
            "needs": "idb + idb_companion",
        },
        "network": {
            "ready": tools["mitmdump"]["state"] == "ok",
            "needs": "mitmdump (mitmproxy)",
        },
    }

    # Optional profilers never gate `--strict` (is_healthy reads `tools` only):
    # a missing xctrace narrows what metrics can do, it does not break the host.
    from .metrics import presets as presets_mod

    metrics_caps = {
        "android_meminfo": tools["adb"]["state"] == "ok",
        "ios_host_ps": shutil.which("ps") is not None,
        "ios_xctrace": presets_mod.xctrace_available(tools["simctl"].get("path")),
        "flutter_frame_summary": True,
    }

    record = session_mod.load_current()
    session_summary = None
    if record:
        session_summary = {
            "session_id": record.get("session_id"),
            "platform": record.get("platform"),
            "target_id": record.get("target_id"),
            "app_id": record.get("app_id"),
            "artifacts_dir": record.get("artifacts_dir"),
        }

    network_state, orphans, warnings = _runtime_state(record)

    overrides, override_warnings = _overrides()
    warnings.extend(override_warnings)

    # The mock registry outlives every session, so doctor is the one place
    # guaranteed to reveal a rule left enabled days ago.
    from .network import mocks as mocks_mod

    mocks_state = mocks_mod.summary()
    if mocks_state["active"]:
        warnings.append({
            "code": "persistent_mocks_active",
            "error": f"{mocks_state['active']} mock rule(s) are enabled in the "
                     f"persistent registry and will fake responses whenever a "
                     f"proxy runs",
            "hint": "Review with 'autonom network mock list', switch off with "
                    "'autonom network mock disable --all'.",
        })

    return {
        "ok": True,
        "tools": tools,
        "capabilities": capabilities,
        "metrics": metrics_caps,
        "session": session_summary,
        "network": network_state,
        "mocks": mocks_state,
        "overrides": overrides,
        "orphans": orphans,
        "warnings": warnings,
    }


def _find_mitmdump(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("AUTONOM_MITMDUMP")
    if candidate:
        return candidate
    path = shutil.which("mitmdump")
    if not path:
        raise errors.tool_missing("mitmdump")
    return path


def _runtime_state(record: dict[str, Any] | None) -> tuple[dict[str, Any], list, list]:
    orphans: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    network = {"running": False, "proxy_port": None, "attached": False}

    proxy_file = _latest_proxy_file(record)
    if proxy_file and proxy_file.exists():
        try:
            proxy = json.loads(proxy_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            proxy = {}
        pid = proxy.get("pid")
        alive = session_mod.pid_alive(pid)
        network = {"running": alive, "proxy_port": proxy.get("port"),
                   "attached": bool((record or {}).get("network", {}).get("attached"))}
        if alive and not record:
            orphans.append({"kind": "proxy", "pid": pid, "port": proxy.get("port"),
                            "hint": "autonom network stop"})

    if record:
        background = record.get("background") or {}
        for kind, key in (("log_stream", "log_stream_pid"), ("recorder", "recorder_pid")):
            pid = background.get(key)
            if pid and not session_mod.pid_alive(pid):
                warnings.append({"code": "stale_background_pid", "kind": kind, "pid": pid,
                                 "hint": "The session records a pid that is no longer running."})

    # Machine-wide sweep. The block above can only see what the current working
    # directory knows about, which is exactly how a proxy started elsewhere held
    # a port for hours while doctor reported a clean machine.
    from . import processes as processes_mod

    machine = processes_mod.scan()
    seen = {item.get("pid") for item in orphans}
    for entry in machine["orphans"]:
        if entry.get("pid") in seen:
            continue
        orphans.append({**entry, "hint": "autonom cleanup"})

    # A live proxy in someone else's directory is not an orphan — `network stop`
    # can still reach it from there — but it is invisible from here, holds a
    # port, and may be intercepting traffic nobody is watching. Name it.
    current_session = (record or {}).get("session_id")
    foreign = [entry for entry in machine["live"]
               if entry.get("session_id") != current_session or not record]
    if foreign:
        warnings.append({
            "code": "foreign_proxy_running",
            "error": f"{len(foreign)} Autonom proxy/proxies belong to another session: "
                     + ", ".join(f"pid {item['pid']}"
                                 + (f" ({item['artifacts_dir']})" if item.get("artifacts_dir")
                                    else "")
                                 for item in foreign[:5]),
            "hint": "Run 'autonom network stop' from that session's directory, or "
                    "'autonom cleanup --all' to stop every Autonom process.",
        })

    if machine["stale_entries"]:
        warnings.append({
            "code": "stale_process_entries",
            "error": f"{len(machine['stale_entries'])} process registry entr(ies) "
                     f"point at pids that are gone",
            "hint": "Run 'autonom cleanup' to reap them.",
        })

    dangling = _dangling_attachment(record, network)
    if dangling:
        warnings.append(dangling)

    warnings.extend(_foreign_attachments(record, machine["live"]))

    return network, orphans, warnings


def _foreign_attachments(record: dict[str, Any] | None,
                         live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emulators whose global proxy points at an Autonom proxy nobody here owns.

    Found on a real machine: a session from three days earlier had attached
    the emulator to its proxy and never detached; the proxy was still alive,
    so `device_may_be_left_attached` (which needs a *dead* proxy) stayed
    silent while every request from the emulator was routed through a
    capture nobody was reading. This looks at every running emulator, not
    just the session's target, because the attachment outlives the session.
    """
    if not live:
        return []
    try:
        adb_path = adb_mod.find_adb()
        devices = adb_mod.list_devices(adb_path)
    except errors.AutonomError:
        return []
    ports_by_pid = {entry.get("pid"): entry for entry in live if entry.get("port")}
    current_port = ((record or {}).get("network") or {}).get("proxy_port")
    found: list[dict[str, Any]] = []
    for device in devices:
        if device.state != "device" or not device.serial.startswith("emulator-"):
            continue
        try:
            completed = adb_mod.run_adb(
                adb_path, ["shell", "settings", "get", "global", "http_proxy"],
                serial=device.serial, timeout=10, check=False,
            )
        except errors.AutonomError:
            continue
        value = (completed.stdout if isinstance(completed.stdout, str) else "").strip()
        if not value or value in ("null", ":0"):
            continue
        host, _, port_text = value.rpartition(":")
        if host != "10.0.2.2" or not port_text.isdigit():
            continue
        port = int(port_text)
        if current_port and port == int(current_port):
            continue
        owner = next((entry for entry in ports_by_pid.values() if entry.get("port") == port), None)
        if owner is None:
            continue
        found.append({
            "code": "device_attached_to_foreign_proxy",
            "target_id": device.serial,
            "device_proxy": value,
            "pid": owner.get("pid"),
            "session_id": owner.get("session_id"),
            "error": f"{device.serial} routes all traffic through Autonom proxy pid "
                     f"{owner.get('pid')} (session {owner.get('session_id')}), which "
                     "this session does not own",
            "hint": "Run 'autonom network detach' from that session's context, or "
                    "'adb shell settings put global http_proxy :0' to clear it by hand.",
        })
    return found


def _latest_proxy_file(record: dict[str, Any] | None) -> Path | None:
    if record:
        return Path(record["artifacts_dir"]) / "network" / "proxy.json"
    root = session_mod.sessions_home()
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("*/network/proxy.json"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    return candidates[0] if candidates else None


def _dangling_attachment(record: dict[str, Any] | None, network: dict[str, Any]) -> dict[str, Any] | None:
    """CAP-DOC-003 — a device left pointing at a dead proxy has no working network."""
    source = record
    if source is None:
        root = session_mod.sessions_home()
        if not root.is_dir():
            return None
        sessions = sorted(root.glob("*/session.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not sessions:
            return None
        try:
            source = json.loads(sessions[0].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    attached = (source.get("network") or {}).get("attached")
    if attached and not network.get("running"):
        return {
            "code": "device_may_be_left_attached",
            "platform": source.get("platform"),
            "target_id": source.get("target_id"),
            "hint": "Run 'autonom network detach' to restore the device's previous proxy setting.",
        }
    return None


def is_healthy(report: dict[str, Any]) -> bool:
    return all(entry["state"] == "ok" for entry in report["tools"].values())
