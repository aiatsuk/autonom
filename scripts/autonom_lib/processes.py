"""Machine-level registry of the processes Autonom spawns, and their reaping.

Long-lived children — `mitmdump`, an iOS `log stream`, a screen recorder — used
to be tracked only inside their own session directory, so `doctor` looked for
them under `Path.cwd()/.autonom`. A proxy started in one working directory was
therefore invisible from another, and could hold a port for hours while every
diagnostic reported a clean machine.

Two independent mechanisms fix that, deliberately overlapping:

1. **The registry** (`$AUTONOM_HOME/processes/processes.json`) records every
   child at spawn time, so discovery no longer depends on the caller's cwd.
2. **Signature discovery** finds a `mitmdump` running *our* addon even when the
   registry entry was lost — a machine that lost power mid-run, an artifacts
   directory deleted by hand, a registry file removed. Without this, "clean up
   whatever is left" would be a promise the registry alone cannot keep.

A process is only ever classified, never guessed at: `live` means it is doing
its job for an intact session and must be left alone; `orphan` means nothing
owns it any more.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import session as session_mod

# Unmistakably ours: mitmproxy invoked with Autonom's own addon file. Matching
# on "mitmdump" alone would sweep up a colleague's unrelated proxy.
ADDON_MARKER = "mitm_addon.py"
PROXY_MARKER = "autonom_dir="


def registry_dir() -> Path:
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        root = Path(explicit)
    else:
        state = os.environ.get("XDG_STATE_HOME")
        root = Path(state) / "autonom" if state else Path.home() / ".local/state/autonom"
    path = root / "processes"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def registry_file() -> Path:
    return registry_dir() / "processes.json"


def _read() -> list[dict[str, Any]]:
    path = registry_file()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("processes") if isinstance(payload, dict) else payload
    return list(entries or [])


def _write(entries: list[dict[str, Any]]) -> None:
    path = registry_file()
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".processes-",
        suffix=".tmp", delete=False,
    )
    try:
        json.dump({"processes": entries}, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, path)


def register(kind: str, pid: int, **detail: Any) -> dict[str, Any]:
    """Record a child. Called at spawn time, before anything can go wrong."""
    entries = [item for item in _read() if item.get("pid") != pid]
    entry = {
        "kind": kind,
        "pid": int(pid),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **detail,
    }
    entries.append(entry)
    _write(entries)
    return entry


def deregister(pid: int) -> None:
    entries = _read()
    remaining = [item for item in entries if item.get("pid") != pid]
    if len(remaining) != len(entries):
        _write(remaining)


# --- discovery ----------------------------------------------------------------


def _running_processes() -> list[tuple[int, str]]:
    """(pid, command) for every process on the machine, or [] if ps is unusable."""
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head, _, command = stripped.partition(" ")
        if head.isdigit() and command:
            found.append((int(head), command))
    return found


def discover_proxies() -> list[dict[str, Any]]:
    """Autonom proxies found by command line, registry or no registry."""
    found = []
    for pid, command in _running_processes():
        if ADDON_MARKER not in command:
            continue
        if "mitmdump" not in command and "mitmproxy" not in command:
            continue
        directory = None
        for token in command.split():
            if token.startswith(PROXY_MARKER):
                directory = token[len(PROXY_MARKER):]
        found.append({"kind": "proxy", "pid": pid, "artifacts_dir": directory,
                      "command": command[:400], "source": "signature"})
    return found


def _still_owned(entry: dict[str, Any]) -> bool:
    """Does an intact session still claim this process?

    A proxy whose artifacts directory or `proxy.json` has gone is answerable to
    nobody: `network stop` can no longer reach it, because the file it reads to
    find the pid is exactly what disappeared.
    """
    directory = entry.get("artifacts_dir")
    if not directory:
        return False
    root = Path(directory)
    if not root.is_dir():
        return False
    if entry.get("kind") == "proxy":
        return (root / "proxy.json").exists()
    return True


def scan() -> dict[str, Any]:
    """Classify every Autonom process on the machine. Read-only."""
    entries = {int(item["pid"]): dict(item) for item in _read() if item.get("pid")}
    for candidate in discover_proxies():
        pid = candidate["pid"]
        if pid in entries:
            entries[pid].setdefault("artifacts_dir", candidate.get("artifacts_dir"))
            entries[pid]["source"] = "registry+signature"
        else:
            # Running with our signature but unknown to the registry: the entry
            # was lost, so nothing but this scan can ever find it again.
            entries[pid] = candidate

    live, orphans, stale = [], [], []
    for pid, entry in sorted(entries.items()):
        if not session_mod.pid_alive(pid):
            stale.append(entry)
        elif _still_owned(entry):
            live.append(entry)
        else:
            entry.setdefault("reason", "no session owns this process any more")
            orphans.append(entry)
    return {"live": live, "orphans": orphans, "stale_entries": stale}


# --- reaping ------------------------------------------------------------------


def reap_stale_entries() -> int:
    """Drop registry rows whose process is gone. Touches no process."""
    entries = _read()
    remaining = [item for item in entries
                 if item.get("pid") and session_mod.pid_alive(int(item["pid"]))]
    if len(remaining) != len(entries):
        _write(remaining)
    return len(entries) - len(remaining)


def cleanup(*, dry_run: bool = False, include_live: bool = False) -> dict[str, Any]:
    """Terminate orphans (and, on request, healthy processes too).

    `include_live` exists for "stop everything Autonom started" — the honest
    escape hatch when a run is being abandoned. It is never the default: a live
    proxy may be serving a session in another terminal.
    """
    state = scan()
    targets = list(state["orphans"])
    if include_live:
        targets += state["live"]

    actions = []
    for entry in targets:
        pid = int(entry["pid"])
        action = {"kind": entry.get("kind", "process"), "pid": pid,
                  "artifacts_dir": entry.get("artifacts_dir"),
                  "reason": entry.get("reason", "requested")}
        if dry_run:
            action["result"] = "would_terminate"
        else:
            terminated = session_mod.terminate_pid(pid)
            action["result"] = "terminated" if terminated else "termination_failed"
            if terminated:
                deregister(pid)
        actions.append(action)

    reaped = 0 if dry_run else reap_stale_entries()
    return {
        "dry_run": dry_run,
        "actions": actions,
        "terminated": sum(1 for item in actions if item["result"] == "terminated"),
        "failed": sum(1 for item in actions if item["result"] == "termination_failed"),
        "stale_entries_reaped": reaped,
        "still_live": 0 if include_live else len(state["live"]),
        "registry": str(registry_file()),
    }
