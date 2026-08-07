"""Session records and artifact directories (CAP-PLAT-003).

Schema v2 adds `platform`, `target_id`, and the `tooling` / `network` /
`background` / `consent_log` blocks. Every v1 key is still written, so a 0.4.0
consumer keeps working (INV-01), and a v1 record found on disk is upgraded **in
memory only** — an upgrade must never silently rewrite a file this process did
not create, because a user may be mid-investigation when they update (INV-02).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import errors

SCHEMA_VERSION = 2


def sessions_home() -> Path:
    """The machine-global session store: `$AUTONOM_HOME/sessions`, else
    `~/.autonom/sessions`. Sessions live here — not in the project — so a run is
    not tied to the directory it was launched from and the active session is
    found from anywhere, the same way mocks, the process registry, and per-app
    knowledge are already machine-level."""
    home = os.environ.get("AUTONOM_HOME")
    base = Path(home) if home else Path.home() / ".autonom"
    root = base / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def artifacts_root(cwd: Path | None = None) -> Path:
    # Default is the global store. An explicit cwd forces the legacy
    # project-local `.autonom/` layout (used by a few tests and anyone who
    # deliberately wants a run's artifacts to live beside the code).
    if cwd is not None:
        base = cwd / ".autonom"
        base.mkdir(parents=True, exist_ok=True)
        return base
    return sessions_home()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_record(
    *,
    platform: str,
    target_id: str,
    app_id: str | None,
    artifacts_dir: Path,
    session_id: str,
    tooling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "platform": platform,
        "target_id": target_id,
        "aliases": {"serial": target_id} if platform == "android" else {"udid": target_id},
        "app_id": app_id,
        "install_path": None,
        "started_at": _now(),
        "artifacts_dir": str(artifacts_dir),
        "display": None,
        "tooling": tooling or {},
        "network": {
            "enabled": False,
            "proxy_host": None,
            "proxy_port": None,
            "device_proxy": None,
            "attached": False,
            "previous_http_proxy": None,
        },
        "background": {"log_stream_pid": None, "recorder_pid": None},
        "consent_log": [],
    }
    if platform == "android":
        # DEC-004: `serial` and `adb` are permanent for Android callers.
        record["serial"] = target_id
        record["adb"] = (tooling or {}).get("adb")
    return record


def upgrade(record: dict[str, Any]) -> dict[str, Any]:
    """Bring a v1 record up to v2 shape without touching the file it came from."""
    if not record:
        return record
    upgraded = dict(record)
    upgraded.setdefault("schema_version", 1)
    platform = upgraded.get("platform") or "android"
    upgraded["platform"] = platform
    if not upgraded.get("target_id"):
        upgraded["target_id"] = upgraded.get("serial") or upgraded.get("udid") or ""
    if not upgraded.get("aliases"):
        key = "serial" if platform == "android" else "udid"
        upgraded["aliases"] = {key: upgraded["target_id"]} if upgraded["target_id"] else {}
    upgraded.setdefault("install_path", None)
    upgraded.setdefault("display", None)
    upgraded.setdefault("tooling", {"adb": upgraded.get("adb")} if upgraded.get("adb") else {})
    upgraded.setdefault(
        "network",
        {
            "enabled": False,
            "proxy_host": None,
            "proxy_port": None,
            "device_proxy": None,
            "attached": False,
            "previous_http_proxy": None,
        },
    )
    upgraded.setdefault("background", {"log_stream_pid": None, "recorder_pid": None})
    upgraded.setdefault("consent_log", [])
    return upgraded


def start_session(
    tool: str,
    *,
    serial: str | None = None,
    app_id: str | None = None,
    cwd: Path | None = None,
    platform: str = "android",
    target_id: str | None = None,
    tooling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the artifact tree and the session record.

    `tool` and `serial` keep their 0.4.0 positions so existing callers and tests
    (which pass `start_session("adb", serial=...)`) are unaffected.
    """
    resolved_id = target_id or serial
    if not resolved_id:
        raise errors.AutonomError(errors.NO_TARGET, "a target id is required to start a session")
    session_id = f"s_{uuid.uuid4().hex[:10]}"
    root = artifacts_root(cwd) / session_id
    for name in ("shots", "trees", "logs", "network", "recordings", "crashes", "files"):
        (root / name).mkdir(parents=True, exist_ok=True)

    resolved_tooling = dict(tooling or {})
    if platform == "android":
        resolved_tooling.setdefault("adb", tool)
    else:
        resolved_tooling.setdefault("simctl", tool)

    record = new_record(
        platform=platform,
        target_id=resolved_id,
        app_id=app_id,
        artifacts_dir=root,
        session_id=session_id,
        tooling=resolved_tooling,
    )
    save(record, cwd)
    return record


def save(record: dict[str, Any], cwd: Path | None = None) -> dict[str, Any]:
    """Persist to both the session directory and the current-session pointer."""
    path = Path(record["artifacts_dir"]) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    path.write_text(payload, encoding="utf-8")
    _write_current(cwd, record)
    return record


def stop_session(cwd: Path | None = None) -> dict[str, Any] | None:
    current = artifacts_root(cwd) / "current.json"
    if not current.exists():
        return None
    record = upgrade(json.loads(current.read_text(encoding="utf-8")))
    record["stopped_at"] = _now()
    session_path = Path(record["artifacts_dir"]) / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    current.unlink(missing_ok=True)
    return record


def load_current(cwd: Path | None = None) -> dict[str, Any] | None:
    path = artifacts_root(cwd) / "current.json"
    if not path.exists():
        return None
    return upgrade(json.loads(path.read_text(encoding="utf-8")))


def require_current(cwd: Path | None = None) -> dict[str, Any]:
    record = load_current(cwd)
    if not record:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION,
            "no active session",
            "Start one with 'autonom session start'.",
        )
    return record


def _write_current(cwd: Path | None, record: dict[str, Any]) -> None:
    path = artifacts_root(cwd) / "current.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def artifact_path(record: dict[str, Any], *parts: str) -> Path:
    path = Path(record["artifacts_dir"]).joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --- teardown ----------------------------------------------------------------


def run_teardown(actions: list[tuple[str, Callable[[], Any]]]) -> list[dict[str, Any]]:
    """Best-effort teardown (INV-10).

    Each action is isolated: `session stop` must never fail because a proxy was
    already dead or a companion refused to disconnect. Failures are reported per
    action so a partial teardown is visible rather than silent.
    """
    results: list[dict[str, Any]] = []
    for name, action in actions:
        try:
            detail = action()
            results.append({"action": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            results.append({"action": name, "ok": False, "error": str(exc)})
    return results


def terminate_pid(pid: int | None, *, timeout: float = 5.0) -> bool:
    """Stop a session-owned background process; returns True when it was alive."""
    if not pid:
        return False
    try:
        os.kill(pid, 15)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    return True


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --- Android app control (unchanged behavior) --------------------------------


def install_app(adb: str, serial: str, path: Path) -> None:
    from . import adb as adb_mod

    adb_mod.run_adb(adb, ["install", "-r", str(path)], serial=serial, timeout=180, check=True)


def launch_app(adb: str, serial: str, app_id: str, activity: str | None = None) -> None:
    from . import adb as adb_mod

    if activity:
        component = activity if "/" in activity else f"{app_id}/{activity}"
        adb_mod.run_adb(
            adb,
            ["shell", "am", "start", "-n", component],
            serial=serial,
            timeout=30,
            check=True,
        )
        return
    adb_mod.run_adb(
        adb,
        ["shell", "monkey", "-p", app_id, "-c", "android.intent.category.LAUNCHER", "1"],
        serial=serial,
        timeout=30,
        check=True,
    )


def force_stop(adb: str, serial: str, app_id: str) -> None:
    from . import adb as adb_mod

    adb_mod.run_adb(adb, ["shell", "am", "force-stop", app_id], serial=serial, timeout=20, check=True)


def clear_data(adb: str, serial: str, app_id: str) -> None:
    from . import adb as adb_mod

    adb_mod.run_adb(adb, ["shell", "pm", "clear", app_id], serial=serial, timeout=30, check=True)
