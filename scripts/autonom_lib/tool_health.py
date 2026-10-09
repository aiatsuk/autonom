"""Tool health for the Canvas Activity tab: is each device tool found and usable?

`check()` answers for adb, emulator, xcrun_simctl, idb_companion, scrcpy_server,
ffmpeg and mitmdump with the probes `autonom doctor` uses (doctor.py), cached
for 60 s. Each entry is a ToolHealth:

    {"name", "found": bool, "path"|null, "version"|null, "ok": bool,
     "needed_for": text, "error"|null, "hint"|null}

`scrcpy_server` is ok only at the scrcpy protocol version the Canvas speaks;
the iOS tools report `found: false`, `error: "macOS only"` on other systems.

Run as `python3 -m autonom_lib.tool_health` (from `scripts/`) it prints
`{"checked_at", "tools"}` as one JSON line, so the Canvas can show health with
no device attached.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import adb as adb_mod
from . import doctor, emulator, errors, ios_simctl

CACHE_SECONDS = 60.0
TOOL_NAMES = ("adb", "emulator", "xcrun_simctl", "idb_companion", "scrcpy_server",
              "ffmpeg", "mitmdump")
NEEDED_FOR = {
    "adb": "every Android device action, screenshots and recordings",
    "emulator": "starting Android emulators (devices boot)",
    "xcrun_simctl": "every iOS Simulator action, screenshots and recordings",
    "idb_companion": "the fast iOS Simulator stream and input of the Canvas",
    "scrcpy_server": "the fast Android stream and input of the Canvas",
    "ffmpeg": "the Canvas screenrecord fallback stream",
    "mitmdump": "network capture and mocks",
}
EMULATOR_HINT = ("Install the Android SDK 'emulator' package (Android Studio does), "
                 "or set AUTONOM_EMULATOR.")
FFMPEG_HINT = "Install ffmpeg (brew install ffmpeg, or sudo apt-get install ffmpeg)."
MACOS_ONLY = "macOS only"

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": None, "value": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _entry(name: str, *, found: bool, path: str | None = None, version: str | None = None,
           ok: bool = False, error: str | None = None, hint: str | None = None) -> dict[str, Any]:
    return {"name": name, "found": bool(found), "path": path, "version": version,
            "ok": bool(ok) and bool(found), "needed_for": NEEDED_FOR[name],
            "error": error, "hint": hint if not ok else None}


def _from_doctor(name: str, probe: dict[str, Any]) -> dict[str, Any]:
    """A doctor probe (`state`, `path`, `version`, `error`, `install_hint`) as ToolHealth."""
    state = probe.get("state")
    return _entry(name, found=bool(probe.get("path")), path=probe.get("path"),
                  version=probe.get("version"), ok=state == "ok",
                  error=probe.get("error") or (None if state == "ok" else
                                               "not found" if not probe.get("path") else None),
                  hint=probe.get("install_hint"))


def _first_line(argv: list[str], timeout: float = 10) -> str | None:
    try:
        completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, text=True, check=False,
                                   timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (completed.stdout or "").strip().splitlines()
    return lines[0].strip()[:160] if completed.returncode == 0 and lines else None


def _adb() -> dict[str, Any]:
    return _from_doctor("adb", doctor._probe_binary(  # noqa: SLF001 - doctor's own probe
        "adb", lambda: adb_mod.find_adb(None), doctor._adb_version))  # noqa: SLF001


def _emulator() -> dict[str, Any]:
    adb_path = shutil.which("adb")
    try:
        path = emulator.find_emulator(None, adb_path=adb_path)
    except errors.AutonomError as exc:
        return _entry("emulator", found=False, error=exc.message, hint=exc.hint or EMULATOR_HINT)
    version = _first_line([path, "-version"], timeout=20)
    if version is None:
        return _entry("emulator", found=True, path=path,
                      error="'emulator -version' did not answer", hint=EMULATOR_HINT)
    return _entry("emulator", found=True, path=path, version=version, ok=True)


def _macos() -> bool:
    return sys.platform == "darwin"


def _simctl() -> dict[str, Any]:
    if not _macos():
        return _entry("xcrun_simctl", found=False, error=MACOS_ONLY)
    return _from_doctor("xcrun_simctl", doctor._probe_binary(  # noqa: SLF001
        "simctl", lambda: ios_simctl.find_simctl(None), doctor._xcrun_version))  # noqa: SLF001


def _idb_companion() -> dict[str, Any]:
    if not _macos():
        return _entry("idb_companion", found=False, error=MACOS_ONLY)
    probe = dict(doctor._companion_entry())  # noqa: SLF001
    probe.pop("_info", None)
    return _from_doctor("idb_companion", probe)


def _scrcpy_server() -> dict[str, Any]:
    probe = doctor._scrcpy_entry()  # noqa: SLF001
    server = probe.get("server_path")
    found = bool(server) and Path(server).is_file()
    ok = probe.get("state") == "ok" and probe.get("version") == doctor.SCRCPY_PROTOCOL_VERSION
    return _entry("scrcpy_server", found=found, path=server if found else None,
                  version=probe.get("version"), ok=ok,
                  error=probe.get("error") or (None if found else "not found"),
                  hint=probe.get("install_hint"))


def _ffmpeg() -> dict[str, Any]:
    path = shutil.which("ffmpeg")
    if not path:
        return _entry("ffmpeg", found=False, error="not found", hint=FFMPEG_HINT)
    version = _first_line([path, "-version"])
    if version is None:
        return _entry("ffmpeg", found=True, path=path, error="'ffmpeg -version' did not answer",
                      hint=FFMPEG_HINT)
    return _entry("ffmpeg", found=True, path=path, version=version, ok=True)


def _mitmdump() -> dict[str, Any]:
    return _from_doctor("mitmdump", doctor._probe_binary(  # noqa: SLF001
        "mitmdump", lambda: doctor._find_mitmdump(None), doctor._mitmdump_version))  # noqa: SLF001


PROBES: dict[str, Callable[[], dict[str, Any]]] = {
    "adb": _adb,
    "emulator": _emulator,
    "xcrun_simctl": _simctl,
    "idb_companion": _idb_companion,
    "scrcpy_server": _scrcpy_server,
    "ffmpeg": _ffmpeg,
    "mitmdump": _mitmdump,
}


def _probe(name: str) -> dict[str, Any]:
    try:
        return PROBES[name]()
    except Exception as exc:  # noqa: BLE001 - one probe never takes the others down
        return _entry(name, found=False, error=f"the check failed: {exc}"[:300])


def check(refresh: bool = False) -> dict[str, Any]:
    """`{"checked_at", "tools": [ToolHealth]}`, from a cache at most 60 s old
    unless `refresh`."""
    with _lock:
        cached_at = _cache["at"]
        if (not refresh and cached_at is not None
                and time.monotonic() - cached_at < CACHE_SECONDS):
            return _cache["value"]
        value = {"checked_at": _now_iso(), "tools": [_probe(name) for name in TOOL_NAMES]}
        _cache["at"] = time.monotonic()
        _cache["value"] = value
        return value


def reset_cache() -> None:
    with _lock:
        _cache["at"] = None
        _cache["value"] = None


def main() -> int:
    print(json.dumps(check(refresh="--refresh" in sys.argv[1:]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
