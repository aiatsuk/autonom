#!/usr/bin/env python3
"""Live check of the iOS device daemon on a Simulator of its own (CASE-003, CASE-004).

Not part of the unit suite. Each run creates, boots, uses and deletes its own
Simulator, so it never touches a device another session uses:

    python3 tests/live/ios_daemon_live.py --part cli --create-simulator --evidence-dir DIR
    python3 tests/live/ios_daemon_live.py --part canvas --create-simulator --evidence-dir DIR

`--create-simulator` is required: the script never runs on an existing
Simulator. It creates "Autonom iosd live CLI <YYYYMMDD-HHMM>" (or "... Canvas
...") with `xcrun simctl create` (iPhone 17 Pro, iOS 27.0 unless
`--device-type` / `--runtime` say otherwise), boots it, and deletes it in a
finally block, also after a failure or SIGTERM. Every run uses its own
temporary AUTONOM_HOME (removed at the end), so the daemon registry, the
session and the Canvas it starts are its own, and sets
AUTONOM_IOSD_IDLE_MS (default 20000) so the idle stop can be watched.
`idb` is reached through a wrapper put first on PATH that logs its argv (one
JSON line) and runs the real idb, so the log shows every idb process a CLI
call (or the daemon) started. Independent reads use the real idb directly
(`idb ui describe-all --json`) and `ps`; nothing is decided from the CLI's
reply alone.

A booted Simulator shows the Home screen, so before each check that needs
the main Settings screen (tree_off and non_animating in --part cli,
one_companion and text_reuses in --part canvas) the run terminates and
launches com.apple.Preferences with simctl and reads `ui tree` every 500 ms
for up to 30 s until a 'Settings' heading and 'General' show; failed reads
are waited through. Before the daemon's first use the reads go through the
idb process (AUTONOM_IOS_DAEMON=off), later through the daemon. Each wait
(seconds, reads, failed reads) is in `settings_waits` and in the check's
`settings_wait`; a timeout fails that check.

--part cli (CASE-003, VER-003):

- tree_off / tree_on: one warm-up, then --runs (20) `ui tree` with
  AUTONOM_IOS_DAEMON=off (transport subprocess, transport_reason
  daemon_disabled) and then with the daemon on (transport daemon, ax_backend
  ax); General must be in every tree. Between the two the companion the idb
  process started for this Simulator is disconnected, so the daemon starts
  (and owns) its own. Median, p95 and host load are recorded.
- single_daemon: exactly one idb_companion whose --udid is this UDID and one
  ios-device-daemon.mjs for it, and the registry names that companion.
- push / back: --runs alternating `ui tap --desc General --observe` (About
  in the outline, input_confirmed true) and `ui tap --desc Settings --role
  button --mode exact --observe`, the Back button only (General in the
  outline, reply under 1 s). On iOS 27 the General page has more than one node
  whose desc holds 'Settings', so `--desc Settings` alone is ambiguous.
- non_animating: --runs `ui tap --x X --y Y --observe` at the centre of the
  "Settings" heading on the main screen: `changes` empty. The first tap's
  baseline is the saved tree of a read that matched the read before it
  (STABLE_GAP_S apart, up to STABLE_WAIT_S): right after the launch Settings
  still lists an offscreen row ("Screen Time") that it drops about 2 s later,
  so a read taken the moment the main screen shows is not the screen the tap
  leaves alone.
- empty_tap: whether an observed tap on empty space was confirmed (U-003):
  the heading's row, 6 points from the right edge of the screen width `ui
  tree` reports ([width, height]); the point must be outside the heading and
  every control, else the check fails without tapping.
- web_read: `simctl openurl` https://example.com/, then `ui tree --format
  outline` until a line holds "Example Domain"; ax_backend must be axbridge.
- idle_stop: `session stop` does not name the daemon's companion (pid from
  the registry) as left running in any place the reply carries it
  (warnings[companion_left_running].companions,
  teardown[session_processes].detail, session.process_teardown); 25 s later
  no daemon, no idb_companion for the UDID and no registry file are left.
- no_double_send: each tap reached the device once: the daemon wrote exactly
  one HID touch down/up pair for it (counted by a hook loaded into the
  daemon's own node process through AUTONOM_NODE, see HID_HOOK) and the idb
  log has no `ui tap` for it (or, for a tap idb served, one `ui tap` and no
  HID event). The daemon's tap/act request count is recorded beside it.
- scope: every idb call that names a UDID names this one.
- cleanup: the Simulator is gone from `simctl list` and nothing is left.

Speed targets (medians of the wall time of each CLI call): tree on 0.28 s,
non-animating tap 0.65 s, push 1.3 s, Back 0.75 s. The non-animating 0.65 s is
the measured 0.56-0.64 s plus a small margin, and Back 0.75 s is the measured
0.70-0.73 s plus a small margin (Back's floor is the 300 ms grace plus CLI
start); the owner approved both on 2026-10-25. Push 1.3 s and tree 0.28 s are
unchanged. A timing check records
`load` and `load_high` (above --max-load, default 60); a miss under high
load may be rerun once after the load drops.

Settle wait: booting a fresh Simulator alone drives the host 1-minute load
far above --max-load for minutes, so after `session start` (and before
tree_off's Settings wait in --part cli; after the Canvas streams and before
one_companion's Settings wait in --part canvas) the run reads the load
(os.getloadavg) every 5 s until 3 readings in a row have both the 1-minute
and the 5-minute load below --max-load, for at most --settle-timeout seconds
(never past the run's time budget; 0 takes one reading and does not wait).
The default is per part: 900 s for --part cli and 1100 s for --part canvas.
The budget math: each part's steps get its --time-limit less the 180 s
cleanup reserve, 1920 s for cli (2100 - 180) and 1520 s for canvas
(1700 - 180); in r523 everything but the settle wait took 245 s (cli) and
177 s (canvas), so 900 + 245 = 1145 s and 1100 + 177 = 1277 s both fit (243 s
of headroom for canvas; the plan's live-canvas gate times out at 1800 s, cli's
at 2400 s). The Canvas part's settle likely needs about 900-940 s after an r523-like boot
(with the Canvas streaming), hence its longer default. The 1-minute load
alone is not enough: in the r523 runs it dipped below 60 while the 5-minute
load was still 155-165 (the boot's tail), and the timed checks right after it
missed their targets by far. It is recorded in
`settle` (waited_s, load_at_start, load_at_end, load5_at_start, load5_at_end,
samples, timed_out). A timeout does not fail the run: the run goes on and the
timing checks keep their own load and load_high.

--part canvas (CASE-004, VER-004):

- canvas: `autonom canvas serve --single --transport idb` on a free local
  port in the temporary home, no browser: the script holds one /ws/video
  connection itself and counts frames with the Canvas's own /status
  (idb.packets); the Canvas must stream on the daemon's companion
  (idb.companion_source daemon).
- one_companion: `ui tree` and an observed tap while it streams; `ps` is
  sampled every 100 ms for the whole run and must show one companion for
  the UDID, and frames keep increasing.
- text_reuses: `ui type hi` into the Settings search field: still one
  companion during and after.
- restart: the companion pid listed in this run's daemon registry for this
  UDID (its command line checked to name idb_companion and this UDID) is
  killed; within 10 s one new companion runs and frames increase again.
  Nothing else is killed; companions of other Simulators are alive after it
  exactly as before.
- cleanup: the Canvas, the session, the daemon (idle stop) and the
  Simulator are gone.

Writes <evidence-dir>/ios-daemon-cli.json or ios-daemon-canvas.json with
every check (`ok` and what decided it), every CLI call (argv, exit code,
seconds, the idb calls it made), the host load and the daemon log's lifecycle
and timing events (`daemon_log`), and prints one JSON line
naming it. Exit 0 when every check passed, 1 otherwise, 2 for a usage error.
`--help` and usage errors touch no device. The whole run, cleanup included,
ends within --time-limit (default 2100 s for cli, 1700 s for canvas); SIGTERM
or SIGHUP stops the steps and still runs the cleanup, then exits 128 + the
signal number.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "autonom.py"

SETTINGS_APP = "com.apple.Preferences"
WEB_URL = "https://example.com/"
WEB_TEXT = "Example Domain"
DEFAULT_DEVICE_TYPE = "iPhone 17 Pro"
DEFAULT_RUNTIME = "iOS 27.0"
DEFAULT_RUNS = 20
DEFAULT_IDLE_MS = 20_000
IDLE_WAIT_S = 25
DEFAULT_MAX_LOAD = 60.0
# Per part (see the module docstring for the budget math).
DEFAULT_SETTLE_TIMEOUTS_S = {"cli": 900.0, "canvas": 1100.0}
SETTLE_POLL_S = 5.0
SETTLE_STABLE_SAMPLES = 3
TIME_LIMITS = {"cli": 2100, "canvas": 1700}
CLEANUP_RESERVE_S = 180
CLI_TIMEOUT = 120
BOOT_TIMEOUT = 300
BACK_LIMIT_S = 1.0
RESTART_LIMIT_S = 10.0
SAMPLE_INTERVAL_S = 0.1
TARGETS_S = {"tree_on": 0.28, "non_animating": 0.65, "push": 1.3, "back": 0.75}
DAEMON_SCRIPT_NAME = "ios-device-daemon.mjs"
CANVAS_SCRIPT_NAME = "android-emulator-browser.mjs"
EXPECTED_CHECKS = {
    "cli": ("simulator", "session", "tree_off", "tree_on", "single_daemon", "push", "back",
            "non_animating", "empty_tap", "web_read", "idle_stop", "no_double_send", "scope",
            "cleanup"),
    "canvas": ("simulator", "session", "canvas", "one_companion", "text_reuses", "restart",
               "cleanup"),
}
# Overrides of the caller's environment that would point the CLI at other
# binaries, another backend, another companion, another home or a claim.
SCRUBBED_ENV = (
    "AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_EMULATOR", "AUTONOM_AXE",
    "AUTONOM_IOS_HID", "AUTONOM_UI_BACKEND", "AUTONOM_IDB_COMPANION", "AUTONOM_IOS_DAEMON",
    "AUTONOM_IDB_STATE_FILE", "AUTONOM_CONTROL_HOLDER", "AUTONOM_CONTROL_GENERATION",
    "AUTONOM_IOSD_IDLE_MS", "AUTONOM_NODE",
)


class StepFailed(Exception):
    pass


class OutOfTime(StepFailed):
    """The run's time limit leaves no time for a step; it was not started."""


class Terminated(BaseException):
    """SIGTERM or SIGHUP: raised so that the cleanup in main still runs."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"stopped by {signal.Signals(signum).name}")
        self.signum = signum


class Budget:
    """Each step's timeout is cut to what is left before the cleanup reserve;
    after `end_steps` the cleanup calls use their own short timeouts."""

    def __init__(self) -> None:
        self.steps_until: float | None = None

    def start(self, limit_s: float) -> None:
        self.steps_until = time.monotonic() + limit_s - CLEANUP_RESERVE_S

    def end_steps(self) -> None:
        self.steps_until = None

    def clamp(self, timeout: float) -> float:
        if self.steps_until is None:
            return timeout
        left = self.steps_until - time.monotonic()
        if left < 1:
            raise OutOfTime("the run's time limit leaves no time for this step; it was not started")
        return min(timeout, left)


BUDGET = Budget()


def _raise_terminated(signum: int, _frame: Any) -> None:
    raise Terminated(signum)


def _note_late_signal(signum: int, _frame: Any) -> None:
    """During the cleanup a second signal does not cut it short."""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def host_load() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def host_loads() -> tuple[float, float] | None:
    """The host's 1-minute and 5-minute load, or None when unreadable."""
    try:
        one, five, _fifteen = os.getloadavg()
    except OSError:
        return None
    return round(one, 2), round(five, 2)


def settle(report: dict[str, Any], max_load: float, timeout_s: float, purpose: str) -> None:
    """Wait until the host's 1-minute and 5-minute loads (`host_loads`) have
    both been below ``max_load`` for SETTLE_STABLE_SAMPLES readings in a row,
    SETTLE_POLL_S apart, for at most ``timeout_s`` (and never past the steps'
    time budget). A dip of the 1-minute load alone is not a settled host (r523:
    1-minute 47-54 with the 5-minute still 155-165). A timeout does not fail
    the run: the timing checks keep their own load/load_high and the wait is
    recorded in report["settle"]."""
    started = time.monotonic()
    deadline = started + max(0.0, timeout_s)
    budget_capped = BUDGET.steps_until is not None and BUDGET.steps_until - 1 < deadline
    if budget_capped:
        deadline = BUDGET.steps_until - 1
    samples: list[float | None] = []
    samples5: list[float | None] = []
    below = 0
    settled = False
    while True:
        loads = host_loads()
        samples.append(loads[0] if loads else None)
        samples5.append(loads[1] if loads else None)
        if loads is None:
            break  # no load reading on this host: nothing to wait for
        below = below + 1 if loads[0] < max_load and loads[1] < max_load else 0
        if below >= SETTLE_STABLE_SAMPLES:
            settled = True
            break
        if time.monotonic() + SETTLE_POLL_S > deadline:
            break
        time.sleep(SETTLE_POLL_S)
    report["settle"] = {"purpose": purpose, "waited_s": round(time.monotonic() - started, 1),
                        "load_at_start": samples[0], "load_at_end": samples[-1],
                        "load5_at_start": samples5[0], "load5_at_end": samples5[-1],
                        "load5_max": max((v for v in samples5 if v is not None), default=None),
                        "criterion": "load_1m and load_5m below max_load",
                        "samples": len(samples),
                        "timed_out": not settled and samples[-1] is not None,
                        "load_readable": samples[-1] is not None, "max_load": max_load,
                        "timeout_s": timeout_s, "stable_samples": SETTLE_STABLE_SAMPLES,
                        "poll_s": SETTLE_POLL_S, "budget_capped": budget_capped}


def run(argv: list[str], *, timeout: float, env: dict[str, str] | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               timeout=BUDGET.clamp(timeout), env=env, check=False)
    if check and completed.returncode != 0:
        raise StepFailed(f"{' '.join(argv[:5])} failed ({completed.returncode}): "
                         f"{(completed.stderr or completed.stdout).strip()[-300:]}")
    return completed


def summary_stats(values: list[float]) -> dict[str, Any]:
    """Median and p95 (nearest rank) of ``values`` in seconds."""
    if not values:
        return {"n": 0, "median_s": None, "p95_s": None, "min_s": None, "max_s": None}
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return {"n": len(ordered), "median_s": round(statistics.median(ordered), 3),
            "p95_s": round(ordered[rank - 1], 3), "min_s": round(ordered[0], 3),
            "max_s": round(ordered[-1], 3)}


def timing_result(name: str, values: list[float], max_load: float) -> dict[str, Any]:
    stats = summary_stats(values)
    target = TARGETS_S.get(name)
    load = host_load()
    result: dict[str, Any] = {**stats, "load": load,
                              "load_high": load is not None and load > max_load}
    if target is not None:
        result["target_median_s"] = target
        result["median_ok"] = stats["median_s"] is not None and stats["median_s"] <= target
    return result


# --- processes ---------------------------------------------------------------------


def process_table() -> list[tuple[int, list[str]]]:
    """(pid, argv words) of every process `ps` lists."""
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True,
                             timeout=10, check=False)
    rows = []
    for line in listing.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1].split()))
    return rows


def _names_udid(words: list[str], udid: str) -> bool:
    return any(word == "--udid" and index + 1 < len(words) and words[index + 1] == udid
               for index, word in enumerate(words))


def companions(udid: str, table: list[tuple[int, list[str]]] | None = None) -> list[int]:
    """Pids whose executable is idb_companion and whose --udid is ``udid``."""
    rows = process_table() if table is None else table
    return sorted(pid for pid, words in rows
                  if words and os.path.basename(words[0]) == "idb_companion"
                  and _names_udid(words, udid))


def other_companions(udid: str, table: list[tuple[int, list[str]]] | None = None) -> list[int]:
    rows = process_table() if table is None else table
    return sorted(pid for pid, words in rows
                  if words and os.path.basename(words[0]) == "idb_companion"
                  and not _names_udid(words, udid))


def daemons(udid: str, table: list[tuple[int, list[str]]] | None = None) -> list[int]:
    """Pids of node processes running ios-device-daemon.mjs for ``udid`` (not
    the Python supervisor around one)."""
    rows = process_table() if table is None else table
    return sorted(pid for pid, words in rows
                  if words and os.path.basename(words[0]).startswith("node")
                  and any(word.endswith(DAEMON_SCRIPT_NAME) for word in words)
                  and _names_udid(words, udid))


def canvases(udid: str, port: int, table: list[tuple[int, list[str]]] | None = None) -> list[int]:
    rows = process_table() if table is None else table
    return sorted(pid for pid, words in rows
                  if any(word.endswith(CANVAS_SCRIPT_NAME) for word in words)
                  and udid in words and str(port) in words)


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def wait_gone(pids: list[int], seconds: float) -> list[int]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        left = [pid for pid in pids if pid_alive(pid)]
        if not left:
            return []
        time.sleep(0.1)
    return [pid for pid in pids if pid_alive(pid)]


class Sampler(threading.Thread):
    """`ps` every 100 ms: how many companions run for the UDID."""

    def __init__(self, udid: str) -> None:
        super().__init__(daemon=True)
        self.udid = udid
        self.samples: list[tuple[float, int, list[int]]] = []
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.started_at = time.monotonic()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                pids = companions(self.udid)
            except (OSError, subprocess.SubprocessError):
                pids = None
            if pids is not None:
                with self.lock:
                    self.samples.append((round(time.monotonic() - self.started_at, 3),
                                         len(pids), pids))
            self.stopped.wait(SAMPLE_INTERVAL_S)

    def mark(self) -> int:
        with self.lock:
            return len(self.samples)

    def since(self, mark: int) -> list[tuple[float, int, list[int]]]:
        with self.lock:
            return list(self.samples[mark:])

    def stop(self) -> None:
        self.stopped.set()
        self.join(timeout=5)


def counts(samples: list[tuple[float, int, list[int]]]) -> dict[str, Any]:
    values = [count for _t, count, _pids in samples]
    return {"samples": len(values), "max": max(values) if values else None,
            "min": min(values) if values else None,
            "pids": sorted({pid for _t, _count, pids in samples for pid in pids})}


# --- idb argv log --------------------------------------------------------------------


def write_wrapper(directory: Path, real: str, log: Path) -> Path:
    path = directory / "idb"
    path.write_text(
        f"#!{sys.executable} -I\n"
        "# Logs every idb call of the live iOS daemon check, then runs the real idb.\n"
        "import json, os, sys, time\n"
        f"LOG = {str(log)!r}\n"
        f"REAL = {real!r}\n"
        "with open(LOG, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'t': round(time.time(), 3), 'argv': sys.argv[1:]}) + '\\n')\n"
        "os.execv(REAL, [REAL, *sys.argv[1:]])\n",
        encoding="utf-8")
    path.chmod(0o755)
    return path


class IdbLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.touch()

    def entries(self) -> list[list[str]]:
        found = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("argv"), list):
                found.append([str(word) for word in entry["argv"]])
        return found

    def mark(self) -> int:
        return len(self.entries())

    def since(self, mark: int) -> list[list[str]]:
        return self.entries()[mark:]


# --- the daemon's HID writes ---------------------------------------------------------
#
# VER-003 counts the HID events the daemon sends per tap. The daemon is
# started by the CLI with AUTONOM_NODE, which the run points at a wrapper:
# for ios-device-daemon.mjs only, it adds `--import <hook>` and execs the
# real node (same pid, same command line apart from the flag). The hook wraps
# IdbHidStream.prototype.send in the daemon's own process and appends one
# JSON line per HID event written to the companion's stream, before the
# daemon goes on, so every event of a tap is logged by the time its reply
# reaches the CLI. Nothing in the daemon or the product code changes.

HID_HOOK = """\
// Logs every HID event the iOS device daemon writes (live check only).
import { appendFileSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const LOG = %(log)s;
const main = process.argv[1] || "";
const write = (fields) => {
  try {
    appendFileSync(LOG, JSON.stringify({ t: Date.now() / 1000, pid: process.pid, ...fields }) + "\\n");
  } catch {
    // best effort: a missing line fails the check, it never stops the daemon
  }
};
if (main.endsWith(%(daemon)s)) {
  try {
    const companion = realpathSync(join(dirname(realpathSync(main)), "ios-idb-companion.mjs"));
    const { IdbHidStream } = await import(pathToFileURL(companion).href);
    const send = IdbHidStream.prototype.send;
    IdbHidStream.prototype.send = function liveCheckSend(event) {
      return send.call(this, event).then((value) => {
        const kind = event?.touch ? "touch" : event?.button !== undefined ? "button"
          : event?.key !== undefined ? "key" : event?.pinch ? "pinch" : "other";
        write({ kind, direction: event?.direction ?? null });
        return value;
      });
    };
    write({ kind: "hook_loaded" });
  } catch (error) {
    write({ kind: "hook_failed", error: String(error?.message ?? error) });
  }
}
"""


def write_node_wrapper(directory: Path, real: str, log: Path) -> Path:
    hook = directory / "hid-hook.mjs"
    hook.write_text(HID_HOOK % {"log": json.dumps(str(log)), "daemon": json.dumps(DAEMON_SCRIPT_NAME)},
                    encoding="utf-8")
    path = directory / "node"
    path.write_text(
        f"#!{sys.executable} -I\n"
        "# Runs the real node; for the iOS device daemon it first loads the HID hook.\n"
        "import os, sys\n"
        f"REAL = {real!r}\n"
        f"HOOK = {hook.resolve().as_uri()!r}\n"
        f"DAEMON = {DAEMON_SCRIPT_NAME!r}\n"
        "args = sys.argv[1:]\n"
        "if any(word.endswith(DAEMON) for word in args):\n"
        "    args = ['--import', HOOK, *args]\n"
        "os.execv(REAL, [REAL, *args])\n",
        encoding="utf-8")
    path.chmod(0o755)
    return path


class HidLog:
    """The hook's lines: {kind: touch|button|key|pinch|other, direction} per
    HID event, plus hook_loaded / hook_failed when a daemon starts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.touch()

    def entries(self) -> list[dict[str, Any]]:
        found = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return found
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                found.append(entry)
        return found

    def loaded(self) -> bool:
        """A daemon of this run loaded the hook, and none failed to."""
        kinds = [entry.get("kind") for entry in self.entries()]
        return "hook_loaded" in kinds and "hook_failed" not in kinds

    def mark(self) -> int:
        return len(self.entries())

    def since(self, mark: int) -> list[dict[str, Any]]:
        return self.entries()[mark:]


def idb_verb(argv: list[str]) -> str:
    """`ui tap`, `connect`, ...: the verb after idb's top-level options."""
    words = list(argv)
    while words and words[0].startswith("--"):
        words = words[2:]
    if not words:
        return ""
    return " ".join(words[:2]) if words[0] in ("ui", "crash", "file") and len(words) > 1 else words[0]


def udid_named(argv: list[str]) -> str | None:
    for index, word in enumerate(argv[:-1]):
        if word == "--udid":
            return argv[index + 1]
    if idb_verb(argv) in ("connect", "disconnect") and len(argv) >= 2:
        return argv[-1]
    return None


# --- the daemon's own socket ---------------------------------------------------------


def registry(home: Path, udid: str) -> dict[str, Any] | None:
    try:
        payload = json.loads((home / "iosd" / f"{udid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


_IDS = iter(range(1, 1_000_000))


def daemon_request(home: Path, udid: str, op: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """One request to this run's daemon (its registry names the socket), or None."""
    record = registry(home, udid)
    path = (record or {}).get("socket")
    if not isinstance(path, str):
        return None
    message = {"id": f"live-{next(_IDS)}", "op": op}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
        client.sendall((json.dumps(message) + "\n").encode("utf-8"))
        data = b""
        deadline = time.monotonic() + timeout
        while b"\n" not in data and time.monotonic() < deadline:
            chunk = client.recv(1 << 20)
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        client.close()
    try:
        reply = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except ValueError:
        return None
    return reply if isinstance(reply, dict) else None


def input_requests(home: Path, udid: str) -> int | None:
    """How many tap and act requests the daemon has served."""
    reply = daemon_request(home, udid, "status")
    by_op = (reply or {}).get("requests_by_op")
    if not isinstance(by_op, dict):
        return None
    return int(by_op.get("tap") or 0) + int(by_op.get("act") or 0)


# --- the CLI under test ------------------------------------------------------------


def parse_reply(stdout: str, stderr: str) -> dict[str, Any]:
    for stream in (stdout, stderr):
        text = stream.strip()
        if not text:
            continue
        for candidate in (text, text.splitlines()[-1]):
            try:
                value = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return {"ok": False, "error": (stderr or stdout).strip()[-300:]}


class Reply:
    def __init__(self, code: int | None, payload: dict[str, Any], seconds: float,
                 idb_calls: list[list[str]]) -> None:
        self.code = code
        self.payload = payload
        self.seconds = seconds
        self.idb_calls = idb_calls

    @property
    def ok(self) -> bool:
        return self.code == 0 and self.payload.get("ok") is True

    def outline(self) -> str:
        screen = self.payload.get("screen")
        if isinstance(screen, dict) and isinstance(screen.get("outline"), str):
            return screen["outline"]
        value = self.payload.get("outline")
        return value if isinstance(value, str) else ""

    def nodes(self) -> list[dict[str, Any]]:
        return [node for node in self.payload.get("nodes") or [] if isinstance(node, dict)]

    def warning_codes(self) -> list[str]:
        return [str(item.get("code")) for item in self.payload.get("warnings") or []
                if isinstance(item, dict)]


class Cli:
    """`python3 scripts/autonom.py <verb> ... --platform ios --target UDID` in the
    temporary home; every call is recorded for the evidence."""

    def __init__(self, udid: str, env: dict[str, str], log: IdbLog) -> None:
        self.udid = udid
        self.env = env
        self.log = log
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, *argv: str, target: bool = True,
                 extra_env: dict[str, str] | None = None, timeout: float = CLI_TIMEOUT) -> Reply:
        command = [sys.executable, str(CLI), *argv]
        if target:
            command += ["--platform", "ios", "--target", self.udid]
        env = {**self.env, **(extra_env or {})}
        timeout = BUDGET.clamp(timeout)
        mark = self.log.mark()
        started = time.monotonic()
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       stdin=subprocess.DEVNULL, timeout=timeout, env=env,
                                       check=False)
            stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr, code = "", "", None
        seconds = round(time.monotonic() - started, 3)
        idb_calls = self.log.since(mark)
        payload = parse_reply(stdout, stderr) if code is not None else {
            "ok": False, "error": f"no reply within {timeout:g} s"}
        self.calls.append({
            "name": name, "argv": command[2:], "exit": code, "seconds": seconds,
            "ok": payload.get("ok"), "error_code": payload.get("error_code"),
            "transport": payload.get("transport"),
            "transport_reason": payload.get("transport_reason"),
            "ax_backend": payload.get("ax_backend"),
            "idb_calls": [idb_verb(argv_) for argv_ in idb_calls],
            **({"daemon_off": True} if (extra_env or {}).get("AUTONOM_IOS_DAEMON") == "off" else {}),
        })
        return Reply(code, payload, seconds, idb_calls)


def guarded(report: dict[str, Any], names: tuple[str, ...],
            body: Callable[[], dict[str, dict[str, Any]]]) -> None:
    """Run ``body`` (one result per check name); a failure fails every check it
    was to decide, and the next checks still run."""
    started = time.monotonic()
    try:
        results = body()
    except Exception as exc:  # noqa: BLE001 - live evidence records every failure
        error = f"{type(exc).__name__}: {exc}"[-400:]
        results = {name: {"ok": False, "error": error} for name in names}
    seconds = round(time.monotonic() - started, 1)
    for name in names:
        result = results.get(name) or {"ok": False, "error": "not decided"}
        result["ok"] = bool(result.get("ok"))
        result.setdefault("seconds", seconds)
        report["checks"][name] = result


def labels_of(nodes: list[dict[str, Any]]) -> set[str]:
    found = set()
    for node in nodes:
        for key in ("desc", "text"):
            if isinstance(node.get(key), str):
                found.add(node[key])
    return found


def outline_has(outline: str, label: str) -> bool:
    return any(f'"{label}"' in line for line in outline.splitlines())


def changes_empty(payload: dict[str, Any]) -> bool:
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return False
    return all(int(changes.get(key) or 0) == 0 for key in ("added", "removed", "changed"))


def centre(bounds: Any) -> tuple[int, int] | None:
    if isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(v, int) for v in bounds):
        left, top, right, bottom = bounds
        if right > left and bottom > top:
            return (left + right) // 2, (top + bottom) // 2
    return None


# Roles that only group other nodes: a point inside one of them can still be empty space.
CONTAINER_ROLES = ("app", "window", "node", "group", "scroll", "list", "navbar", "other")


def contains(bounds: Any, point: tuple[int, int]) -> bool:
    if not (isinstance(bounds, list) and len(bounds) == 4
            and all(isinstance(v, (int, float)) for v in bounds)):
        return False
    left, top, right, bottom = bounds
    return left <= point[0] <= right and top <= point[1] <= bottom


def screen_width(payload: dict[str, Any], nodes: list[dict[str, Any]]) -> int | None:
    """The width `ui tree` reports: `screen` is [width, height] on both
    platforms (a dict with `width` is accepted too); without it, the root
    app/window node's width."""
    screen = payload.get("screen")
    if (isinstance(screen, list) and len(screen) == 2 and isinstance(screen[0], int)
            and screen[0] > 0):
        return screen[0]
    if isinstance(screen, dict) and isinstance(screen.get("width"), int) and screen["width"] > 0:
        return screen["width"]
    for node in nodes:
        bounds = node.get("bounds")
        if node.get("role") in ("app", "window") and centre(bounds):
            return bounds[2] - bounds[0]
    return None


def pids_of(items: Any) -> list[Any]:
    """Pids from a list of {pid: ...} records or bare pids."""
    found: list[Any] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            found.append(item.get("pid"))
        elif isinstance(item, int) and not isinstance(item, bool):
            found.append(item)
    return found


def companions_left_running(payload: dict[str, Any]) -> list[Any]:
    """Every companion pid a `session stop` reply says it left running, from
    each place the reply can carry it: warnings[code=companion_left_running]
    .companions, teardown[action=session_processes].detail
    .companion_left_running, session.process_teardown.companion_left_running,
    and top-level process_teardown / companion_left_running."""
    left: list[Any] = []
    for warning in payload.get("warnings") or []:
        if isinstance(warning, dict) and warning.get("code") == "companion_left_running":
            left += pids_of(warning.get("companions"))
    for step in payload.get("teardown") or []:
        if isinstance(step, dict) and step.get("action") == "session_processes":
            detail = step.get("detail")
            if isinstance(detail, dict):
                left += pids_of(detail.get("companion_left_running"))
    session = payload.get("session")
    holders = [payload.get("process_teardown")]
    if isinstance(session, dict):
        holders.append(session.get("process_teardown"))
    for holder in holders:
        if isinstance(holder, dict):
            left += pids_of(holder.get("companion_left_running"))
    left += pids_of(payload.get("companion_left_running"))
    unique: list[Any] = []
    for pid in left:
        if pid is not None and pid not in unique:
            unique.append(pid)
    return unique


# --- the Simulator -----------------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.udid: str | None = None
        self.name: str | None = None
        self.home: Path | None = None
        self.cli: Cli | None = None
        self.session_started = False
        self.canvas: subprocess.Popen | None = None
        self.canvas_port: int | None = None
        self.canvas_log: Any = None
        self.video: VideoClient | None = None
        self.sampler: Sampler | None = None
        self.hid_log: HidLog | None = None


def simctl(args: argparse.Namespace, *words: str, timeout: float = 60,
           check: bool = True) -> subprocess.CompletedProcess:
    return run([args.xcrun, "simctl", *words], timeout=timeout, check=check)


def create_simulator(args: argparse.Namespace, state: State, report: dict[str, Any]) -> None:
    types = json.loads(simctl(args, "list", "devicetypes", "-j").stdout or "{}").get("devicetypes", [])
    runtimes = json.loads(simctl(args, "list", "runtimes", "-j").stdout or "{}").get("runtimes", [])
    device_type = next((item for item in types if item.get("name") == args.device_type), None)
    runtime = next((item for item in runtimes if item.get("name") == args.runtime
                    and item.get("isAvailable", True) is not False), None)
    if device_type is None or runtime is None:
        raise StepFailed(f"device type {args.device_type!r} or runtime {args.runtime!r} "
                         "is not available")
    label = "CLI" if args.part == "cli" else "Canvas"
    state.name = f"Autonom iosd live {label} {time.strftime('%Y%m%d-%H%M')}"
    created = simctl(args, "create", state.name, device_type["identifier"],
                     runtime["identifier"], timeout=120)
    udid = created.stdout.strip().splitlines()[-1].strip() if created.stdout.strip() else ""
    if not udid:
        raise StepFailed("simctl create printed no UDID")
    state.udid = udid
    started = time.monotonic()
    simctl(args, "boot", udid, timeout=120)
    simctl(args, "bootstatus", udid, "-b", timeout=BOOT_TIMEOUT)
    report["simulator"] = {"udid": udid, "name": state.name, "device_type": args.device_type,
                           "runtime": args.runtime, "runtime_build": runtime.get("buildversion"),
                           "boot_s": round(time.monotonic() - started, 1)}
    report["checks"]["simulator"] = {"ok": True, "udid": udid, "name": state.name}


def simulator_listed(args: argparse.Namespace, udid: str) -> bool | None:
    try:
        listing = subprocess.run([args.xcrun, "simctl", "list", "devices", "-j"],
                                 capture_output=True, text=True, timeout=60, check=False)
        devices = json.loads(listing.stdout or "{}").get("devices", {})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return any(item.get("udid") == udid for group in devices.values() for item in group)


def versions(args: argparse.Namespace) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, argv in (("xcode", ["xcodebuild", "-version"]),
                      ("idb_companion", ["idb_companion", "--version"]),
                      ("node", ["node", "--version"])):
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
            found[key] = (completed.stdout or completed.stderr).strip().splitlines()[:2]
        except (OSError, subprocess.SubprocessError):
            found[key] = None
    found["idb"] = args.idb
    found["python"] = sys.version.split()[0]
    return found


def real_node() -> str | None:
    """The node the CLI would run the daemon with (AUTONOM_NODE, else PATH)."""
    explicit = os.environ.get("AUTONOM_NODE")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("node")


def base_env(home: Path, bin_dir: Path, idle_ms: int, node: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    for name in SCRUBBED_ENV:
        env.pop(name, None)
    if node is not None:
        env["AUTONOM_NODE"] = str(node)
    env["AUTONOM_HOME"] = str(home)
    env["AUTONOM_IOSD_IDLE_MS"] = str(idle_ms)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def release_companions(args: argparse.Namespace, udid: str) -> dict[str, Any]:
    """End every companion for this run's own Simulator: `idb disconnect`, then
    SIGTERM and SIGKILL of what `ps` still shows for this UDID only."""
    before = companions(udid)
    result: dict[str, Any] = {"before": before, "signalled": []}
    if not before:
        return result
    subprocess.run([args.idb, "disconnect", udid], capture_output=True, text=True,
                   timeout=20, check=False)
    left = wait_gone(before, 3)
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for pid in left:
            if pid in companions(udid):
                try:
                    os.kill(pid, signum)
                    result["signalled"].append([pid, signal.Signals(signum).name])
                except OSError:
                    pass
        left = wait_gone(left, 3)
    result["after"] = companions(udid)
    return result


def independent_labels(args: argparse.Namespace, udid: str) -> set[str]:
    """Labels of a separate `idb ui describe-all --json` (the real idb)."""
    completed = run([args.idb, "ui", "describe-all", "--json", "--udid", udid], timeout=60)
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return set()
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("AXLabel", "AXValue", "label", "title"):
                if isinstance(value.get(key), str):
                    found.add(value[key])
            for child in value.values():
                if isinstance(child, (list, dict)):
                    walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


# --- Settings on its main screen -----------------------------------------------------
#
# A freshly booted Simulator shows the Home screen, so every check that needs
# Settings first (re)launches it and waits for its main screen with
# `open_settings`. Which tree read the wait uses matters:
#
# - Before the daemon's first use (part cli, before tree_off) the reads go
#   through the idb process (AUTONOM_IOS_DAEMON=off), so the daemon does not
#   start early. That idb process may leave a companion for the UDID, the same
#   as tree_off's own runs; `companions_before_daemon` ends it before tree_on,
#   so the daemon still starts (and owns) its own companion. tree_off still
#   counts only its own runs.
# - Once the daemon runs (part cli after push/back, part canvas while the
#   Canvas streams) the reads go through the daemon (the default path): a read
#   through the idb process would start a second companion for the UDID, which
#   one_companion, text_reuses, restart and idle_stop would count. The
#   daemon's tree requests do not enter the tap/act or HID counts of
#   no_double_send, and each check takes its own marks after the wait.
#
# Every read of the wait is recorded in `calls` under its own name, and each
# wait in report["settings_waits"] and in the result of the check it precedes
# (`settings_wait`).

SETTINGS_WAIT_S = 30.0
SETTINGS_POLL_S = 0.5


def settings_main_screen(nodes: list[dict[str, Any]]) -> bool:
    """The main Settings screen: the 'Settings' heading (as `heading` in part
    cli picks it: desc 'Settings', not a button, navbar, app or window) and a
    node whose desc is 'General'."""
    heading = any(node.get("desc") == "Settings" and centre(node.get("bounds"))
                  and node.get("role") not in ("button", "navbar", "app", "window")
                  for node in nodes)
    return heading and any(node.get("desc") == "General" for node in nodes)


STABLE_GAP_S = 1.0
STABLE_WAIT_S = 15.0


def tree_signature(nodes: list[dict[str, Any]]) -> list[tuple]:
    """What observe's diff compares of each node: identity and outline flags."""
    return [(node.get("role"), node.get("resource_id"), node.get("text"), node.get("desc"),
             tuple(node.get("bounds") or ()), node.get("visible"), node.get("enabled"),
             node.get("checked"), node.get("selected"), node.get("focused")) for node in nodes]


def stable_tree(cli: "Cli", name: str) -> tuple["Reply", bool]:
    """`ui tree --format json` every STABLE_GAP_S until a read matches the one
    before it (tree_signature), for at most STABLE_WAIT_S: (the last read,
    whether it matched). The last read is the session's saved tree, the
    baseline of the next observed coordinate tap."""
    deadline = time.monotonic() + STABLE_WAIT_S
    previous = None
    while True:
        reply = cli(name, "ui", "tree", "--format", "json")
        signature = tree_signature(reply.nodes()) if reply.ok else None
        if signature is not None and signature == previous:
            return reply, True
        previous = signature
        if time.monotonic() + STABLE_GAP_S > deadline:
            return reply, False
        time.sleep(STABLE_GAP_S)


def open_settings(args: argparse.Namespace, state: State, report: dict[str, Any], purpose: str,
                  *, daemon_off: bool) -> dict[str, Any]:
    """Terminate and launch Settings on this run's Simulator, then read the
    tree every 500 ms for up to 30 s until its main screen shows. Failed reads
    (for example 'No translation object returned for simulator' right after
    the launch) are counted and waited through. Raises StepFailed on timeout."""
    cli, udid = state.cli, state.udid
    assert cli is not None and udid is not None
    extra = {"AUTONOM_IOS_DAEMON": "off"} if daemon_off else None
    # Not running is fine: terminate only makes the launch start a fresh main screen.
    terminated = simctl(args, "terminate", udid, SETTINGS_APP, timeout=30, check=False)
    simctl(args, "launch", udid, SETTINGS_APP, timeout=60)
    started = time.monotonic()
    deadline = started + SETTINGS_WAIT_S
    reads = failed = 0
    ready = False
    last_error: str | None = None
    last_nodes = 0
    while True:
        reads += 1
        reply = cli(f"ui tree (wait for Settings, {purpose})", "ui", "tree", "--format", "json",
                    extra_env=extra, timeout=max(5.0, deadline - time.monotonic()))
        nodes = reply.nodes()
        last_nodes = len(nodes)
        if reply.ok and settings_main_screen(nodes):
            ready = True
            break
        if not reply.ok:
            failed += 1
            words = [str(reply.payload[key]) for key in ("error_code", "error")
                     if reply.payload.get(key)]
            last_error = (": ".join(words) or f"exit {reply.code}")[-200:]
        if time.monotonic() + SETTINGS_POLL_S >= deadline:
            break
        time.sleep(SETTINGS_POLL_S)
    entry = {"purpose": purpose, "daemon_off": daemon_off, "terminate_exit": terminated.returncode,
             "waited_s": round(time.monotonic() - started, 2), "reads": reads,
             "failed_reads": failed, "last_error": last_error, "nodes_last": last_nodes,
             "ready": ready}
    report.setdefault("settings_waits", []).append(entry)
    if not ready:
        raise StepFailed(f"Settings did not show its main screen (a 'Settings' heading and "
                         f"'General') within {SETTINGS_WAIT_S:g} s of `simctl launch` "
                         f"({purpose}; {reads} tree reads, {failed} failed, last error "
                         f"{last_error!r}, {last_nodes} nodes in the last tree)")
    return entry


# --- part cli (CASE-003)-------------------------------------------------------------


def part_cli(args: argparse.Namespace, state: State, report: dict[str, Any]) -> None:
    cli, udid, home = state.cli, state.udid, state.home
    assert cli is not None and udid is not None and home is not None
    taps: list[dict[str, Any]] = []

    def start() -> dict[str, dict[str, Any]]:
        started = cli("session start", "session", "start")
        state.session_started = started.ok
        return {"session": {"ok": started.ok, "exit": started.code,
                            "error_code": started.payload.get("error_code")}}

    guarded(report, ("session",), start)
    # Booting a fresh Simulator drives the host load far above --max-load for
    # minutes: wait for it to settle before the first timed check (tree_off's
    # Settings wait and runs, then tree_on).
    settle(report, args.max_load, args.settle_timeout, "before tree_off")

    def trees(off: bool) -> dict[str, Any]:
        extra = {"AUTONOM_IOS_DAEMON": "off"} if off else None
        name = "ui tree (daemon off)" if off else "ui tree"
        warm = cli(f"{name} warm-up", "ui", "tree", extra_env=extra)
        times, bad = [], []
        counts_: list[int] = []
        for index in range(args.runs):
            reply = cli(name, "ui", "tree", extra_env=extra)
            times.append(reply.seconds)
            nodes = reply.nodes()
            counts_.append(len(nodes))
            payload = reply.payload
            good = (reply.ok and "General" in labels_of(nodes)
                    and (payload.get("transport") == "subprocess"
                         and payload.get("transport_reason") == "daemon_disabled"
                         if off else payload.get("transport") == "daemon"
                         and payload.get("ax_backend") == "ax"))
            if not good:
                bad.append({"run": index, "exit": reply.code, "transport": payload.get("transport"),
                            "transport_reason": payload.get("transport_reason"),
                            "ax_backend": payload.get("ax_backend"),
                            "error_code": payload.get("error_code")})
        result = timing_result("tree_off" if off else "tree_on", times, args.max_load)
        result.update({"warmup_s": warm.seconds, "warmup_ok": warm.ok, "bad_runs": bad,
                       "nodes_max": max(counts_) if counts_ else None,
                       "nodes_at_most_30": bool(counts_) and max(counts_) <= 30})
        result["ok"] = warm.ok and not bad and result.get("median_ok", True)
        return result

    def tree_off() -> dict[str, dict[str, Any]]:
        # The Simulator boots to the Home screen: open Settings first. The
        # wait reads go through the idb process, so the daemon is not started
        # before tree_on (see open_settings).
        wait = open_settings(args, state, report, "before tree_off", daemon_off=True)
        result = trees(off=True)
        result["settings_wait"] = wait
        return {"tree_off": result}

    guarded(report, ("tree_off",), tree_off)
    # The idb process started a companion for the off runs: end it, so the
    # daemon starts (and owns) the one it uses.
    report["companions_before_daemon"] = release_companions(args, udid)

    def tree_on() -> dict[str, dict[str, Any]]:
        result = trees(off=False)
        labels = independent_labels(args, udid)
        result["independent_general"] = "General" in labels
        result["ok"] = result["ok"] and result["independent_general"]
        return {"tree_on": result}

    guarded(report, ("tree_on",), tree_on)

    def single() -> dict[str, dict[str, Any]]:
        table = process_table()
        found_companions, found_daemons = companions(udid, table), daemons(udid, table)
        record = registry(home, udid) or {}
        companion = record.get("companion") if isinstance(record.get("companion"), dict) else {}
        result = {"companions": found_companions, "daemons": found_daemons,
                  "registry_pid": record.get("pid"), "registry_companion": companion,
                  "versions": record.get("versions"), "load": host_load()}
        result["ok"] = (len(found_companions) == 1 and len(found_daemons) == 1
                        and companion.get("pid") in found_companions
                        and record.get("pid") in found_daemons)
        return {"single_daemon": result}

    guarded(report, ("single_daemon",), single)

    hid_log = state.hid_log
    assert hid_log is not None

    def observed_tap(name: str, *selector: str) -> tuple[Reply, dict[str, Any]]:
        before = input_requests(home, udid)
        hid_mark = hid_log.mark()
        reply = cli(name, "ui", "tap", *selector, "--observe")
        after = input_requests(home, udid)
        idb_taps = sum(1 for argv in reply.idb_calls if idb_verb(argv) == "ui tap")
        daemon_inputs = after - before if before is not None and after is not None else None
        # VER-003's oracle: the HID events the daemon wrote for this tap, as
        # logged by the hook in the daemon's own process (one down/up pair).
        events = hid_log.since(hid_mark)
        downs = sum(1 for event in events if event.get("kind") == "touch"
                    and event.get("direction") == "down")
        ups = sum(1 for event in events if event.get("kind") == "touch"
                  and event.get("direction") == "up")
        hid_known = hid_log.loaded()
        once = hid_known and ((idb_taps == 0 and downs == 1 and ups == 1)
                              or (idb_taps == 1 and downs == 0 and ups == 0))
        entry = {"name": name, "seconds": reply.seconds, "idb_taps": idb_taps,
                 "daemon_inputs": daemon_inputs, "hid_touch_down": downs, "hid_touch_up": ups,
                 "hid_known": hid_known, "once": once}
        taps.append(entry)
        return reply, entry

    def push_back() -> dict[str, dict[str, Any]]:
        push_times, back_times, push_bad, back_bad = [], [], [], []
        independent: dict[str, Any] = {}
        for index in range(args.runs):
            reply, _ = observed_tap("ui tap General", "--desc", "General")
            push_times.append(reply.seconds)
            if not (reply.ok and reply.payload.get("input_confirmed") is True
                    and outline_has(reply.outline(), "About")):
                push_bad.append({"run": index, "exit": reply.code,
                                 "input_confirmed": reply.payload.get("input_confirmed"),
                                 "error_code": reply.payload.get("error_code"),
                                 "warnings": reply.warning_codes()})
            if index == 0:
                independent["push_about"] = "About" in independent_labels(args, udid)
            # The Back button only: on iOS 27 a second node on the General page has
            # desc 'Settings' too, and `--desc` alone (a contains match) was ambiguous.
            reply, _ = observed_tap("ui tap Settings (Back)", "--desc", "Settings",
                                    "--role", "button", "--mode", "exact")
            back_times.append(reply.seconds)
            if not (reply.ok and reply.payload.get("input_confirmed") is True
                    and outline_has(reply.outline(), "General") and reply.seconds < BACK_LIMIT_S):
                back_bad.append({"run": index, "exit": reply.code, "seconds": reply.seconds,
                                 "input_confirmed": reply.payload.get("input_confirmed"),
                                 "error_code": reply.payload.get("error_code"),
                                 "warnings": reply.warning_codes()})
            if index == 0:
                independent["back_general"] = "General" in independent_labels(args, udid)
        push = timing_result("push", push_times, args.max_load)
        back = timing_result("back", back_times, args.max_load)
        first = next((tap for tap in taps if tap["name"] == "ui tap General"), None)
        push.update({"bad_runs": push_bad, "independent_about": independent.get("push_about"),
                     "first_tap_s": first["seconds"] if first else None})
        back.update({"bad_runs": back_bad, "independent_general": independent.get("back_general"),
                     "limit_s": BACK_LIMIT_S})
        push["ok"] = not push_bad and push["median_ok"] and bool(independent.get("push_about"))
        back["ok"] = not back_bad and back["median_ok"] and bool(independent.get("back_general"))
        return {"push": push, "back": back}

    guarded(report, ("push", "back"), push_back)

    def heading() -> tuple[tuple[int, int], list[int], int | None, list[dict[str, Any]]]:
        tree, stable = stable_tree(cli, "ui tree (heading)")
        if not stable:
            raise StepFailed(f"the main Settings screen kept changing for {STABLE_WAIT_S:g} s; "
                             "no settled baseline for non_animating")
        nodes = tree.nodes()
        matches = [node for node in nodes if node.get("desc") == "Settings"
                   and centre(node.get("bounds"))
                   and node.get("role") not in ("button", "navbar", "app", "window")]
        if not matches:
            raise StepFailed("no 'Settings' heading on the main Settings screen")
        node = min(matches, key=lambda item: (item.get("role") != "heading", item["bounds"][1]))
        point = centre(node["bounds"])
        assert point is not None
        return point, node["bounds"], screen_width(tree.payload, nodes), nodes

    def still_taps() -> dict[str, dict[str, Any]]:
        # push/back may have left General (or another screen) open: back to
        # the main screen, read through the daemon, which runs by now.
        wait = open_settings(args, state, report, "before non_animating", daemon_off=False)
        point, bounds, width, nodes = heading()
        times, bad, confirmed = [], [], []
        for index in range(args.runs):
            reply, _ = observed_tap("ui tap heading", "--x", str(point[0]), "--y", str(point[1]))
            times.append(reply.seconds)
            confirmed.append(reply.payload.get("input_confirmed"))
            if not (reply.ok and changes_empty(reply.payload)):
                bad.append({"run": index, "exit": reply.code, "changes": reply.payload.get("changes"),
                            "error_code": reply.payload.get("error_code")})
        still = timing_result("non_animating", times, args.max_load)
        still.update({"point": list(point), "heading_bounds": bounds, "bad_runs": bad,
                      "input_confirmed": confirmed})
        still["ok"] = not bad and still["median_ok"]
        still["settings_wait"] = wait
        # U-003: an observed tap on empty space: the heading's row, 6 points
        # from the screen's right edge. The point must lie outside the heading
        # and outside every control, or the check fails without tapping.
        empty: dict[str, Any] = {"screen_width": width, "heading_bounds": bounds,
                                 "settings_wait": wait}
        if width is None:
            empty.update(ok=False, error="the tree gave no screen width; no empty point chosen")
            return {"non_animating": still, "empty_tap": empty}
        empty_point = (width - 6, point[1])
        hits = [{"role": node.get("role"), "desc": node.get("desc"), "bounds": node.get("bounds")}
                for node in nodes if node.get("role") not in CONTAINER_ROLES
                and contains(node.get("bounds"), empty_point)]
        empty["point"] = list(empty_point)
        if contains(bounds, empty_point) or hits:
            empty.update(ok=False, nodes_at_point=hits[:5],
                         error="the chosen point is not empty space; not tapped")
            return {"non_animating": still, "empty_tap": empty}
        reply, _ = observed_tap("ui tap empty space", "--x", str(empty_point[0]),
                                "--y", str(empty_point[1]))
        empty.update({"exit": reply.code, "input_confirmed": reply.payload.get("input_confirmed"),
                      "confirmed_by": reply.payload.get("confirmed_by"),
                      "warnings": reply.warning_codes(), "seconds": reply.seconds,
                      "changes_empty": changes_empty(reply.payload)})
        empty["ok"] = reply.ok and isinstance(empty["input_confirmed"], bool)
        return {"non_animating": still, "empty_tap": empty}

    guarded(report, ("non_animating", "empty_tap"), still_taps)

    def web() -> dict[str, dict[str, Any]]:
        simctl(args, "openurl", udid, WEB_URL, timeout=60)
        deadline = time.monotonic() + 45
        attempts, last = 0, None
        while time.monotonic() < deadline:
            attempts += 1
            last = cli("ui tree (web)", "ui", "tree", "--format", "outline")
            if last.ok and WEB_TEXT in last.outline():
                break
            time.sleep(1)
        assert last is not None
        found = last.ok and WEB_TEXT in last.outline()
        result = {"attempts": attempts, "found": found, "seconds": last.seconds,
                  "ax_backend": last.payload.get("ax_backend"),
                  "transport": last.payload.get("transport"), "load": host_load()}
        if not found:
            result["outline_head"] = last.outline().splitlines()[:10]
        result["ok"] = found and result["ax_backend"] == "axbridge"
        return {"web_read": result}

    guarded(report, ("web_read",), web)

    def idle() -> dict[str, dict[str, Any]]:
        record = registry(home, udid) or {}
        companion = record.get("companion") if isinstance(record.get("companion"), dict) else {}
        companion_pid = companion.get("pid")
        stopped = cli("session stop", "session", "stop", target=False)
        state.session_started = not stopped.ok and state.session_started
        left = companions_left_running(stopped.payload)
        stopped_at = time.monotonic()
        time.sleep(max(0.0, IDLE_WAIT_S - (time.monotonic() - stopped_at)))
        table = process_table()
        at_wait = {"daemons": daemons(udid, table), "companions": companions(udid, table),
                   "registry": (home / "iosd" / f"{udid}.json").exists()}
        gone_after = None
        if any((at_wait["daemons"], at_wait["companions"], at_wait["registry"])):
            extra_deadline = time.monotonic() + 15
            while time.monotonic() < extra_deadline:
                if not (daemons(udid) or companions(udid)
                        or (home / "iosd" / f"{udid}.json").exists()):
                    gone_after = round(time.monotonic() - stopped_at, 1)
                    break
                time.sleep(0.5)
        result = {"session_stop_ok": stopped.ok, "daemon_companion_pid": companion_pid,
                  "companion_owned": companion.get("owned"),
                  "companion_left_running": left, "wait_s": IDLE_WAIT_S,
                  "idle_ms": args.idle_ms, "at_wait": at_wait, "gone_after_s": gone_after}
        if not isinstance(companion_pid, int):
            result["error"] = "the registry named no daemon companion before the stop"
        result["ok"] = (stopped.ok and isinstance(companion_pid, int) and companion_pid not in left
                        and not at_wait["daemons"] and not at_wait["companions"]
                        and not at_wait["registry"])
        return {"idle_stop": result}

    guarded(report, ("idle_stop",), idle)

    report["taps"] = taps
    once = [tap for tap in taps if not tap["once"]]
    report["checks"]["no_double_send"] = {"ok": bool(taps) and not once, "taps": len(taps),
                                          "hid_hook_loaded": state.hid_log.loaded()
                                          if state.hid_log else False,
                                          "not_once": once[:10]}
    others = sorted({named for argv in state.cli.log.entries()
                     if (named := udid_named(argv)) and named != udid})
    report["checks"]["scope"] = {"ok": not others, "other_udids": others,
                                 "idb_calls": len(state.cli.log.entries())}


# --- part canvas (CASE-004) ----------------------------------------------------------


class VideoClient(threading.Thread):
    """One /ws/video connection to the Canvas, so it streams: frames are read
    and dropped, pings answered."""

    def __init__(self, port: int, token: str) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.token = token
        self.sock: socket.socket | None = None
        self.messages = 0
        self.bytes = 0
        self.error: str | None = None
        self.closed = threading.Event()
        self.buffer = b""

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET /ws/video?token={self.token} HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(request.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise StepFailed("the Canvas closed the video connection during the handshake")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in f"{status} ":
            sock.close()
            raise StepFailed(f"/ws/video was refused: {status}")
        self.buffer = rest
        sock.settimeout(None)
        self.sock = sock

    def _read(self, size: int) -> bytes:
        assert self.sock is not None
        while len(self.buffer) < size:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        assert self.sock is not None and len(payload) < 126
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)

    def run(self) -> None:
        try:
            while not self.closed.is_set():
                first, second = self._read(2)
                opcode, length = first & 0x0F, second & 0x7F
                if length == 126:
                    length = int.from_bytes(self._read(2), "big")
                elif length == 127:
                    length = int.from_bytes(self._read(8), "big")
                mask = self._read(4) if second & 0x80 else b""
                payload = self._read(length)
                if mask:
                    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send(0xA, payload[:125])
                    continue
                self.messages += 1
                self.bytes += length
        except (EOFError, OSError) as exc:
            if not self.closed.is_set():
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.closed.set()

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send(0x8, b"\x03\xe8")
        except OSError:
            pass
        self.closed.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.join(timeout=5)


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def canvas_status(port: int, token: str) -> dict[str, Any] | None:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(f"http://127.0.0.1:{port}/status",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback only
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def packets(port: int, token: str) -> int | None:
    status = canvas_status(port, token)
    idb = (status or {}).get("idb")
    return int(idb.get("packets") or 0) if isinstance(idb, dict) else None


def wait_for(predicate: Callable[[], Any], seconds: float, step: float = 0.1) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def stop_canvas(state: State) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if state.video is not None:
        state.video.close()
        result["video_messages"] = state.video.messages
        state.video = None
    process = state.canvas
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            result["killed"] = True
    if process is not None:
        result["exit"] = process.returncode
    if state.canvas_log is not None:
        state.canvas_log.close()
        state.canvas_log = None
    if state.udid and state.canvas_port:
        left = canvases(state.udid, state.canvas_port)
        for pid in left:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        result["left"] = wait_gone(left, 5) if left else []
    state.canvas = None
    return result


def part_canvas(args: argparse.Namespace, state: State, report: dict[str, Any]) -> None:
    cli, udid, home = state.cli, state.udid, state.home
    assert cli is not None and udid is not None and home is not None
    token = secrets.token_urlsafe(18)

    def start() -> dict[str, dict[str, Any]]:
        started = cli("session start", "session", "start")
        state.session_started = started.ok
        return {"session": {"ok": started.ok, "exit": started.code,
                            "error_code": started.payload.get("error_code")}}

    guarded(report, ("session",), start)
    state.sampler = Sampler(udid)
    state.sampler.start()
    others_before = other_companions(udid)

    def canvas() -> dict[str, dict[str, Any]]:
        port = free_port()
        state.canvas_port = port
        state.canvas_log = open(home / "canvas.log", "w", encoding="utf-8")  # noqa: SIM115
        command = [sys.executable, str(CLI), "canvas", "serve", "--single", "--platform", "ios",
                   "--target", udid, "--port", str(port), "--token", token, "--transport", "idb"]
        state.canvas = subprocess.Popen(command, env=cli.env, stdin=subprocess.DEVNULL,
                                        stdout=state.canvas_log, stderr=subprocess.STDOUT,
                                        cwd=str(ROOT))
        if not wait_for(lambda: canvas_status(port, token), BUDGET.clamp(40), 0.25):
            raise StepFailed("the Canvas did not answer /status within 40 s")
        state.video = VideoClient(port, token)
        state.video.connect()
        state.video.start()
        streaming = wait_for(lambda: (packets(port, token) or 0) > 0, BUDGET.clamp(40), 0.25)
        status = canvas_status(port, token) or {}
        idb = status.get("idb") if isinstance(status.get("idb"), dict) else {}
        report["canvas"] = {"port": port, "frames_before": idb.get("packets"),
                            "transport": status.get("transport")}
        result = {"port": port, "transport": status.get("transport"),
                  "frames_before": idb.get("packets"),
                  "companion_source": idb.get("companion_source"),
                  "daemon_attached": idb.get("daemon_attached"),
                  "daemon_fallback": idb.get("daemon_fallback"),
                  "companion_pid": idb.get("companion_pid"),
                  "companion_starts": idb.get("companion_starts"),
                  "video_messages": state.video.messages}
        result["ok"] = (bool(streaming) and status.get("transport") == "idb"
                        and idb.get("companion_source") == "daemon")
        return {"canvas": result}

    guarded(report, ("canvas",), canvas)
    port = state.canvas_port or 0
    sampler = state.sampler

    # Booting a fresh Simulator drives the host load far above --max-load for
    # minutes: wait for it to settle (with the Canvas streaming, the condition
    # one_companion's observed tap runs under) before one_companion.
    settle(report, args.max_load, args.settle_timeout, "before one_companion")

    def one() -> dict[str, dict[str, Any]]:
        # The Simulator boots to the Home screen: open Settings first, read
        # through the daemon the Canvas uses (a read through the idb process
        # would start a second companion; see open_settings).
        wait = open_settings(args, state, report, "before one_companion", daemon_off=False)
        mark, frames_before = sampler.mark(), packets(port, token)
        tree = cli("ui tree", "ui", "tree")
        tap = cli("ui tap General", "ui", "tap", "--desc", "General", "--observe")
        time.sleep(1)
        frames_after = packets(port, token)
        seen = counts(sampler.since(mark))
        result = {"tree_ok": tree.ok, "tree_transport": tree.payload.get("transport"),
                  "tap_ok": tap.ok, "tap_transport": tap.payload.get("transport"),
                  "input_confirmed": tap.payload.get("input_confirmed"),
                  "frames_before": frames_before, "frames_after": frames_after, "ps": seen,
                  "load": host_load(), "settings_wait": wait}
        result["ok"] = (tree.ok and tap.ok and seen["max"] == 1 and seen["min"] == 1
                        and frames_before is not None and frames_after is not None
                        and frames_after > frames_before)
        return {"one_companion": result}

    guarded(report, ("one_companion",), one)

    def search_field() -> tuple[int, int] | None:
        tree = cli("ui tree (search)", "ui", "tree", "--format", "json")
        for node in tree.nodes():
            role = str(node.get("role") or "").lower()
            if node.get("editable") or "search" in role or role in ("textfield", "text_field"):
                point = centre(node.get("bounds"))
                if point:
                    return point
        return None

    def text() -> dict[str, dict[str, Any]]:
        # Back to the main screen (the search field is on it), whatever the
        # General tap left open; the field's point then comes from the tree.
        wait = open_settings(args, state, report, "before text_reuses", daemon_off=False)
        point = search_field()
        swiped = False
        if point is None:
            swiped = True
            cli("ui swipe (reveal search)", "ui", "swipe", "--from", "200,300", "--to", "200,520")
            point = search_field()
        tapped = None
        if point is not None:
            tapped = cli("ui tap search", "ui", "tap", "--x", str(point[0]), "--y", str(point[1]))
        mark, before = sampler.mark(), companions(udid)
        typed = cli("ui type hi", "ui", "type", "hi")
        during = counts(sampler.since(mark))
        mark_after = sampler.mark()
        time.sleep(2)
        after = counts(sampler.since(mark_after))
        idb_text = sum(1 for argv in typed.idb_calls if idb_verb(argv) == "ui text")
        result = {"field_point": list(point) if point else None, "swiped": swiped,
                  "field_tap_ok": tapped.ok if tapped else None, "type_ok": typed.ok,
                  "type_transport": typed.payload.get("transport"), "idb_ui_text": idb_text,
                  "companions_before": before, "during": during, "after": after,
                  "warnings": typed.warning_codes(), "settings_wait": wait}
        result["ok"] = (typed.ok and during["max"] == 1 and during["min"] == 1
                        and after["max"] == 1 and after["min"] == 1
                        and during["pids"] == before == after["pids"])
        return {"text_reuses": result}

    guarded(report, ("text_reuses",), text)

    def restart() -> dict[str, dict[str, Any]]:
        record = registry(home, udid) or {}
        companion = record.get("companion") if isinstance(record.get("companion"), dict) else {}
        pid = companion.get("pid")
        table = process_table()
        ours = companions(udid, table)
        if not isinstance(pid, int) or pid not in ours:
            raise StepFailed(f"the registry's companion {pid!r} is not the running one {ours}; "
                             "nothing was killed")
        frames_at_kill = packets(port, token)
        killed_at = time.monotonic()
        os.kill(pid, signal.SIGKILL)
        new_pid = wait_for(lambda: next((other for other in companions(udid) if other != pid), None),
                           RESTART_LIMIT_S)
        new_after = round(time.monotonic() - killed_at, 2) if new_pid else None
        frames_new_companion = packets(port, token) or 0
        resumed = wait_for(lambda: (packets(port, token) or 0) > frames_new_companion,
                           max(0.0, RESTART_LIMIT_S - (time.monotonic() - killed_at)) or 0.1)
        resumed_after = round(time.monotonic() - killed_at, 2) if resumed else None
        time.sleep(1)
        final = companions(udid)
        others_after = other_companions(udid)
        status = canvas_status(port, token) or {}
        idb = status.get("idb") if isinstance(status.get("idb"), dict) else {}
        overall = counts(sampler.since(0))
        result = {"killed_pid": pid, "new_pid": new_pid, "new_companion_after_s": new_after,
                  "frames_at_kill": frames_at_kill, "frames_at_new_companion": frames_new_companion,
                  "frames_resumed_after_s": resumed_after, "companions_after": final,
                  "companion_source_after": idb.get("companion_source"),
                  "others_before": others_before, "others_after": others_after,
                  "max_companions_any_sample": overall["max"], "load": host_load()}
        result["ok"] = (new_pid is not None and new_after is not None
                        and new_after <= RESTART_LIMIT_S and resumed_after is not None
                        and resumed_after <= RESTART_LIMIT_S and len(final) == 1
                        and set(others_before) <= set(others_after)
                        and overall["max"] is not None and overall["max"] <= 1
                        and idb.get("companion_source") == "daemon")
        return {"restart": result}

    guarded(report, ("restart",), restart)


# --- cleanup -----------------------------------------------------------------------


# The daemon log's lifecycle and timing events kept in the evidence (the log
# never holds tree contents or typed text); the home with the log itself is
# removed at cleanup. quiet_probe (the silent probes before a replacement, with
# silent_run), quiet_suspect (host_load / deadline / too_soon skips) and
# quiet_backoff_reset carry only timings, signal names and counts, which the
# CASE-004 checks read.
DAEMON_LOG_EVENTS = ("warm", "quiet_first_event", "quiet_arm_wait", "quiet_reopened",
                     "quiet_reopen_failed", "quiet_stream_error", "quiet_target",
                     "quiet_target_first_event", "quiet_probe", "quiet_suspect",
                     "quiet_backoff_reset", "quiet_stuck", "touch_late", "act_timing",
                     "hid_primed", "hid_unconfirmed", "hid_lost", "hid_drop", "input_cause",
                     "idb_connect", "attach", "detach", "companion_lost", "companion_restarted",
                     "companion_restart_failed", "companion_adopted", "companion_inherited",
                     "stopping")
DAEMON_LOG_KEPT = 400


def daemon_log_events(home: Path, udid: str) -> list[dict[str, Any]]:
    """The last DAEMON_LOG_KEPT DAEMON_LOG_EVENTS lines of this run's daemon
    log, parsed; [] when there is no log."""
    try:
        lines = (home / "iosd" / f"{udid}.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("event") in DAEMON_LOG_EVENTS:
            events.append(record)
    return events[-DAEMON_LOG_KEPT:]


def cleanup(args: argparse.Namespace, state: State, report: dict[str, Any]) -> None:
    detail: dict[str, Any] = {}
    udid, home = state.udid, state.home
    if state.canvas is not None or state.video is not None:
        detail["canvas"] = stop_canvas(state)
    if state.session_started and state.cli is not None:
        try:
            stopped = state.cli("session stop (cleanup)", "session", "stop", target=False,
                                timeout=60)
            detail["session_stop_ok"] = stopped.ok
        except (OSError, subprocess.SubprocessError) as exc:
            detail["session_stop_error"] = str(exc)
    if udid and home is not None:
        if args.part == "canvas":
            # The Canvas detached: the daemon now stops by its idle rule.
            deadline = time.monotonic() + args.idle_ms / 1000 + 10
            while time.monotonic() < deadline and (daemons(udid) or registry(home, udid)):
                time.sleep(0.5)
            detail["idle_stop"] = {"daemons": daemons(udid), "companions": companions(udid),
                                   "registry": registry(home, udid) is not None}
        if daemons(udid):
            detail["daemon_stop_request"] = daemon_request(home, udid, "stop", timeout=15) is not None
            left = wait_gone(daemons(udid), 10)
            for pid in left:
                if pid in daemons(udid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
            detail["daemons_left"] = wait_gone(left, 5) if left else []
        if companions(udid):
            detail["companions"] = release_companions(args, udid)
    if state.sampler is not None:
        state.sampler.stop()
        report["ps_samples"] = len(state.sampler.samples)
    if udid:
        subprocess.run([args.xcrun, "simctl", "shutdown", udid], capture_output=True, text=True,
                       timeout=120, check=False)
        deleted = subprocess.run([args.xcrun, "simctl", "delete", udid], capture_output=True,
                                 text=True, timeout=120, check=False)
        detail["delete_exit"] = deleted.returncode
        detail["simulator_listed"] = simulator_listed(args, udid)
    elif state.name:
        detail["simulator_left"] = (f"simctl create for {state.name!r} printed no UDID; "
                                    "look for it in `xcrun simctl list devices`")
    if home is not None:
        if udid:
            report["daemon_log"] = daemon_log_events(home, udid)
        shutil.rmtree(home, ignore_errors=True)
        detail["home_removed"] = not home.exists()
    if udid:
        table = process_table()
        detail["left"] = {"daemons": daemons(udid, table), "companions": companions(udid, table)}
    ok = (bool(udid) and detail.get("simulator_listed") is False
          and detail.get("home_removed") is True
          and not detail.get("left", {}).get("daemons") and not detail.get("left", {}).get("companions"))
    if args.part == "canvas":
        idle = detail.get("idle_stop") or {}
        canvas = detail.get("canvas") or {}
        ok = ok and not idle.get("daemons") and not idle.get("companions") \
            and not idle.get("registry") and not canvas.get("left")
    report["checks"]["cleanup"] = {"ok": ok, **detail}


# --- main --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ios_daemon_live.py",
        description="Live check of the iOS device daemon on a Simulator the run creates and "
                    "deletes (CASE-003 --part cli, CASE-004 --part canvas). --help touches no device.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--part", choices=("cli", "canvas"), required=True,
                        help="cli: CASE-003 (one daemon, ax/axbridge reads, confirmed taps, speed, "
                             "idle stop); canvas: CASE-004 (one companion with the Canvas, ui type "
                             "reuse, video after a companion kill)")
    parser.add_argument("--create-simulator", action="store_true",
                        help="create, boot, use and delete a Simulator of the run's own (required: "
                             "the run never uses an existing Simulator)")
    parser.add_argument("--evidence-dir", required=True,
                        help="where ios-daemon-cli.json or ios-daemon-canvas.json is written")
    parser.add_argument("--device-type", default=DEFAULT_DEVICE_TYPE,
                        help=f"simctl device type name (default {DEFAULT_DEVICE_TYPE!r})")
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME,
                        help=f"simctl runtime name (default {DEFAULT_RUNTIME!r})")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help=f"timed repetitions per measured step (default {DEFAULT_RUNS})")
    parser.add_argument("--idle-ms", type=int, default=DEFAULT_IDLE_MS,
                        help=f"AUTONOM_IOSD_IDLE_MS for the run's daemon (default {DEFAULT_IDLE_MS}; "
                             "the idle-stop check waits 25 s)")
    parser.add_argument("--max-load", type=float, default=DEFAULT_MAX_LOAD,
                        help="host load above which a timing result is marked load_high (default 60)")
    parser.add_argument("--settle-timeout", type=float, default=None,
                        help="seconds to wait, after the boot, for the host 1-minute and 5-minute "
                             "loads to stay below --max-load for 3 readings 5 s apart before the timed checks "
                             "(default 900 for --part cli, 1100 for --part canvas: with the 180 s cleanup "
                             "reserve the steps get 1920 s and 1520 s, and the rest of an r523-like run "
                             "took 245 s and 177 s; a timeout is recorded in settle, not a failure)")
    parser.add_argument("--time-limit", type=float, default=None,
                        help="seconds for the whole run, cleanup included (default 2100 for cli, "
                             "1700 for canvas)")
    parser.add_argument("--xcrun", default="xcrun", help="xcrun binary (default xcrun)")
    parser.add_argument("--idb", default=None, help="the real fb-idb client (default: idb on PATH)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.create_simulator:
        parser.error("--create-simulator is required: the run never uses an existing Simulator")
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.settle_timeout is None:
        args.settle_timeout = DEFAULT_SETTLE_TIMEOUTS_S[args.part]
    if not args.settle_timeout >= 0:
        parser.error("--settle-timeout must be 0 or more")
    if args.idle_ms < 1000 or args.idle_ms >= IDLE_WAIT_S * 1000:
        parser.error(f"--idle-ms must be at least 1000 and below {IDLE_WAIT_S * 1000}")
    args.idb = args.idb or shutil.which("idb")
    if not args.idb:
        parser.error("idb is not on PATH; pass --idb")
    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir / f"ios-daemon-{args.part}.json"
    limit = args.time_limit or TIME_LIMITS[args.part]

    report: dict[str, Any] = {
        "schema": "autonom-ios-daemon-live/v1", "case": "CASE-003" if args.part == "cli" else "CASE-004",
        "part": args.part, "started_at": now(), "load_start": host_load(),
        "max_load": args.max_load, "settle_timeout_s": args.settle_timeout, "runs": args.runs,
        "idle_ms": args.idle_ms,
        "time_limit_s": limit, "versions": versions(args), "checks": {},
    }
    state = State()
    exit_signal: int | None = None
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _raise_terminated)
    try:
        try:
            BUDGET.start(limit)
            state.home = Path(tempfile.mkdtemp(prefix="autonom-iosd-live-"))
            bin_dir = state.home / "bin"
            bin_dir.mkdir()
            log = IdbLog(state.home / "idb-calls.ndjson")
            write_wrapper(bin_dir, args.idb, log.path)
            state.hid_log = HidLog(state.home / "hid-events.ndjson")
            node = real_node()
            node_wrapper = write_node_wrapper(bin_dir, node, state.hid_log.path) if node else None
            report["hid_hook"] = {"node": node, "wrapped": node_wrapper is not None}
            create_simulator(args, state, report)
            assert state.udid is not None
            state.cli = Cli(state.udid, base_env(state.home, bin_dir, args.idle_ms, node_wrapper),
                            log)
            if args.part == "cli":
                part_cli(args, state, report)
            else:
                part_canvas(args, state, report)
        except Terminated as exc:
            exit_signal = exc.signum
            report["terminated"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded, then cleaned up
            report["error"] = f"{type(exc).__name__}: {exc}"[-400:]
        finally:
            for signum in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, _note_late_signal)
            BUDGET.end_steps()
            try:
                cleanup(args, state, report)
            except Exception as exc:  # noqa: BLE001
                report["checks"]["cleanup"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if state.cli is not None:
            report["calls"] = state.cli.calls
        report["ended_at"] = now()
        report["load_end"] = host_load()
        for name in EXPECTED_CHECKS[args.part]:
            report["checks"].setdefault(name, {"ok": False, "error": "not run"})
        report["ok"] = all(result.get("ok") for result in report["checks"].values())
        partial = evidence.with_suffix(".json.partial")
        partial.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        partial.replace(evidence)
        print(json.dumps({"ok": report["ok"], "part": args.part, "evidence": str(evidence),
                          "failed": sorted(name for name, result in report["checks"].items()
                                           if not result.get("ok"))}))
    if exit_signal is not None:
        return 128 + exit_signal
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
