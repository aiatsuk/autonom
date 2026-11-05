#!/usr/bin/env python3
"""Live check of the whole Android path through the CLI (VER-003).

Not part of the unit suite. It drives one private bench emulator and nothing
else, through the Autonom CLI plus independent adb reads:

    python3 tests/live/android_helper_live.py --serial emulator-5612 \\
        --avd AB_Bench_MCP_API36 [--boot] --evidence-dir DIR

Before anything else it refuses unless ``adb -s SERIAL emu avd name`` is
--avd (exit 2; nothing on the device is touched). A serial that is not
running is booted only with --boot: cold (``-no-snapshot``) on the serial's
console port, and then it must name --avd too. When another emulator takes
the serial during the boot (it names another AVD, or this run's emulator
exits while the serial is listed), the run is refused the same way: only
this run's own emulator process is stopped, and no command goes to the
device on the serial. The cleanup sends device commands only to a serial
whose AVD name this run confirmed (and reads that name again first); on
any other serial it skips them. Only an emulator this run booted is stopped
at the end. The host load (1 minute) must fall below --max-load (60) before
the boot and before the checks.

Every CLI call is ``scripts/autonom.py --adb <wrapper> --serial SERIAL ...``
in a fresh AUTONOM_HOME (a second fresh one for step 13), without any
AUTONOM_* variable of the caller's environment, so the backend is the default
(auto). The wrapper appends every adb call the CLI makes to a log and runs the
real adb: the report counts helper starts (``app_process``) and size lookups
from it, and proves that no other serial was named and that no forward but
Autonom's own was removed. The script's own adb calls always name the serial
too (``adb devices`` and ``forward --list`` are host reads).

Preflight: helper processes left on the bench device by an earlier run are
killed (pid verified by /proc/<pid>/cmdline); Settings is restarted on its
list and "Network & internet" is opened once with ``adb shell input`` and
closed with BACK, so the navigation tap below does not also measure the first
visit to that page after a boot. Settings is always restarted with
``am start -W --activity-clear-task``, so a page an earlier step left in its
task (the search page, an app info page) cannot come back to the front.
A step whose expected screen did not show saves the last read's payload as
``<step>-last-read.json`` and names that read's first labels in its error.

Steps, one check each (``s01``..``s15`` in the report):

 1. boot and forwards: ``adb forward --list`` is recorded, then a decoy
    forward ``tcp:0 -> localabstract:autonom-decoy`` is added on the serial.
 2. ``ui tree`` on the Settings list: ok, ``ui_backend: helper``, one build
    under ``<home>/android-helper/builds/<id>/`` made by this call (one
    ``app_process`` start), ``ui helper status`` names that build, and a
    second ``ui tree`` reuses it (same id, same dex file; no new start).
 3. the same screen with ``--ui-backend uiautomator``: the same outline lines
    as a helper read just before it (up to three tries for a screen that was
    still changing), and the opt-out reply has no ``ui_backend``.
 4. ``ui tree`` on Settings > About emulated device (a page uiautomator
    cannot read idle): ok through the helper, its labels above the fold
    present ("Device name", "Model"), ``settled`` reported, no
    ``ui_not_idle``.
    Then the device must be quiet before the timing steps: ``ui tree`` reads
    repeat until two in a row report ``settled`` true with ``settle_ms`` <=
    1000 through the helper, for at most 240 s (``preflight.device_quiet``).
 5. ``session start --profile agent``; ``ui tap --text "Network & internet"``
    returns that page (``screen.outline``) with ``settled: true``, no
    ``screen_not_settled`` warning, ``input_backend: helper``, and the CLI
    made no ``wm size``, ``dumpsys input``/``window`` or rotation call and
    no ``adb shell input``.
 6. Settings search field: ``ui type "Привет café 東京 😀"``, then
    ``ui fill "abc"``, then ``ui type "é東"``; fresh reads show exactly the
    typed text, then "abc", then "abcé東"; the replies say ``complete`` and
    ``text_matches``.
 7. ``signals.keyboard.visible`` true while the search field has focus, and
    false after ``ui key KEYCODE_BACK``.
 8. App info of an installed app (``--alert-app``, else the first of a few
    stock apps) after starting it; ``ui tap --text "Force stop"`` shows the
    dialog; ``ui tree`` reports ``signals.dialog`` of kind ``alert`` (a
    framework AlertDialog) or ``window`` (a Compose dialog, as Settings shows
    on API 36) with at least two button labels; a second read a moment later
    still shows it and the app still runs (nothing was clicked by Autonom).
    The script then taps Cancel.
 9. ``screenshot --agent`` on the Settings list: a JPEG whose long edge is at
    most 1000 px and whose frame header matches the reply, ``scale`` = screen
    long edge / image long edge, and a ``coordinates`` note. The centre of
    the "Network & internet" row, taken to image pixels and back with the
    note's formula, lies on the row, and ``ui tap --x --y`` there opens that
    page.
10. the helper's pid is killed with adb; ``ui tree`` starts exactly one new
    helper (one ``app_process``) and is served by it.
11. the helper is killed and one with a wrong build id is started on the same
    socket; ``ui tree`` replaces it (one ``app_process``), and
    ``ui helper status`` names the expected build.
12. 125 s without a call: no ``autonom-helper`` process is left (and none
    was gone before 115 s), its log says ``exit idle``, and
    ``uiautomator dump`` returns a complete hierarchy.
13. in a second fresh AUTONOM_HOME with no JDK on PATH and no JAVA_HOME,
    ANDROID_HOME or ANDROID_SDK_ROOT: ``ui tree`` is served by uiautomator
    with a ``fallback_reason`` naming javac and a ``fallback_hint``, and the
    CLI pushed, started and forwarded nothing for the helper.
14. ``ui tree`` starts the helper again after the idle stop (one start);
    ``session stop``'s ``android_helper`` step stops it: no ``autonom-helper``
    process and no Autonom forward are left, while the decoy and every
    forward of this serial from step 1 are still listed. A forward of another
    serial that went away meanwhile is reported, not failed, when no adb call
    of this run removed it (other sessions own those).
15. cleanup, always (also after a failure): first the serial must still run
    the AVD this run confirmed (``device_identity``: ``verified``); else
    every device step below is skipped (``never verified`` when the run
    stopped before it confirmed the AVD, ``changed`` when the serial now
    names another AVD or none, which fails the check). On the confirmed
    device, a session still live is stopped
    and a helper still running is stopped (``ui helper stop``, then its
    verified pid); the decoy is removed by its port (never
    ``forward --remove-all``); the helper's dex and log are removed from
    /data/local/tmp by exact name; airplane mode is checked (and turned off
    when a stray tap turned it on); no forward of this serial from step 1 is
    missing. Always: the CLI and the script named no other serial; the
    emulator is stopped only when this run booted it (with ``emu kill`` only
    while it names the confirmed AVD, else by its own process).

Writes one report, <evidence-dir>/android-helper-live.json (with ``timings``:
first build, warm read, tap with settle, typing, fill, screenshot), plus step
files beside it (the adb log of the CLI, replies, the screenshot, the helper
log, dumps), and prints one JSON line naming it. Exit 0 when every check
passed; 3 (environment fault) when the device never went quiet within the
240 s, every failed check is a timing check (5, 6, 7, 9) that ended without
an exception, and the cleanup passed; 1 for any other failure; 2 for a usage
error or a refusal. SIGTERM or SIGHUP stops the steps, runs the cleanup and
exits 128 + the signal.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "autonom.py"

PROCESS = "autonom-helper"
SOCKET = "autonom-helper"
MAIN_CLASS = "dev.autonom.helper.Main"
DEVICE_DIR = "/data/local/tmp"
DEVICE_LOG = f"{DEVICE_DIR}/autonom-helper.log"
HELPER_IDLE_MS = 120000
DECOY_SOCKET = "autonom-decoy"
SETTINGS = "com.android.settings"
SETTINGS_MAIN = "com.android.settings/.Settings"
UNICODE = "Привет café 東京 😀"
HOME_LABELS = {"Network & internet", "Connected devices"}
NETWORK_ROW = "Network & internet"
NETWORK_LABEL = "Airplane mode"
# Above the fold of the About page on a 1080x2400 AVD: 'Build number' is below
# it and 'Android version' is the clipped last row (2026-10-25).
ABOUT_LABELS = {"Device name", "Model"}
# s08: a framework AlertDialog is `alert`, a Compose or other modal dialog
# (no framework ids) is `window`.
DIALOG_KINDS = ("alert", "window")
# What a step that did not find its screen names of the last read.
LAST_READ_LABELS = 6
LAST_READ_LABEL_CHARS = 40
SEARCH_LABEL = "Search Settings"
ALERT_APPS = ("com.google.android.deskclock", "com.android.deskclock",
              "com.google.android.calculator", "com.android.calculator2",
              "com.google.android.contacts", "com.android.contacts",
              "com.google.android.apps.messaging", "com.android.messaging")
SERIAL = re.compile(r"^emulator-(\d+)$")
AVD = re.compile(r"^[A-Za-z0-9._-]+$")
PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
REPORT_NAME = "android-helper-live.json"
ADB_LOG_NAME = "cli-adb-calls.log"
EXPECTED_CHECKS = (
    "s01_boot_forwards", "s02_first_read_builds", "s03_parity_uiautomator",
    "s04_never_idle_page", "s05_agent_tap_settled", "s06_unicode_type_fill",
    "s07_keyboard_signal", "s08_dialog_signal", "s09_agent_screenshot_tap",
    "s10_restart_after_kill", "s11_wrong_build_replaced", "s12_idle_stop",
    "s13_fallback_without_tools", "s14_session_stop_teardown", "s15_cleanup",
)
TIMING_CHECKS = ("s05_agent_tap_settled", "s06_unicode_type_fill", "s07_keyboard_signal",
                 "s09_agent_screenshot_tap")
QUIET_LIMIT_S = 240.0
QUIET_SETTLE_MS = 1000
QUIET_READS = 2
IDLE_WAIT_S = 125.0
IDLE_EARLY_S = 115.0
EXIT_ENVIRONMENT = 3
CLI_TIMEOUT = 180.0
# How long the serial must stay listed after this run's emulator exited
# before another emulator is taken to hold it.
HOLDER_WAIT_S = 5.0
# Variables of the caller's environment that would change what the CLI does.
SCRUBBED_PREFIX = "AUTONOM_"
UNIT_SEP = "\x1f"


class StepFailed(Exception):
    pass


class Refused(Exception):
    pass


class Terminated(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def host_load() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


class Budget:
    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def need(self, seconds: float, what: str) -> None:
        if self.left() < seconds:
            raise StepFailed(f"not enough time left for {what}")


# --- the CLI's adb calls -----------------------------------------------------------------

def write_adb_wrapper(path: Path, real_adb: str, log: Path) -> Path:
    """A shell script that appends its argv (fields joined by the unit
    separator, one line per call, one write) to `log` and runs `real_adb`."""
    path.write_text(
        "#!/bin/sh\n"
        "# Autonom live check: log the CLI's adb call, then run the real adb.\n"
        "sep=$(printf '\\037')\n"
        "line=\n"
        "for arg in \"$@\"; do line=\"$line$arg$sep\"; done\n"
        f"printf '%s\\n' \"$line\" >> {shlex.quote(str(log))}\n"
        f"exec {shlex.quote(real_adb)} \"$@\"\n",
        encoding="utf-8")
    path.chmod(0o755)
    return path


def parse_adb_log(text: str) -> list[tuple[str | None, list[str]]]:
    """(serial or None, the rest of the argv) per logged call."""
    calls: list[tuple[str | None, list[str]]] = []
    for line in text.splitlines():
        if not line:
            continue
        argv = line.split(UNIT_SEP)
        if argv and argv[-1] == "":
            argv = argv[:-1]
        if argv[:1] == ["-s"] and len(argv) >= 2:
            calls.append((argv[1], argv[2:]))
        else:
            calls.append((None, argv))
    return calls


def shell_words(rest: list[str]) -> list[str]:
    """The words of an `adb shell` call, whether passed apart or as one string."""
    if rest[:1] != ["shell"]:
        return []
    words = rest[1:]
    if len(words) == 1:
        return words[0].split()
    return words


def is_size_call(rest: list[str]) -> bool:
    """`wm size` and the rotation sources: the per-tap lookups AU-04 removes."""
    words = shell_words(rest)
    return (words[:2] == ["wm", "size"] or words[:2] == ["dumpsys", "input"]
            or words[:2] == ["dumpsys", "window"]
            or words[:4] == ["settings", "get", "system", "user_rotation"])


def is_adb_input(rest: list[str]) -> bool:
    return shell_words(rest)[:1] == ["input"]


def is_helper_launch(rest: list[str]) -> bool:
    return rest[:1] == ["shell"] and "app_process" in " ".join(rest[1:]) \
        and f"--nice-name={PROCESS}" in " ".join(rest[1:])


def is_helper_setup(rest: list[str]) -> bool:
    """A push of the helper, its start, or a forward to its socket."""
    joined = " ".join(rest)
    return (is_helper_launch(rest)
            or (rest[:1] == ["push"] and "autonom-helper-" in joined)
            or (rest[:1] == ["forward"] and f"localabstract:{SOCKET}" in joined))


HOST_READS = (("devices",), ("version",), ("start-server",), ("forward", "--list"))


def adb_log_summary(calls: list[tuple[str | None, list[str]]], serial: str) -> dict[str, Any]:
    """What the calls prove about the device boundary: serials other than
    ours, calls without a serial that are not host reads, global forward
    removals, and every forward removal."""
    foreign = sorted({who for who, _ in calls if who is not None and who != serial})
    unscoped = [rest for who, rest in calls
                if who is None and not any(tuple(rest[:len(read)]) == read for read in HOST_READS)]
    removals = [rest for _, rest in calls if rest[:2] == ["forward", "--remove"]]
    return {"calls": len(calls), "foreign_serials": foreign, "unscoped": unscoped[:10],
            "remove_all": sum(1 for _, rest in calls if "--remove-all" in rest),
            "forward_removals": [" ".join(rest) for rest in removals]}


class AdbLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.touch()

    def calls(self) -> list[tuple[str | None, list[str]]]:
        return parse_adb_log(self.path.read_text(encoding="utf-8", errors="replace"))

    def mark(self) -> int:
        return len(self.calls())

    def since(self, mark: int) -> list[tuple[str | None, list[str]]]:
        return self.calls()[mark:]


# --- forwards ---------------------------------------------------------------------------

def forwards_verdict(before: list[str], after: list[str], serial: str, *,
                     decoy: str | None, own_ports: set[int], removals: list[str]) -> dict[str, Any]:
    """Step 14/15 oracle: every forward of `serial` listed in step 1 is still
    listed; a forward of another serial that went away is acceptable only
    when no adb call of this run removed it; no helper forward this run added
    for `serial` is left; and the decoy's state (`decoy` listed or not)."""
    before_set, after_set = set(before), set(after)
    missing = sorted(before_set - after_set)
    ours_missing = [line for line in missing if line.split()[:1] == [serial]]
    others_missing = [line for line in missing if line.split()[:1] != [serial]]
    removed_ports = {rest.split()[-1] for rest in removals}
    others_removed_by_us = [line for line in others_missing
                            if any(port == line.split()[1] for port in removed_ports)]
    own_left = sorted(line for line in after_set - before_set
                      if line.split()[:1] == [serial]
                      and (line.endswith(f"localabstract:{SOCKET}")
                           or any(f" tcp:{port} " in f" {line} " for port in own_ports)))
    return {"missing_this_serial": ours_missing, "missing_other_serials": others_missing,
            "other_serials_removed_by_this_run": others_removed_by_us,
            "own_helper_forwards_left": own_left,
            "decoy_listed": None if decoy is None else decoy in after_set}


# --- replies ------------------------------------------------------------------------------

def nodes_of(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    nodes = (payload or {}).get("nodes")
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def labels_of(payload: dict[str, Any] | None) -> set[str]:
    return {value for node in nodes_of(payload) for key in ("text", "desc")
            if isinstance(value := node.get(key), str) and value}


def outline_lines(payload: dict[str, Any] | None) -> list[str]:
    outline = (payload or {}).get("outline")
    if isinstance(outline, list):
        return [str(line) for line in outline]
    if isinstance(outline, str):
        return outline.splitlines()
    return []


def screen_outline(payload: dict[str, Any] | None) -> str:
    screen = (payload or {}).get("screen")
    if isinstance(screen, dict):
        outline = screen.get("outline")
        return "\n".join(outline) if isinstance(outline, list) else str(outline or "")
    return ""


def warning_codes(payload: dict[str, Any] | None) -> list[str]:
    return [str(item.get("code")) for item in (payload or {}).get("warnings") or []
            if isinstance(item, dict)]


def focused_field(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """The focused editable node of a read, else None."""
    for node in nodes_of(payload):
        if node.get("focused") and node.get("editable"):
            return node
    return None


def find_node(payload: dict[str, Any] | None, text: str) -> dict[str, Any] | None:
    for node in nodes_of(payload):
        if node.get("text") == text or node.get("desc") == text:
            return node
    return None


def signals_of(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    signals = (payload or {}).get("signals")
    if signals is None and isinstance((payload or {}).get("screen"), dict):
        signals = payload["screen"].get("signals")
    return signals if isinstance(signals, dict) else None


def dialog_seen(dialog: Any) -> bool:
    """A dialog signal of a kind s08 accepts (`DIALOG_KINDS`)."""
    return isinstance(dialog, dict) and dialog.get("kind") in DIALOG_KINDS


def button_labels(dialog: Any) -> list[str]:
    """The non-empty button labels of a dialog signal."""
    buttons = dialog.get("buttons") if isinstance(dialog, dict) else None
    if not isinstance(buttons, list):
        return []
    return [label for item in buttons if isinstance(item, dict)
            and isinstance(label := item.get("label"), str) and label]


def first_labels(payload: dict[str, Any] | None, limit: int = LAST_READ_LABELS) -> list[str]:
    """The first distinct labels of a read (text or description in node
    order, else its outline lines), each at most LAST_READ_LABEL_CHARS."""
    found: list[str] = []
    values = [node.get(key) for node in nodes_of(payload) for key in ("text", "desc")]
    if not any(isinstance(value, str) and value.strip() for value in values):
        values = outline_lines(payload)
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        label = " ".join(value.split())[:LAST_READ_LABEL_CHARS]
        if label not in found:
            found.append(label)
        if len(found) >= limit:
            break
    return found


_SCALE = re.compile(r"round\(x\*([0-9]+(?:\.[0-9]+)?)\)")


def note_scale(note: Any) -> float | None:
    """S from `image pixel (x, y) → ui tap --x round(x*S) --y round(y*S)`."""
    match = _SCALE.search(str(note or ""))
    return float(match.group(1)) if match else None


def image_round_trip(point: tuple[int, int], scale: float) -> tuple[tuple[int, int], tuple[int, int]]:
    """A screen point taken to image pixels, then back with the note's
    formula: (image point, screen point to tap)."""
    image = (round(point[0] / scale), round(point[1] / scale))
    return image, (round(image[0] * scale), round(image[1] * scale))


def inside(bounds: Any, point: tuple[int, int]) -> bool:
    if not (isinstance(bounds, list) and len(bounds) == 4):
        return False
    left, top, right, bottom = bounds
    return left <= point[0] < right and top <= point[1] < bottom


def center(bounds: list[int]) -> tuple[int, int]:
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + length
    return None


def stripped_tools_env(env: dict[str, str]) -> dict[str, str]:
    """`env` without a JDK: no JAVA_HOME, ANDROID_HOME or ANDROID_SDK_ROOT,
    and no PATH entry that holds an executable `javac`."""
    out = {key: value for key, value in env.items()
           if key not in ("JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT")}
    kept = []
    for entry in env.get("PATH", "").split(os.pathsep):
        candidate = Path(entry or ".") / "javac"
        if entry and not (candidate.is_file() and os.access(candidate, os.X_OK)):
            kept.append(entry)
    out["PATH"] = os.pathsep.join(kept)
    return out


def sdk_root_of(adb: str) -> str | None:
    """The SDK two levels above the real adb, when it holds build-tools."""
    root = Path(adb).resolve().parent.parent
    return str(root) if (root / "build-tools").is_dir() else None


# --- adb --------------------------------------------------------------------------------

class Adb:
    """The script's own adb: every device command names the one serial."""

    def __init__(self, adb: str, serial: str) -> None:
        self.adb = adb
        self.serial = serial
        self.calls: list[list[str]] = []

    def run(self, *args: str, timeout: float = 60, check: bool = True
            ) -> subprocess.CompletedProcess:
        argv = [self.adb, "-s", self.serial, *args]
        self.calls.append(argv[1:])
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                                   stdin=subprocess.DEVNULL, text=True, errors="replace")
        if check and completed.returncode != 0:
            raise StepFailed(f"adb {' '.join(args[:4])} failed ({completed.returncode}): "
                             f"{(completed.stderr or completed.stdout or '')[-300:]}")
        return completed

    def shell(self, command: str, *, timeout: float = 60, check: bool = True) -> str:
        return self.run("shell", command, timeout=timeout, check=check).stdout

    def devices(self) -> dict[str, str]:
        """serial -> state for every attached device (a host read)."""
        self.calls.append(["devices"])
        completed = subprocess.run([self.adb, "devices"], capture_output=True, text=True,
                                   timeout=30, check=False, stdin=subprocess.DEVNULL)
        rows: dict[str, str] = {}
        for line in completed.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                rows[parts[0]] = parts[1]
        return rows

    def forwards(self) -> list[str]:
        """Every forward of the adb server (a host read), one line each."""
        completed = self.run("forward", "--list", timeout=30, check=False)
        return sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def avd_name(self) -> str | None:
        completed = self.run("emu", "avd", "name", timeout=30, check=False)
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        return lines[0] if completed.returncode == 0 and lines and lines[0] != "OK" else None

    def helper_pids(self) -> list[int]:
        out = self.shell(f"pidof {PROCESS}", check=False).split()
        return [int(value) for value in out if value.isdigit()]

    def is_helper(self, pid: int) -> bool:
        cmdline = self.shell(f"cat /proc/{pid}/cmdline", check=False)
        return cmdline.split("\0")[0] == PROCESS

    def kill_helpers(self) -> list[int]:
        """Kill every `autonom-helper` pid whose cmdline says so."""
        killed = []
        for pid in self.helper_pids():
            if self.is_helper(pid):
                self.shell(f"kill {pid}", check=False)
                killed.append(pid)
        return killed

    def wait_no_helper(self, pids: list[int], seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not set(pids) & set(self.helper_pids()):
                return True
            time.sleep(0.3)
        return not set(pids) & set(self.helper_pids())

    def pidof(self, package: str) -> int | None:
        out = self.shell(f"pidof {package}", check=False).split()
        return int(out[0]) if out and out[0].isdigit() else None

    def dump(self) -> tuple[str | None, str]:
        """`uiautomator dump` XML, or None and what it printed instead."""
        completed = self.run("exec-out", "uiautomator", "dump", "/dev/tty", timeout=90,
                             check=False)
        out = completed.stdout or ""
        start, end = out.find("<?xml"), out.rfind("</hierarchy>")
        if start < 0 or end < 0:
            return None, (out + (completed.stderr or ""))[-300:]
        return out[start:end + len("</hierarchy>")], ""


def dump_nodes(xml: str) -> list[ET.Element]:
    try:
        return list(ET.fromstring(xml.encode("utf-8")).iter("node"))
    except ET.ParseError:
        return []


def dump_labels(xml: str) -> set[str]:
    return {value for node in dump_nodes(xml) for key in ("text", "content-desc")
            if (value := node.get(key))}


_DUMP_BOUNDS = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")


def dump_point(xml: str, text: str) -> tuple[int, int] | None:
    """Centre of the first node whose text is `text` in a uiautomator dump."""
    for node in dump_nodes(xml):
        if node.get("text") == text:
            match = _DUMP_BOUNDS.match(node.get("bounds") or "")
            if match:
                left, top, right, bottom = (int(value) for value in match.groups())
                return (left + right) // 2, (top + bottom) // 2
    return None


# --- the CLI ----------------------------------------------------------------------------

class Reply:
    def __init__(self, argv: list[str], code: int, payload: dict[str, Any] | None,
                 stdout: str, stderr: str, ms: float,
                 calls: list[tuple[str | None, list[str]]]) -> None:
        self.argv, self.code, self.payload = argv, code, payload or {}
        self.stdout, self.stderr, self.ms, self.calls = stdout, stderr, ms, calls

    @property
    def ok(self) -> bool:
        return self.code == 0 and self.payload.get("ok") is True

    def summary(self) -> dict[str, Any]:
        return {"argv": self.argv, "code": self.code, "ms": self.ms,
                "error_code": self.payload.get("error_code"),
                "ui_backend": self.payload.get("ui_backend"),
                "input_backend": self.payload.get("input_backend"),
                "fallback_reason": self.payload.get("fallback_reason"),
                "adb_calls": len(self.calls)}


def last_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            return value if isinstance(value, dict) else None
    return None


class Cli:
    """`scripts/autonom.py --adb <wrapper> --serial SERIAL ...` in one home."""

    def __init__(self, wrapper: Path, serial: str, log: AdbLog, base_env: dict[str, str]) -> None:
        self.wrapper = wrapper
        self.serial = serial
        self.log = log
        self.base_env = base_env
        self.history: list[dict[str, Any]] = []

    def __call__(self, *argv: str, home: Path, strip_tools: bool = False,
                 timeout: float = CLI_TIMEOUT) -> Reply:
        env = dict(self.base_env)
        env["AUTONOM_HOME"] = str(home)
        if strip_tools:
            env = stripped_tools_env(env)
        full = [sys.executable, str(CLI), "--adb", str(self.wrapper), "--serial", self.serial,
                *argv]
        mark = self.log.mark()
        started = time.monotonic()
        try:
            completed = subprocess.run(full, capture_output=True, text=True, timeout=timeout,
                                       env=env, stdin=subprocess.DEVNULL, check=False,
                                       errors="replace")
            code, out, err = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = -1, str(exc.stdout or ""), f"timed out after {timeout} s"
        ms = round((time.monotonic() - started) * 1000, 1)
        payload = last_json(out if code in (0, 1) else err) or last_json(out)
        reply = Reply(list(argv), code, payload, out, err, ms, self.log.since(mark))
        self.history.append({**reply.summary(), "strip_tools": strip_tools,
                             "home": "second" if strip_tools else "main"})
        return reply


# --- the run ---------------------------------------------------------------------------

class Live:
    def __init__(self, args: argparse.Namespace, report: dict[str, Any]) -> None:
        self.args = args
        self.report = report
        self.checks: dict[str, dict[str, Any]] = report["checks"]
        self.timings: dict[str, Any] = report["timings"]
        self.evidence = Path(args.evidence_dir)
        self.budget = Budget(args.time_limit)
        self.adb = Adb(args.adb, args.serial)
        self.work = Path(tempfile.mkdtemp(prefix="autonom-helper-live-"))
        self.home = self.work / "home"
        self.home_bare = self.work / "home-no-tools"
        self.log = AdbLog(self.work / "adb-calls.log")
        self.wrapper = write_adb_wrapper(self.work / "adb", args.adb, self.log.path)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(SCRUBBED_PREFIX)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if not env.get("ANDROID_HOME") and not env.get("ANDROID_SDK_ROOT"):
            # the wrapper hides the SDK the real adb lives in
            root = sdk_root_of(args.adb)
            if root:
                env["ANDROID_HOME"] = root
        self.cli = Cli(self.wrapper, args.serial, self.log, env)
        self.booted: subprocess.Popen | None = None
        # True only once `emu avd name` on the serial said --avd (preflight or
        # boot); no device command of the cleanup runs before that.
        self.target_verified = False
        self.decoy_port: int | None = None
        self.decoy_line: str | None = None
        self.session_live = False
        self.own_ports: set[int] = set()
        self.build: str | None = None
        self.keyboard_up: dict[str, Any] | None = None
        self.last_helper_call = time.monotonic()
        self.forwards_before: list[str] = []
        # the check `guarded` runs now (None outside the steps)
        self.current_step: str | None = None

    # -- bookkeeping

    def check(self, name: str, ok: bool, **detail: Any) -> bool:
        self.checks[name] = {"ok": bool(ok), **detail}
        return bool(ok)

    def guarded(self, name: str, step: Callable[[], None]) -> None:
        started = time.monotonic()
        self.current_step = name
        try:
            step()
        except (Terminated, KeyboardInterrupt):
            raise
        except Exception as exc:  # one broken step must not stop the others or the cleanup
            self.checks.setdefault(name, {})
            self.checks[name].update({"ok": False, "error": f"{type(exc).__name__}: {exc}"[-500:]})
        finally:
            self.current_step = None
        self.checks.setdefault(name, {"ok": False, "error": "no result"})
        self.checks[name]["seconds"] = round(time.monotonic() - started, 1)

    def screen_missing(self, what: str, reply: Reply | None) -> StepFailed:
        """The failure of a step whose expected screen did not show: the last
        read's payload is saved as `<step>-last-read.json` (`preflight-...`
        outside the steps) and its first labels are named."""
        payload = reply.payload if reply is not None else {}
        name = f"{self.current_step or 'preflight'}-last-read.json"
        try:
            self.save(name, payload)
            saved = f"saved as {name}"
        except (OSError, TypeError, ValueError) as exc:
            saved = f"not saved: {type(exc).__name__}"
        labels = first_labels(payload)
        shown = ", ".join(repr(label) for label in labels) if labels else "no labels"
        return StepFailed(f"{what}; the last read showed {shown} ({saved})")

    def save(self, name: str, data: str | bytes | dict) -> str:
        path = self.evidence / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        elif isinstance(data, dict):
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            path.write_text(data, encoding="utf-8")
        return str(path)

    def run_cli(self, *argv: str, bare: bool = False, timeout: float = CLI_TIMEOUT) -> Reply:
        reply = self.cli(*argv, home=self.home_bare if bare else self.home, strip_tools=bare,
                         timeout=timeout)
        if not bare:
            self.last_helper_call = time.monotonic()
        return reply

    def tree(self, *extra: str) -> Reply:
        return self.run_cli("ui", "tree", "--format", "json", *extra)

    def record(self) -> dict[str, Any]:
        path = self.home / "android-helper" / "devices" / f"{self.args.serial}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def builds(self, home: Path | None = None) -> list[dict[str, Any]]:
        root = (home or self.home) / "android-helper" / "builds"
        found = []
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    dex = entry / "classes.dex"
                    stat = dex.stat() if dex.exists() else None
                    found.append({"id": entry.name, "complete": (entry / "complete").exists(),
                                  "dex_bytes": stat.st_size if stat else None,
                                  "dex_inode": stat.st_ino if stat else None,
                                  "dex_mtime_ns": stat.st_mtime_ns if stat else None})
                elif entry.name.endswith(".failed"):
                    found.append({"id": entry.name, "failed": True})
        return found

    def note_port(self, payload: dict[str, Any] | None) -> None:
        port = (payload or {}).get("port")
        if isinstance(port, int):
            self.own_ports.add(port)

    # -- device and host

    def resolve_target(self) -> None:
        state = self.adb.devices().get(self.args.serial)
        self.report["preflight"] = preflight = {"state_before": state, "load_start": host_load()}
        if state is not None:
            name = self.adb.avd_name()
            preflight["avd_name"] = name
            if name != self.args.avd:
                raise Refused(f"{self.args.serial} runs AVD {name!r}, not {self.args.avd!r}")
            if state != "device":
                raise Refused(f"{self.args.serial} is {state}")
            self.target_verified = True
            preflight["booted_by_live"] = False
            return
        if not self.args.boot:
            raise Refused(f"{self.args.serial} is not running; pass --boot to boot "
                          f"{self.args.avd} cold on it")
        self.wait_load("boot")
        self.boot()

    def boot(self) -> None:
        port = int(SERIAL.match(self.args.serial).group(1))
        emulator = self.args.emulator or find_emulator(self.args.adb)
        if not emulator:
            raise StepFailed("emulator binary not found (pass --emulator)")
        argv = [emulator, "-avd", self.args.avd, "-port", str(port), "-no-snapshot",
                "-no-audio", "-no-boot-anim"]
        if not self.args.window:
            argv.append("-no-window")
        started = time.monotonic()
        with (self.evidence / "emulator.log").open("w", encoding="utf-8") as log:
            self.booted = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
        self.report["boot"] = boot = {"argv": argv, "pid": self.booted.pid, "started_at": now()}
        deadline = time.monotonic() + min(self.args.boot_timeout, self.budget.left() - 600)
        named = False
        while time.monotonic() < deadline:
            if self.booted.poll() is not None:
                # this run's emulator is gone: whatever the serial runs now is
                # not ours, so the cleanup sends it nothing
                self.target_verified = False
                boot["exit_code"] = self.booted.returncode
                holder = self.serial_holder(HOLDER_WAIT_S)
                if holder is not None and not named:
                    # another emulator holds the port (ours exited because of
                    # it): a refusal, like a serial running another AVD
                    boot["serial_held_by"] = name = self.adb.avd_name()
                    raise Refused(f"the emulator exited with {self.booted.returncode} while "
                                  f"{self.args.serial} is held ({holder}) by an emulator this "
                                  f"run did not boot (AVD {name!r})")
                raise StepFailed(f"the emulator exited with {self.booted.returncode} "
                                 f"(see emulator.log)")
            if self.adb.devices().get(self.args.serial) == "device":
                if not named:
                    name = self.adb.avd_name()
                    if name is None:
                        time.sleep(2)
                        continue
                    if name != self.args.avd:
                        # another emulator holds the port: stop only our own child
                        # process and refuse; nothing is sent to that device
                        self.stop_child()
                        boot["child_stopped"] = self.booted.poll() is not None
                        self.booted = None
                        raise Refused(f"{self.args.serial} came up as AVD {name!r}, not "
                                      f"{self.args.avd!r}")
                    named = True
                    self.target_verified = True
                    boot["avd_name"] = name
                if self.adb.shell("getprop sys.boot_completed", check=False).strip() == "1":
                    break
            time.sleep(2)
        else:
            raise StepFailed("the emulator did not finish booting in time")
        while time.monotonic() < deadline:  # the package manager settles after boot_completed
            if self.adb.run("shell", "pm", "path", SETTINGS, check=False).returncode == 0:
                break
            time.sleep(2)
        time.sleep(10)
        boot["seconds"] = round(time.monotonic() - started, 1)
        self.report["preflight"]["booted_by_live"] = True

    def serial_holder(self, seconds: float) -> str | None:
        """The serial's state in `adb devices` once it has stayed listed for
        `seconds` (adb drops an emulator that just exited within a moment),
        or None as soon as it is not listed. A host read only."""
        deadline = time.monotonic() + seconds
        while True:
            holder = self.adb.devices().get(self.args.serial)
            if holder is None or time.monotonic() >= deadline:
                return holder
            time.sleep(1)

    def wait_load(self, before: str) -> None:
        waited = time.monotonic()
        limit = min(self.args.load_wait, max(0.0, self.budget.left() - 600))
        while True:
            load = host_load()
            if load is None or load < self.args.max_load:
                self.report.setdefault("load", {})[before] = {
                    "load": load, "waited_s": round(time.monotonic() - waited, 1)}
                return
            if time.monotonic() - waited > limit:
                raise StepFailed(f"host load {load} stayed above {self.args.max_load}")
            time.sleep(10)

    def still_home_dump(self, seconds: float) -> str | None:
        """The uiautomator XML of the Settings list once two dumps in a row
        are equal (no helper runs yet), or None after `seconds`."""
        previous = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(1.5)
            xml, _ = self.adb.dump()
            if xml is None or not HOME_LABELS <= dump_labels(xml):
                previous = None
                continue
            if previous is not None and previous == xml:
                return xml
            previous = xml
        return None

    def warm_settings(self) -> None:
        """Settings restarted on its list, "Network & internet" opened once
        with adb input and closed with BACK (preflight; before any helper)."""
        detail: dict[str, Any] = {"opened": False}
        self.report["preflight"]["settings_warm_up"] = detail
        started = time.monotonic()
        home = None
        for _attempt in range(3):
            self.restart_settings()
            home = self.still_home_dump(40)
            if home:
                break
        if not home:
            detail["error"] = "Settings did not show a still main list"
            return
        point = dump_point(home, NETWORK_ROW)
        if point is None:
            detail["error"] = f"no {NETWORK_ROW!r} row"
            return
        self.adb.shell(f"input tap {point[0]} {point[1]}", check=False)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(1.0)
            page, _ = self.adb.dump()
            if page and NETWORK_LABEL in dump_labels(page):
                detail["opened"] = True
                break
        self.adb.shell("input keyevent 4", check=False)
        detail["home_still"] = self.still_home_dump(30) is not None
        detail["seconds"] = round(time.monotonic() - started, 1)

    def restart_settings(self) -> None:
        """Settings on its list in a cleared task: a page an earlier step
        left in that task (the search page of Settings Intelligence, an app
        info page) cannot come back to the front."""
        self.adb.shell(f"am force-stop {SETTINGS}", check=False)
        self.adb.run("shell", "am", "start", "-W", "--activity-clear-task", "-n", SETTINGS_MAIN,
                     timeout=120)

    def home_via_cli(self, seconds: float = 30, *, restart: bool = True) -> Reply:
        """The Settings list, read through the CLI: Settings is restarted on
        it (with `restart`, or when the list does not show), then read until
        the list shows in a read that settled, for at most `seconds` (a
        list that never settles is returned as it is: the step judges)."""
        reply = None
        if not restart:
            reply = self.tree()
            if not HOME_LABELS <= labels_of(reply.payload):
                reply = None
        if reply is None:
            self.restart_settings()
            reply = self.tree()
        deadline = time.monotonic() + seconds
        while (time.monotonic() < deadline
               and not (HOME_LABELS <= labels_of(reply.payload)
                        and reply.payload.get("settled") is True)):
            time.sleep(0.5)
            reply = self.tree()
        if not HOME_LABELS <= labels_of(reply.payload):
            raise self.screen_missing("the Settings list did not show", reply)
        return reply

    def quiet_read(self) -> dict[str, Any]:
        reply = self.tree()
        payload = reply.payload
        return {"settled": payload.get("settled") if payload.get("ui_backend") == "helper" else None,
                "settle_ms": payload.get("settle_ms"), "ui_backend": payload.get("ui_backend"),
                "code": reply.code}

    def wait_device_quiet(self) -> None:
        """Helper reads until QUIET_READS in a row settle within
        QUIET_SETTLE_MS, for at most QUIET_LIMIT_S; recorded in
        preflight.device_quiet. The steps run either way."""
        started = time.monotonic()
        limit = max(0.0, min(QUIET_LIMIT_S, self.budget.left() - 600))
        detail: dict[str, Any] = {"ok": False, "seconds": 0.0, "attempts": 0,
                                  "limit_s": round(limit, 1), "settle_ms_max": QUIET_SETTLE_MS,
                                  "full_wait": False}
        self.report.setdefault("preflight", {})["device_quiet"] = detail
        in_a_row = 0
        recent: list[dict[str, Any]] = []
        try:
            while True:
                read = self.quiet_read()
                detail["attempts"] += 1
                recent = (recent + [read])[-6:]
                settled, settle_ms = read.get("settled"), read.get("settle_ms")
                if settled is True and isinstance(settle_ms, int) and settle_ms <= QUIET_SETTLE_MS:
                    in_a_row += 1
                else:
                    in_a_row = 0
                if in_a_row >= QUIET_READS:
                    detail["ok"] = True
                    break
                if time.monotonic() - started >= limit:
                    detail["full_wait"] = limit >= QUIET_LIMIT_S
                    break
                time.sleep(0.5)
        except (Terminated, KeyboardInterrupt):
            raise
        except Exception as exc:  # a broken read is the steps' business
            detail["error"] = f"{type(exc).__name__}: {exc}"[-300:]
        detail["recent_reads"] = recent
        detail["seconds"] = round(time.monotonic() - started, 1)

    # -- the steps

    def run(self) -> None:
        self.resolve_target()
        self.wait_load("checks")
        self.guarded("s01_boot_forwards", self.step01)
        leftovers = self.adb.kill_helpers()  # an earlier run's helper on this bench device
        self.report["preflight"]["leftover_helpers_killed"] = leftovers
        if leftovers:
            self.adb.wait_no_helper(leftovers, 5)
        self.warm_settings()
        self.guarded("s02_first_read_builds", self.step02)
        if not self.checks["s02_first_read_builds"].get("served_by_helper"):
            return  # no helper: the other steps would only repeat that
        self.guarded("s03_parity_uiautomator", self.step03)
        self.guarded("s04_never_idle_page", self.step04)
        try:
            self.home_via_cli()
        except StepFailed as exc:
            self.report["preflight"]["home_before_quiet"] = str(exc)
        self.wait_device_quiet()
        self.guarded("s05_agent_tap_settled", self.step05)
        self.guarded("s06_unicode_type_fill", self.step06)
        self.guarded("s07_keyboard_signal", self.step07)
        self.guarded("s08_dialog_signal", self.step08)
        self.guarded("s09_agent_screenshot_tap", self.step09)
        self.guarded("s10_restart_after_kill", self.step10)
        self.guarded("s11_wrong_build_replaced", self.step11)
        self.guarded("s12_idle_stop", self.step12)
        self.guarded("s13_fallback_without_tools", self.step13)
        self.guarded("s14_session_stop_teardown", self.step14)

    def step01(self) -> None:
        self.forwards_before = self.adb.forwards()
        self.report["forwards_before"] = self.forwards_before
        self.save("forwards-before.txt", "\n".join(self.forwards_before) + "\n")
        out = self.adb.run("forward", "tcp:0", f"localabstract:{DECOY_SOCKET}",
                           timeout=30).stdout.strip()
        if not out.isdigit():
            raise StepFailed(f"the decoy forward printed {out!r}, not a port")
        self.decoy_port = int(out)
        self.decoy_line = f"{self.args.serial} tcp:{out} localabstract:{DECOY_SOCKET}"
        listed = self.adb.forwards()
        preflight = self.report.get("preflight") or {}
        self.check("s01_boot_forwards", self.decoy_line in listed
                   and set(self.forwards_before) <= set(listed),
                   booted_by_live=preflight.get("booted_by_live"),
                   boot_seconds=(self.report.get("boot") or {}).get("seconds"),
                   avd_name=preflight.get("avd_name") or self.args.avd,
                   forwards_before=len(self.forwards_before), decoy=self.decoy_line)

    def step02(self) -> None:
        before = self.builds()
        first = self.tree()
        self.timings["first_read_with_build_ms"] = first.ms
        self.save("s02-first-tree.json", first.payload)
        built = self.builds()
        status = self.run_cli("ui", "helper", "status")
        self.note_port(status.payload)
        second = self.tree()
        self.timings["warm_read_cli_ms"] = second.ms
        self.timings["read_settle_ms"] = second.payload.get("settle_ms")
        again = self.builds()
        complete = [item for item in built if item.get("complete")]
        self.build = complete[0]["id"] if len(complete) == 1 else None
        launches = [sum(1 for _, rest in reply.calls if is_helper_launch(rest))
                    for reply in (first, second)]
        served = first.ok and first.payload.get("ui_backend") == "helper"
        self.checks["s02_first_read_builds"] = {"served_by_helper": served}
        self.check("s02_first_read_builds",
                   served and before == [] and len(built) == 1 and self.build is not None
                   and not any(item.get("failed") for item in built)
                   and status.ok and status.payload.get("running") is True
                   and status.payload.get("build") == self.build
                   and second.ok and second.payload.get("ui_backend") == "helper"
                   and again == built and launches == [1, 0]
                   and HOME_LABELS <= labels_of(first.payload),
                   served_by_helper=served, builds_before=before, builds=built,
                   builds_after_second_read=again, build=self.build,
                   status_build=status.payload.get("build"), launches=launches,
                   settled=first.payload.get("settled"), settle_ms=first.payload.get("settle_ms"),
                   first_ms=first.ms, second_ms=second.ms,
                   tool_versions_cached=(self.home / "android-helper" / "tool-versions.json").exists())

    def step03(self) -> None:
        tries = []
        equal = False
        helper = opt_out = None
        for _attempt in range(3):
            helper = self.run_cli("ui", "tree", "--format", "outline")
            opt_out = self.run_cli("--ui-backend", "uiautomator", "ui", "tree", "--format", "outline")
            lines_h, lines_u = outline_lines(helper.payload), outline_lines(opt_out.payload)
            equal = bool(lines_h) and lines_h == lines_u
            tries.append({"helper_lines": len(lines_h), "uiautomator_lines": len(lines_u),
                          "equal": equal,
                          "first_differences": [[a, b] for a, b in zip(lines_h, lines_u) if a != b][:5]})
            if equal:
                break
            time.sleep(2)
        self.save("s03-helper-outline.txt", "\n".join(outline_lines(helper.payload)) + "\n")
        self.save("s03-uiautomator-outline.txt", "\n".join(outline_lines(opt_out.payload)) + "\n")
        self.check("s03_parity_uiautomator",
                   equal and helper.ok and helper.payload.get("ui_backend") == "helper"
                   and opt_out.ok and "ui_backend" not in opt_out.payload
                   and "settled" not in opt_out.payload,
                   tries=tries, helper_ms=helper.ms, uiautomator_ms=opt_out.ms,
                   opt_out_keys_absent="ui_backend" not in opt_out.payload)

    def step04(self) -> None:
        self.adb.run("shell", "am", "start", "-W", "-a", "android.settings.DEVICE_INFO_SETTINGS",
                     timeout=120)
        time.sleep(1.5)
        reply = self.tree()
        self.save("s04-about-tree.json", reply.payload)
        shown = labels_of(reply.payload)
        self.check("s04_never_idle_page",
                   reply.ok and reply.payload.get("ui_backend") == "helper"
                   and isinstance(reply.payload.get("settled"), bool)
                   and ABOUT_LABELS <= shown
                   and reply.payload.get("error_code") != "ui_not_idle",
                   settled=reply.payload.get("settled"), settle_ms=reply.payload.get("settle_ms"),
                   labels_found=sorted(ABOUT_LABELS & shown), error_code=reply.payload.get("error_code"),
                   cli_ms=reply.ms)

    def step05(self) -> None:
        start = self.run_cli("session", "start", "--app-id", SETTINGS, "--profile", "agent")
        self.session_live = start.ok
        if not start.ok:
            raise StepFailed(f"session start failed: {start.payload}")
        home = self.home_via_cli(restart=False)  # the list the quiet wait read
        if NETWORK_ROW not in labels_of(home.payload):
            raise self.screen_missing(f"no {NETWORK_ROW!r} row on the list", home)
        tap = self.run_cli("ui", "tap", "--text", NETWORK_ROW)
        self.save("s05-tap.json", tap.payload)
        outline = screen_outline(tap.payload)
        size_calls = [rest for _, rest in tap.calls if is_size_call(rest)]
        inputs = [rest for _, rest in tap.calls if is_adb_input(rest)]
        self.timings["tap_settle_read_s"] = round(tap.ms / 1000, 2)
        self.timings["tap_settle_ms"] = tap.payload.get("settle_ms")
        self.check("s05_agent_tap_settled",
                   tap.ok and tap.payload.get("input_backend") == "helper"
                   and tap.payload.get("settled") is True
                   and "screen_not_settled" not in warning_codes(tap.payload)
                   and NETWORK_LABEL in outline and not size_calls and not inputs,
                   settled=tap.payload.get("settled"), settle_ms=tap.payload.get("settle_ms"),
                   warnings=warning_codes(tap.payload), network_page=NETWORK_LABEL in outline,
                   input_backend=tap.payload.get("input_backend"),
                   size_calls=size_calls, adb_inputs=inputs, cli_ms=tap.ms,
                   session_profile=start.payload.get("profile")
                   or (start.payload.get("session") or {}).get("profile"))

    def field_text(self) -> str | None:
        field = focused_field(self.tree().payload)
        return None if field is None else field.get("text")

    def step06(self) -> None:
        self.keyboard_up = None
        self.home_via_cli()
        tap = self.run_cli("ui", "tap", "--text", SEARCH_LABEL)
        if not tap.ok:
            raise StepFailed(f"the search bar tap failed: {tap.payload.get('error_code')}")
        field = None
        read: Reply | None = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            read = self.tree()
            field = focused_field(read.payload)
            keyboard = (signals_of(read.payload) or {}).get("keyboard")
            if field and isinstance(keyboard, dict) and keyboard.get("visible") is True:
                break
            time.sleep(0.5)
        signals = signals_of(read.payload if read else None) or {}
        self.keyboard_up = {"field": bool(field), "keyboard": signals.get("keyboard")}
        if not field:
            raise self.screen_missing("the search field did not get focus", read)
        typed = self.run_cli("ui", "type", UNICODE)
        self.timings["type_unicode_cli_ms"] = typed.ms
        after_type = self.field_text()
        fill = self.run_cli("ui", "fill", "abc")
        self.timings["fill_cli_ms"] = fill.ms
        after_fill = self.field_text()
        insert = self.run_cli("ui", "type", "é東")
        after_insert = self.field_text()
        self.save("s06-replies.json", {"type": typed.payload, "fill": fill.payload,
                                       "insert": insert.payload})
        inputs = [rest for reply in (typed, fill, insert) for _, rest in reply.calls
                  if is_adb_input(rest)]
        self.check("s06_unicode_type_fill",
                   typed.ok and typed.payload.get("complete") is True
                   and typed.payload.get("text_after") == UNICODE and after_type == UNICODE
                   and fill.ok and fill.payload.get("text_matches") is True and after_fill == "abc"
                   and insert.ok and after_insert == "abcé東" and not inputs,
                   type_method=typed.payload.get("method"), type_complete=typed.payload.get("complete"),
                   type_text_after=typed.payload.get("text_after"), read_after_type=after_type,
                   fill_text_matches=fill.payload.get("text_matches"), read_after_fill=after_fill,
                   insert_complete=insert.payload.get("complete"), read_after_insert=after_insert,
                   backends=[reply.payload.get("input_backend") for reply in (typed, fill, insert)],
                   adb_inputs=inputs, field=(field or {}).get("resource_id"))

    def step07(self) -> None:
        up = self.keyboard_up
        if not up or not up.get("field"):
            raise StepFailed("no read with the search field focused")
        keyboard_up = up.get("keyboard") or {}
        self.run_cli("ui", "key", "KEYCODE_BACK")
        down: dict[str, Any] | None = None
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            down = (signals_of(self.tree().payload) or {}).get("keyboard")
            if isinstance(down, dict) and down.get("visible") is False:
                break
            time.sleep(0.5)
        self.check("s07_keyboard_signal",
                   isinstance(keyboard_up, dict) and keyboard_up.get("visible") is True
                   and bool(keyboard_up.get("package"))
                   and isinstance(down, dict) and down.get("visible") is False,
                   keyboard_up=keyboard_up, keyboard_down=down)

    def pick_alert_app(self) -> tuple[str, str]:
        candidates = [self.args.alert_app] if self.args.alert_app else list(ALERT_APPS)
        for package in candidates:
            if self.adb.run("shell", "pm", "path", package, check=False).returncode != 0:
                continue
            out = self.adb.shell(
                "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                f"-c android.intent.category.LAUNCHER {package}", check=False)
            lines = [line.strip() for line in out.splitlines() if "/" in line]
            if lines:
                return package, lines[-1]
        raise StepFailed(f"none of {candidates} is installed with a launcher activity")

    def step08(self) -> None:
        package, component = self.pick_alert_app()
        self.adb.run("shell", "am", "start", "-W", "-n", component, timeout=120)
        time.sleep(2)
        pid_before = self.adb.pidof(package)
        self.adb.run("shell", "am", "start", "-W", "-a",
                     "android.settings.APPLICATION_DETAILS_SETTINGS", "-d", f"package:{package}",
                     timeout=120)
        time.sleep(1.5)
        tap = self.run_cli("ui", "tap", "--text", "Force stop", "--mode", "exact")
        dialog_in_tap = (signals_of(tap.payload) or {}).get("dialog")
        read = self.tree()
        self.save("s08-dialog-tree.json", read.payload)
        dialog = (signals_of(read.payload) or {}).get("dialog")
        time.sleep(1.5)
        again = (signals_of(self.tree().payload) or {}).get("dialog")
        pid_after = self.adb.pidof(package)
        buttons = button_labels(dialog)
        dismissed = self.run_cli("ui", "tap", "--text", "Cancel", "--mode", "exact")
        self.check("s08_dialog_signal",
                   tap.ok and dialog_seen(dialog) and len(buttons) >= 2 and dialog_seen(again)
                   and pid_before is not None and pid_after == pid_before,
                   package=package, dialog=dialog, dialog_in_tap_reply=dialog_in_tap,
                   still_shown_after_reads=isinstance(again, dict), buttons=buttons,
                   app_pid_before=pid_before, app_pid_after=pid_after,
                   cancelled_by_script=dismissed.ok)

    def step09(self) -> None:
        home = self.home_via_cli()
        row = find_node(home.payload, NETWORK_ROW)
        if row is None or not isinstance(row.get("bounds"), list):
            raise self.screen_missing(f"no {NETWORK_ROW!r} row with bounds", home)
        shot = self.run_cli("screenshot", "--agent", "--label", "agent")
        self.timings["screenshot_cli_ms"] = shot.ms
        payload = shot.payload
        path = Path(str(payload.get("path") or ""))
        data = path.read_bytes() if path.is_file() else b""
        if data:
            self.save("s09-agent-screenshot.jpg", data)
        size = jpeg_size(data)
        width, height = payload.get("width"), payload.get("height")
        screen = payload.get("screen") or home.payload.get("screen")
        scale = payload.get("scale")
        expected = (max(screen) / max(width, height)
                    if isinstance(screen, list) and len(screen) == 2 and width and height else None)
        noted = note_scale(payload.get("coordinates"))
        point = center(row["bounds"])
        image_point = tap_point = None
        if isinstance(noted, float) and noted > 0:
            image_point, tap_point = image_round_trip(point, noted)
        on_row = tap_point is not None and inside(row["bounds"], tap_point)
        tap = None
        if on_row:
            tap = self.run_cli("ui", "tap", "--x", str(tap_point[0]), "--y", str(tap_point[1]))
        opened = tap is not None and NETWORK_LABEL in screen_outline(tap.payload)
        self.check("s09_agent_screenshot_tap",
                   shot.ok and payload.get("format") == "jpeg" and size is not None
                   and list(size) == [width, height] and max(size) <= 1000
                   and data.endswith(b"\xff\xd9") and isinstance(scale, (int, float))
                   and expected is not None and abs(scale - expected) < 0.01
                   and noted is not None and abs(noted - scale) < 0.01
                   and on_row and tap is not None and tap.ok and opened,
                   format=payload.get("format"), jpeg_size=list(size) if size else None,
                   reply_size=[width, height], bytes=len(data), screen=screen, scale=scale,
                   expected_scale=expected, coordinates=payload.get("coordinates"),
                   row_bounds=row["bounds"], row_center=list(point),
                   image_point=list(image_point) if image_point else None,
                   tap_point=list(tap_point) if tap_point else None, on_row=on_row,
                   page_opened=opened, shot_ms=shot.ms)

    def step10(self) -> None:
        old = [pid for pid in self.adb.helper_pids() if self.adb.is_helper(pid)]
        if not old:
            self.tree()  # start one, so there is something to kill
            old = [pid for pid in self.adb.helper_pids() if self.adb.is_helper(pid)]
        for pid in old:
            self.adb.shell(f"kill {pid}", check=False)
        gone = self.adb.wait_no_helper(old, 5)
        reply = self.tree()
        new = self.adb.helper_pids()
        launches = sum(1 for _, rest in reply.calls if is_helper_launch(rest))
        self.check("s10_restart_after_kill",
                   bool(old) and gone and reply.ok and reply.payload.get("ui_backend") == "helper"
                   and launches == 1 and len(new) == 1 and new[0] not in old,
                   killed=old, gone=gone, launches=launches, new_pids=new, cli_ms=reply.ms)

    def step11(self) -> None:
        build = self.record().get("build") or self.build
        if not build:
            raise StepFailed("no build id recorded")
        dex = f"{DEVICE_DIR}/autonom-helper-{build}.dex"
        old = [pid for pid in self.adb.helper_pids() if self.adb.is_helper(pid)]
        for pid in old:
            self.adb.shell(f"kill {pid}", check=False)
        self.adb.wait_no_helper(old, 5)
        stale = "stale-" + str(build)[:10]
        self.adb.shell(f"CLASSPATH={dex} nohup app_process /system/bin --nice-name={PROCESS} "
                       f"{MAIN_CLASS} --build {stale} --idle-ms {HELPER_IDLE_MS} "
                       f"--socket {SOCKET} >{DEVICE_LOG} 2>&1 &", timeout=30)
        # ready once its log (rewritten by this start) says it holds the
        # socket and UiAutomation
        ready = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready:
            log = self.adb.shell(f"cat {DEVICE_LOG}", check=False)
            ready = f"start build={stale} " in log and "uiautomation connected" in log
            if not ready:
                time.sleep(0.3)
        fresh = [pid for pid in self.adb.helper_pids() if pid not in old]
        stale_pid = fresh[0] if ready and len(fresh) == 1 else None
        reply = self.tree()
        status = self.run_cli("ui", "helper", "status")
        self.note_port(status.payload)
        new = self.adb.helper_pids()
        launches = sum(1 for _, rest in reply.calls if is_helper_launch(rest))
        self.check("s11_wrong_build_replaced",
                   stale_pid is not None and reply.ok and reply.payload.get("ui_backend") == "helper"
                   and launches == 1 and status.ok and status.payload.get("running") is True
                   and status.payload.get("build") == build and stale_pid not in new
                   and new == [status.payload.get("pid")],
                   stale_build=stale, stale_ready=ready, stale_pid=stale_pid, launches=launches,
                   status_build=status.payload.get("build"), status_pid=status.payload.get("pid"),
                   pids_after=new)

    def step12(self) -> None:
        self.budget.need(IDLE_WAIT_S + 120, "the idle wait")
        pids = self.adb.helper_pids()
        early = False
        while time.monotonic() - self.last_helper_call < IDLE_EARLY_S:
            if pids and not set(pids) & set(self.adb.helper_pids()):
                early = True
                break
            time.sleep(5)
        while time.monotonic() - self.last_helper_call < IDLE_WAIT_S:
            time.sleep(1)
        gone = self.adb.wait_no_helper(pids, 20) if pids else False
        waited = round(time.monotonic() - self.last_helper_call, 1)
        log = self.adb.shell(f"cat {DEVICE_LOG}", check=False)
        self.report["helper_log"] = self.save("helper.log", log)
        xml, problem = None, ""
        for _ in range(3):
            xml, problem = self.adb.dump()
            if xml:
                break
            time.sleep(2)
        if xml:
            self.save("s12-uiautomator-after-idle.xml", xml)
        nodes = xml.count("<node ") if xml else 0
        self.check("s12_idle_stop",
                   bool(pids) and not early and gone and not self.adb.helper_pids()
                   and "exit idle" in log and bool(xml) and nodes > 0,
                   helper_pids=pids, exited_early=early, gone=gone,
                   seconds_since_last_call=waited, log_says_idle="exit idle" in log,
                   dump_nodes=nodes, dump_problem=problem or None)

    def step13(self) -> None:
        reply = self.run_cli("ui", "tree", "--format", "json", bare=True)
        self.save("s13-fallback-tree.json", {key: value for key, value in reply.payload.items()
                                             if key != "nodes"})
        setup = [" ".join(rest) for _, rest in reply.calls if is_helper_setup(rest)]
        reason = str(reply.payload.get("fallback_reason") or "")
        keyboard = (signals_of(reply.payload) or {}).get("keyboard", "absent")
        self.check("s13_fallback_without_tools",
                   reply.ok and reply.payload.get("ui_backend") == "uiautomator"
                   and "javac" in reason and bool(reply.payload.get("fallback_hint"))
                   and not setup and bool(nodes_of(reply.payload)),
                   ui_backend=reply.payload.get("ui_backend"), fallback_reason=reason,
                   fallback_hint=reply.payload.get("fallback_hint"), helper_setup_calls=setup,
                   keyboard_signal=keyboard, cli_ms=reply.ms,
                   builds_in_second_home=self.builds(self.home_bare))

    def step14(self) -> None:
        tree = self.tree()
        launches = sum(1 for _, rest in tree.calls if is_helper_launch(rest))
        status = self.run_cli("ui", "helper", "status")
        self.note_port(status.payload)
        port = status.payload.get("port")
        own_line = f"{self.args.serial} tcp:{port} localabstract:{SOCKET}"
        listed_before_stop = own_line in self.adb.forwards()
        stop = self.run_cli("session", "stop")
        self.session_live = self.session_live and not stop.ok
        self.save("s14-session-stop.json", stop.payload)
        step = next((item for item in stop.payload.get("teardown") or []
                     if isinstance(item, dict) and item.get("action") == "android_helper"), None)
        detail = (step or {}).get("detail") or {}
        pids = self.adb.helper_pids()
        after = self.adb.forwards()
        verdict = forwards_verdict(self.forwards_before, after, self.args.serial,
                                   decoy=self.decoy_line, own_ports=self.own_ports,
                                   removals=adb_log_summary(self.log.calls(),
                                                            self.args.serial)["forward_removals"]
                                   + [" ".join(call[2:]) for call in self.adb.calls
                                      if call[2:4] == ["forward", "--remove"]])
        self.check("s14_session_stop_teardown",
                   tree.ok and launches == 1 and status.payload.get("running") is True
                   and listed_before_stop and stop.ok and bool(step) and step.get("ok") is True
                   and detail.get("stopped") is True and not pids and own_line not in after
                   and verdict["decoy_listed"] is True and not verdict["missing_this_serial"]
                   and not verdict["other_serials_removed_by_this_run"]
                   and not verdict["own_helper_forwards_left"],
                   restart_after_idle_launches=launches, own_forward=own_line,
                   own_forward_listed_before_stop=listed_before_stop,
                   own_forward_listed_after=own_line in after, teardown_step=step,
                   helper_pids_after=pids, forwards=verdict)

    # -- cleanup

    def cleanup_identity(self, cleanup: dict[str, Any]) -> str:
        """`verified` while the serial still names the AVD this run confirmed
        on it; `never verified` when the run stopped before confirming it
        (nothing is read from the serial then); `changed` when it now names
        another AVD, or none in three reads."""
        if not self.target_verified:
            return "never verified"
        name = None
        for attempt in range(3):
            try:
                name = self.adb.avd_name()
            except subprocess.TimeoutExpired:
                name = None
            if name == self.args.avd:
                return "verified"
            if name is not None:
                break  # a clear answer naming another AVD
            if attempt < 2:
                time.sleep(2)
        cleanup["avd_name_now"] = name
        return "changed"

    def cleanup_device(self, cleanup: dict[str, Any]) -> None:
        """The device side of the cleanup; only on the AVD this run confirmed."""
        if self.session_live:
            stop = self.cli("session", "stop", home=self.home)
            cleanup["session_stopped"] = stop.ok
            self.session_live = not stop.ok
        if self.adb.helper_pids():
            if self.record():
                cleanup["helper_stop"] = self.cli("ui", "helper", "stop", home=self.home).ok
            cleanup["killed_helper_pids"] = self.adb.kill_helpers()
        cleanup["helper_running_after"] = bool(self.adb.helper_pids())
        if self.decoy_port is not None:
            removed = self.adb.run("forward", "--remove", f"tcp:{self.decoy_port}",
                                   timeout=30, check=False)
            cleanup["decoy_removed"] = removed.returncode == 0
        listed = self.adb.shell(f"ls {DEVICE_DIR}", check=False).split()
        names = [name for name in listed
                 if (name.startswith("autonom-helper-") and name.endswith(".dex"))
                 or name == "autonom-helper.log"]
        for name in names:  # by exact name, never a glob on the device
            self.adb.shell(f"rm -f {DEVICE_DIR}/{name}", check=False)
        cleanup["device_files_removed"] = names
        left = self.adb.shell(f"ls {DEVICE_DIR}", check=False).split()
        cleanup["device_files_left"] = [name for name in left if name.startswith("autonom-helper")]
        airplane = self.adb.shell("settings get global airplane_mode_on", check=False).strip()
        cleanup["airplane_mode_was_on"] = airplane == "1"
        if airplane == "1":
            self.adb.shell("cmd connectivity airplane-mode disable", check=False)
            airplane = self.adb.shell("settings get global airplane_mode_on",
                                      check=False).strip()
        cleanup["airplane_mode_off"] = airplane == "0"
        after = self.adb.forwards()
        # read after `session stop` / `ui helper stop` above, which may have
        # removed Autonom's own forward
        cli_removals = adb_log_summary(self.log.calls(), self.args.serial)["forward_removals"]
        cleanup["forwards"] = forwards_verdict(
            self.forwards_before, after, self.args.serial, decoy=self.decoy_line,
            own_ports=self.own_ports, removals=cli_removals + self.script_forward_removals())

    def script_forward_removals(self) -> list[str]:
        return [" ".join(call[2:]) for call in self.adb.calls
                if call[2:4] == ["forward", "--remove"]]

    def cleanup(self) -> None:
        cleanup: dict[str, Any] = {}
        self.report["cleanup"] = cleanup
        try:
            identity = cleanup["device_identity"] = self.cleanup_identity(cleanup)
            if identity == "verified":
                self.cleanup_device(cleanup)
            else:  # never a command to a device whose AVD this run has not confirmed
                cleanup["device_steps_skipped"] = True
            cleanup["cli_adb"] = adb_log_summary(self.log.calls(), self.args.serial)
            script_serials = sorted({call[1] for call in self.adb.calls
                                     if call[:1] == ["-s"] and len(call) > 1})
            cleanup["script_adb"] = {"calls": len(self.adb.calls), "serials": script_serials,
                                     "remove_all": sum(1 for call in self.adb.calls
                                                       if "--remove-all" in call),
                                     "forward_removals": self.script_forward_removals()}
        except Exception as exc:  # noqa: BLE001 - the emulator must still be stopped
            cleanup["error"] = f"{type(exc).__name__}: {exc}"[-300:]
        if self.booted is not None:
            cleanup["emulator_stopped"] = self.stop_emulator()
        self.keep_evidence()
        shutil.rmtree(self.work, ignore_errors=True)
        verdict = cleanup.get("forwards") or {}
        cli_adb = cleanup.get("cli_adb") or {}
        script_adb = cleanup.get("script_adb") or {}
        identity = cleanup.get("device_identity")
        if identity == "verified":
            device_ok = bool(
                not cleanup.get("helper_running_after")
                and cleanup.get("decoy_removed", self.decoy_port is None)
                and verdict.get("decoy_listed") in (False, None)
                and not verdict.get("missing_this_serial")
                and not verdict.get("other_serials_removed_by_this_run")
                and not verdict.get("own_helper_forwards_left")
                and not cleanup.get("device_files_left") and cleanup.get("airplane_mode_off"))
        else:
            # nothing of this run reached a device it never confirmed; a device
            # that changed under the serial left this run's cleanup undone
            device_ok = identity == "never verified"
        cleanup["ok"] = bool(
            not cleanup.get("error") and device_ok
            and not cli_adb.get("foreign_serials") and not cli_adb.get("unscoped")
            and not cli_adb.get("remove_all") and not script_adb.get("remove_all")
            and script_adb.get("serials") in ([], [self.args.serial])
            and cleanup.get("emulator_stopped", True))
        self.check("s15_cleanup", cleanup["ok"],
                   **{key: value for key, value in cleanup.items() if key != "ok"})

    def keep_evidence(self) -> None:
        try:
            shutil.copyfile(self.log.path, self.evidence / ADB_LOG_NAME)
            record = self.record()
            if record:
                self.save("helper-device-record.json", record)
            self.report["builds"] = self.builds()
            self.report["cli_calls"] = self.cli.history
        except OSError as exc:
            self.report.setdefault("cleanup", {})["evidence_error"] = str(exc)

    def stop_emulator(self) -> bool:
        """The emulator this run booted: `emu kill` only while the serial
        names the AVD this run confirmed on it; else (never confirmed, or the
        serial now names another AVD) only our own child process, with no
        command to the device on the serial."""
        ours = False
        if self.target_verified:
            try:
                ours = self.adb.avd_name() == self.args.avd
                if ours:
                    self.adb.run("emu", "kill", timeout=30, check=False)
            except subprocess.TimeoutExpired:
                pass
        if ours:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self.booted.poll() is not None and self.args.serial not in self.adb.devices():
                    return True
                time.sleep(1)
        self.stop_child()
        if ours:
            return self.booted.poll() is not None and self.args.serial not in self.adb.devices()
        return self.booted.poll() is not None

    def stop_child(self) -> None:
        if self.booted is None or self.booted.poll() is not None:
            return
        self.booted.terminate()
        try:
            self.booted.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.booted.kill()
            self.booted.wait(timeout=10)


def find_emulator(adb: str) -> str | None:
    candidates = [os.environ.get("AUTONOM_EMULATOR"),
                  str(Path(adb).resolve().parent.parent / "emulator/emulator")]
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(name):
            candidates.append(str(Path(os.environ[name]) / "emulator/emulator"))
    candidates += [str(Path.home() / "Library/Android/sdk/emulator/emulator"), shutil.which("emulator")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", required=True, help="emulator-<port> of the bench AVD")
    parser.add_argument("--avd", required=True, help="the AVD name the serial must run")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--boot", action="store_true", help="boot the AVD cold when not running")
    parser.add_argument("--window", action="store_true", help="show the emulator window")
    parser.add_argument("--adb", default=os.environ.get("AUTONOM_ADB") or shutil.which("adb"))
    parser.add_argument("--emulator")
    parser.add_argument("--alert-app", help="an installed app whose Force-stop alert step 8 opens")
    parser.add_argument("--max-load", type=float, default=60.0)
    parser.add_argument("--load-wait", type=float, default=600.0)
    parser.add_argument("--boot-timeout", type=float, default=420.0)
    parser.add_argument("--time-limit", type=float, default=2400.0)
    args = parser.parse_args(argv)
    if not SERIAL.match(args.serial):
        parser.error("--serial must be emulator-<port>")
    if not AVD.match(args.avd):
        parser.error("--avd must be a plain AVD name")
    if not args.adb:
        parser.error("adb not found (pass --adb)")
    if args.alert_app and not PACKAGE.match(args.alert_app):
        parser.error("--alert-app must be a package name")
    args.adb = str(Path(args.adb).resolve()) if os.path.exists(args.adb) else args.adb
    return args


def environment_fault(code: int, report: dict[str, Any], failed: list[str], missing: list[str],
                      quiet: Any) -> bool:
    """True for a run whose only failures are timing checks on a device that
    did not go quiet in the whole wait: everything else, the cleanup
    included, passed, and no failed check broke off with an exception."""
    checks = report.get("checks") or {}
    return (code == 1 and "error" not in report and not missing and bool(failed)
            and set(failed) <= set(TIMING_CHECKS)
            and not any("error" in (checks.get(name) or {}) for name in failed)
            and isinstance(quiet, dict) and quiet.get("ok") is False
            and quiet.get("full_wait") is True
            and (report.get("checks", {}).get("s15_cleanup") or {}).get("ok") is True)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "ok": False, "serial": args.serial, "avd": args.avd, "started_at": now(),
        "expected_checks": list(EXPECTED_CHECKS), "checks": {}, "timings": {},
    }
    live = Live(args, report)

    def handler(signum: int, _frame: Any) -> None:
        raise Terminated(signum)

    previous = {signum: signal.signal(signum, handler) for signum in (signal.SIGTERM, signal.SIGHUP)}
    code = 1
    try:
        try:
            live.run()
        except Refused as exc:
            report["refused"] = str(exc)
            code = 2
        except Terminated as exc:
            report["terminated"] = signal.Signals(exc.signum).name
            code = 128 + exc.signum
        except Exception as exc:  # noqa: BLE001 - recorded; the cleanup still runs
            report["error"] = f"{type(exc).__name__}: {exc}"[-500:]
        finally:
            for signum in previous:  # the cleanup is not interrupted
                signal.signal(signum, signal.SIG_IGN)
            if "refused" not in report:
                live.cleanup()
            else:  # no device command: at most this run's own emulator process is stopped
                shutil.rmtree(live.work, ignore_errors=True)
                try:
                    live.stop_child()
                except Exception as exc:  # noqa: BLE001 - recorded; the report is still written
                    report["child_stop_error"] = f"{type(exc).__name__}: {exc}"[-300:]
            for signum, old in previous.items():
                signal.signal(signum, old)
    finally:
        checks = report["checks"]
        missing = [name for name in EXPECTED_CHECKS if name not in checks]
        report["missing_checks"] = missing if "refused" not in report else []
        report["load_end"] = host_load()
        report["finished_at"] = now()
        failed = [name for name, check in checks.items() if not check.get("ok")]
        quiet = (report.get("preflight") or {}).get("device_quiet")
        report["ok"] = (code == 1 and "error" not in report and not missing and not failed)
        if report["ok"]:
            code = 0
        elif environment_fault(code, report, failed, missing, quiet):
            code = EXIT_ENVIRONMENT
            report["environment_fault"] = ("the device never went quiet and only timing checks "
                                           "failed")
        report["exit_code"] = code
        out = evidence / REPORT_NAME
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"ok": report["ok"], "report": str(out), "failed": failed,
                          "missing": report["missing_checks"], "device_quiet": quiet,
                          "refused": report.get("refused")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
