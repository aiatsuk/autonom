"""Read-only facts about one built iOS app, for `simulator slim suggest`.

Three sources, each optional: the Info.plist of the app and its extensions,
the entitlements (`codesign`, else the XML plist a simulator build keeps in
its executable), and the frameworks the binaries link (`otool -L`) or ship.
Nothing here fails the scan: a missing tool or an unreadable file becomes a
line in `limitations`. The rules that turn the facts into features are data
in `ios_slim.py`.
"""
from __future__ import annotations

import mmap
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from . import ios_slim

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[bytes]"]

TOOL_TIMEOUT = 20
MAX_BINARIES = 120          # otool runs: the main executable and its frameworks
MAX_EXTENSIONS = 40
MAX_EMBEDDED_PLISTS = 16    # XML plists looked at inside one executable
MAX_PLIST_BYTES = 1 << 20
_XML_START = b"<?xml"
_PLIST_END = b"</plist>"
_FRAMEWORK = re.compile(r"([^/\s]+)\.framework/")
_USAGE_KEY = re.compile(r"^NS[A-Za-z0-9]*UsageDescription$")
_OTHER_INFO_KEYS = ("NSSupportsLiveActivities",)


def run_tool(argv: Sequence[str]) -> "subprocess.CompletedProcess[bytes]":
    """One host tool, output as bytes; a tool that is missing or hangs is
    exit 127 / 124 with the reason on stderr, never an exception."""
    try:
        return subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False, timeout=TOOL_TIMEOUT)
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, b"", b"not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, b"", b"timed out")
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 126, b"", str(exc).encode())


def _load_plist(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except Exception:  # noqa: BLE001 - plistlib raises expat, value and OS errors
        return None
    return loaded if isinstance(loaded, dict) else None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _read_info(info: dict[str, Any], facts: dict[str, Any]) -> None:
    for key in info:
        if isinstance(key, str) and (_USAGE_KEY.match(key) or key in _OTHER_INFO_KEYS):
            facts["info_keys"].add(key)
    facts["background_modes"].update(_strings(info.get("UIBackgroundModes")))
    facts["activity_types"].update(_strings(info.get("NSUserActivityTypes")))
    extension = info.get("NSExtension")
    if isinstance(extension, dict):
        facts["extension_points"].update(
            _strings(extension.get("NSExtensionPointIdentifier")))


def _looks_like_entitlements(loaded: Any) -> bool:
    if not isinstance(loaded, dict) or not loaded:
        return False
    if "CFBundleIdentifier" in loaded or "CFBundleExecutable" in loaded:
        return False  # an embedded Info.plist
    return all(isinstance(key, str) for key in loaded)


def parse_entitlements(data: bytes) -> dict[str, Any] | None:
    """The first XML plist in `data` that reads as entitlements."""
    position = 0
    for _ in range(MAX_EMBEDDED_PLISTS):
        start = data.find(_XML_START, position)
        if start < 0:
            return None
        end = data.find(_PLIST_END, start, start + MAX_PLIST_BYTES)
        if end < 0:
            position = start + len(_XML_START)
            continue
        position = end + len(_PLIST_END)
        try:
            loaded = plistlib.loads(bytes(data[start:position]))
        except Exception:  # noqa: BLE001 - not a plist after all
            continue
        if _looks_like_entitlements(loaded):
            return loaded
    return None


def embedded_entitlements(executable: Path) -> dict[str, Any] | None:
    """The entitlements a simulator build keeps in `__TEXT,__entitlements`,
    found by scanning the executable for an XML plist; None when there is
    none. Raises OSError when the file cannot be read."""
    with executable.open("rb") as handle:
        try:
            view = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:  # an empty file cannot be mapped
            return None
        try:
            return parse_entitlements(view)
        finally:
            view.close()


def parse_otool(text: str) -> tuple[set[str], set[str]]:
    """`otool -L` output -> (system framework names, names of frameworks the
    app ships itself: `@rpath/...`, `@executable_path/...`, `@loader_path/...`)."""
    system: set[str] = set()
    shipped: set[str] = set()
    for line in text.splitlines()[1:]:
        entry = line.strip().split(" (", 1)[0]
        match = _FRAMEWORK.search(entry)
        if not match:
            continue
        (shipped if entry.startswith("@") else system).add(match.group(1))
    return system, shipped


def _entitlements(app: Path, executable: Path | None, runner: Runner,
                  limitations: list[str]) -> dict[str, Any]:
    completed = runner(["codesign", "-d", "--entitlements", "-", "--xml", str(app)])
    if completed.returncode == 0 and completed.stdout:
        found = parse_entitlements(completed.stdout)
        if found:
            return found
    if executable is not None:
        try:
            found = embedded_entitlements(executable)
        except OSError as exc:
            limitations.append(f"entitlements: cannot read {executable.name}: "
                               f"{exc.strerror or exc}")
            return {}
        if found:
            return found
    limitations.append(
        "entitlements: none found by codesign or in the executable; features proven "
        "only by entitlements (push, iCloud, associated domains) may be missing")
    return {}


def scan(app: Path, *, runner: Runner = run_tool) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """(facts, app description, limitations) for the `.app` bundle at `app`."""
    facts = ios_slim.empty_facts()
    limitations: list[str] = []
    info = _load_plist(app / "Info.plist")
    if info is None:
        limitations.append("Info.plist: missing or unreadable; usage descriptions and "
                           "background modes were not read")
        info = {}
    _read_info(info, facts)
    name = info.get("CFBundleExecutable")
    executable = app / name if isinstance(name, str) and name and "/" not in name else None
    if executable is not None and not executable.is_file():
        limitations.append(f"executable {executable.name} not found in the bundle")
        executable = None
    elif executable is None:
        limitations.append("Info.plist names no executable; linked frameworks and "
                           "entitlements of the main binary were not read")

    extensions = []
    try:
        extensions = sorted((app / "PlugIns").glob("*.appex"))
    except OSError:
        pass
    if len(extensions) > MAX_EXTENSIONS:
        limitations.append(f"extensions: {len(extensions) - MAX_EXTENSIONS} of "
                           f"{len(extensions)} were not read")
        extensions = extensions[:MAX_EXTENSIONS]
    for extension in extensions:
        extension_info = _load_plist(extension / "Info.plist")
        if extension_info is None:
            limitations.append(f"extension {extension.name}: Info.plist unreadable")
            continue
        _read_info(extension_info, facts)
    try:
        facts["app_clip"] = any((app / "AppClips").glob("*.app"))
    except OSError:
        pass

    facts["entitlements"] = _entitlements(app, executable, runner, limitations)

    binaries: list[Path] = [executable] if executable is not None else []
    try:
        frameworks = sorted((app / "Frameworks").glob("*.framework"))
    except OSError:
        frameworks = []
    for framework in frameworks:
        facts["embedded_frameworks"].add(framework.name[:-len(".framework")])
        binary = framework / framework.name[:-len(".framework")]
        if binary.is_file():
            binaries.append(binary)
    if len(binaries) > MAX_BINARIES:
        limitations.append(f"linked frameworks: {len(binaries) - MAX_BINARIES} of "
                           f"{len(binaries)} binaries were not read")
        binaries = binaries[:MAX_BINARIES]
    failed = 0
    reason = ""
    for binary in binaries:
        completed = runner(["otool", "-L", str(binary)])
        if completed.returncode != 0:
            failed += 1
            reason = reason or (completed.stderr or b"").decode("utf-8", "replace").strip()
            if completed.returncode == 127:
                break  # no otool on this host: one line, not one per binary
            continue
        system, shipped = parse_otool((completed.stdout or b"").decode("utf-8", "replace"))
        facts["linked_frameworks"] |= system
        facts["embedded_frameworks"] |= shipped
    if failed:
        limitations.append(
            f"linked frameworks: otool could not read {failed} of {len(binaries)} "
            f"binaries ({reason[:120] or 'no output'}); frameworks linked only there "
            "are missing")
    if not facts["embedded_frameworks"]:
        limitations.append("no embedded frameworks: plugins linked statically show up "
                           "only through the system frameworks they use")
    described = {
        "path": str(app),
        "bundle_id": info.get("CFBundleIdentifier")
        if isinstance(info.get("CFBundleIdentifier"), str) else None,
        "name": next((info[key] for key in ("CFBundleDisplayName", "CFBundleName")
                      if isinstance(info.get(key), str) and info[key]), app.stem),
        "extensions": len(extensions),
        "linked_frameworks": len(facts["linked_frameworks"]),
        "embedded_frameworks": len(facts["embedded_frameworks"]),
        "entitlements": len(facts["entitlements"]),
    }
    return facts, described, limitations
