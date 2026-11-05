#!/usr/bin/env python3
"""Live check of exact idb companion matching on one iOS Simulator (CASE-004, VER-004).

Not part of the unit suite. It must only run on a private bench simulator no
other session uses (the run's "AB Bench mobile-mcp"):

    python3 tests/live/ios_companion_live.py --udid <UDID> [--boot] --evidence-dir DIR \
        [--idb PATH] [--max-load 60] [--keep-home]

`--boot` boots the --udid simulator when it is Shutdown (`xcrun simctl
bootstatus <UDID> -b`) and shuts it down at the end only when this run
booted it; no other simulator is ever booted or shut down. That is also what
a run without `--boot` does: the flag states it on the command line (the
approved CASE-004 command passes it) and the report records it
(`boot_requested`).

After a boot this run made, the first `ui tree` is tried again 2 s
(UI_TREE_RETRY_EVERY_S) after each attempt that failed with `backend_failed`
or did not finish within CLI_TIMEOUT, until it succeeds, no attempt starting
more than UI_TREE_RETRY_S (60 s) after the first: right after `bootstatus
-b` idb's first accessibility read can fail with `backend_failed` ("No
translation object returned for simulator"; CASE-004 run 1, 2026-10-25). Any
other error code ends the retry at once (a failed `ui_tree_ok`, exit 1). A
simulator that was already Booted gets one attempt, as before. Every attempt
is in the report (`steps.ui_tree_attempts`: `seconds` from the first
attempt's start to the end of this one, `exit`, `error_code`, `timed_out`
for a call killed at CLI_TIMEOUT, and the `bench_companions` running after
it). When the retry runs out, `steps.ui_tree` holds the last attempt,
`ui_tree_ok` is false, and the rest of the case still runs and is recorded
(registry, `session stop` and its checks, the decoy checks), so CO-02 is
judged. A simulator this run booted that never answered is an environment
fault, not a pass or a product failure (exit 3, `environment_fault`), only
when every other check of that run passed.

Every run uses its own temporary AUTONOM_HOME (removed at the end unless
--keep-home), so it never reads, stops or writes the user's sessions. The
script touches nothing but the one --udid simulator and the one decoy
process it starts: other simulators' companions are only read, never
signalled.

Steps (the oracle is the kernel's process table, never the CLI alone):

1. record: the 1-minute host load must be below --max-load; every
   `idb_companion` on the Mac (`pgrep -x idb_companion`, then each pid's
   command line) is recorded, and none may serve this UDID (`--udid
   <UDID>` as two whole arguments) — otherwise nothing is started. Then the
   decoy `python3 -c 'import time; time.sleep(600)' idb_companion --udid
   <UDID>` is started, and the simulator is booted only when it is Shutdown.
2. `session start --app-id com.apple.Preferences --ui-backend idb`, then
   `ui tree` (retried after a boot, above): the call starts a companion for
   the UDID (a read that never succeeded is recorded and the case goes
   on); across all attempts exactly one companion served the UDID, and
   the registry (`$AUTONOM_HOME/processes/processes.json`) holds exactly one
   `idb_companion` row for it, owned by the session, whose pid is that
   companion.
3. `session stop`: its `session_processes` teardown terminated that pid; no
   `idb_companion` serves the UDID any more; the decoy is alive and was not
   reported as a companion left running.
4. cleanup: the script stops its decoy; a companion still serving this
   UDID (one Autonom failed to stop: step 1 saw none, so this run started
   it) is stopped by the script and recorded, the checks above staying
   failed; the simulator is shut down only when this run booted it; every
   companion of another UDID recorded in step 1 must still run, with the
   same command line (one another session started meanwhile is listed in
   the report, not counted).

Exactly one companion is started and stopped. Writes
<evidence-dir>/ios-companion.json (saved before the cleanup too, so a
killed run still leaves evidence) with one entry per check and `ok`.

`ok` is true only when the run reached its last step, every check in
REQUIRED_CHECKS was recorded and passed, and no cleanup step failed. A run
cut short can never pass on the checks it happened to record: Ctrl-C
(SIGINT), SIGTERM and SIGHUP stop the steps and are recorded under `error`,
and so is any failed step or other exception (an unexpected one with its
traceback); `missing_checks` names the checks that never ran. The cleanup
always runs, with those three signals ignored so a second Ctrl-C cannot cut
it short, and the decoy's handle is kept the moment it is started, so the
decoy is stopped however the run ends. Exit 0 when `ok`; 128 + the signal number when a signal
stopped the run; 3 (environment fault) when the simulator this run booted
never answered `ui tree` within UI_TREE_RETRY_S (every attempt
`backend_failed` or timed out) while the steps ran to their end and every
other required check was recorded and passed, no companion was left for the
script to stop and no cleanup step failed; 2 for a usage error (argparse); 1
otherwise.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "autonom.py"
SETTINGS = "com.apple.Preferences"
CLI_TIMEOUT = 180
BOOT_TIMEOUT = 600
BOOT_SETTLE_S = 5  # let SpringBoard settle before the first accessibility read
# After a boot this run made: `ui tree` again 2 s after each attempt that
# failed with RETRIED_CODE (the code CASE-004 run 1 saw right after the
# boot) or timed out, until it answers, no attempt starting more than 60 s
# after the first.
UI_TREE_RETRY_EVERY_S = 2
UI_TREE_RETRY_S = 60
RETRIED_CODE = "backend_failed"
EXIT_ENVIRONMENT = 3
STOP_WAIT_S = 15
DECOY_SLEEP = "import time; time.sleep(600)"
# Overrides that would send the CLI to another idb, backend or companion.
SCRUBBED = ("AUTONOM_IDB", "AUTONOM_UI_BACKEND", "AUTONOM_SIMCTL", "AUTONOM_IDB_COMPANION",
            "AUTONOM_IOS_HID", "AUTONOM_IDB_STATE_FILE", "AUTONOM_SESSION_ID",
            "AUTONOM_CONTROL_HOLDER", "AUTONOM_CONTROL_GENERATION")
# Every check a passing run records, in step order. `ok` needs each one, so
# a run cut short never passes on the few checks it recorded before it stopped.
REQUIRED_CHECKS = (
    # 1. record and decoy
    "host_load_below_limit", "no_bench_companion_before",
    "decoy_names_both", "decoy_not_named_idb_companion",
    # 2. session start and ui tree
    "session_start_ok", "ui_tree_ok", "one_companion_started",
    "companion_registered_to_session",
    # 3. session stop
    "session_stop_ok", "session_stop_terminated_the_companion",
    "no_bench_companion_after_stop", "decoy_alive_after_stop", "decoy_not_reported",
    "decoy_never_signalled",
    # 4. cleanup
    "decoy_stopped_by_script", "state_restored", "other_companions_unchanged",
    "no_bench_companion_at_end",
)
# Ctrl-C, `kill` and a closed terminal: each stops the steps, never the cleanup.
CAUGHT = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class StepFailed(Exception):
    pass


class Terminated(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"interrupted by {signal.Signals(signum).name}")
        self.signum = signum


class Signals:
    """SIGINT, SIGTERM and SIGHUP stop the steps by raising Terminated, so
    the cleanup still runs. Only the first one raises, and only while the
    steps run (`armed`): a second Ctrl-C, or one that lands after the steps
    ended or while a failed step is being recorded, can never cut the
    cleanup short. Inside `held()` a signal is kept until the block ends,
    so a process started there is recorded before the run stops. During
    the cleanup all three are ignored outright (`ignore()`), so the
    commands it runs ignore them too."""

    def __init__(self) -> None:
        self.previous: dict[int, Any] = {}
        self.armed = False
        self.holding = False
        self.pending: int | None = None

    def _handler(self, signum: int, _frame: Any) -> None:
        if self.holding:
            if self.pending is None:
                self.pending = signum
            return
        if self.armed:
            self.armed = False
            raise Terminated(signum)

    def catch(self) -> None:
        self.armed = True
        for signum in CAUGHT:
            self.previous[signum] = signal.signal(signum, self._handler)

    def disarm(self) -> None:
        """The steps are over: from here on a signal raises nothing."""
        self.armed = False

    def ignore(self) -> None:
        self.armed = False
        for signum in self.previous:
            signal.signal(signum, signal.SIG_IGN)

    def restore(self) -> None:
        for signum, handler in self.previous.items():
            # None: the old handler was not set from Python; the default is closest
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)

    @contextlib.contextmanager
    def held(self) -> Iterator[None]:
        self.holding = True
        try:
            yield
        finally:
            self.holding = False
        if self.pending is not None and self.armed:
            signum, self.pending, self.armed = self.pending, None, False
            raise Terminated(signum)

def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clock() -> float:
    """The retry's clock (a test replaces it with a fake)."""
    return time.monotonic()


def pause(seconds: float) -> None:
    """The retry's wait (a test replaces it with a fake)."""
    time.sleep(seconds)


def host_load() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def run(argv: list[str], *, timeout: float, env: dict | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               env=env, stdin=subprocess.DEVNULL, check=False)
    if check and completed.returncode != 0:
        raise StepFailed(f"{' '.join(argv[:4])} failed ({completed.returncode}): "
                         f"{(completed.stderr or completed.stdout).strip()[-300:]}")
    return completed


def simulator_state(udid: str) -> str:
    listing = json.loads(run(["xcrun", "simctl", "list", "devices", "--json"],
                             timeout=60).stdout)
    for devices in listing.get("devices", {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return str(device.get("state", ""))
    raise StepFailed(f"no simulator {udid}")


def boot_if_shutdown(udid: str, state: str) -> bool:
    """Boot the one simulator `udid` when `state` is Shutdown, and wait until
    it has booted; True when it was booted here. No other UDID is touched."""
    if state != "Shutdown":
        return False
    run(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=BOOT_TIMEOUT)
    return True


def shutdown_if_booted_here(udid: str, booted_here: bool) -> bool:
    """Shut `udid` down only when this run booted it; True when asked to."""
    if not booted_here:
        return False
    run(["xcrun", "simctl", "shutdown", udid], timeout=120, check=False)
    return True


def pid_command(pid: int) -> str:
    completed = run(["ps", "-ww", "-o", "command=", "-p", str(pid)], timeout=15, check=False)
    return completed.stdout.strip()


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def serves(command: str, udid: str) -> bool:
    """`--udid <udid>` as two whole arguments of the command line."""
    words = command.split()
    return any(flag == "--udid" and value == udid for flag, value in zip(words, words[1:]))


def companions() -> dict[int, str]:
    """Every process the kernel names `idb_companion`, with its command line.
    The decoy is a Python process, so it is never among them."""
    listed = run(["pgrep", "-x", "idb_companion"], timeout=15, check=False)
    if listed.returncode not in (0, 1):
        raise StepFailed(f"pgrep failed ({listed.returncode}): {listed.stderr.strip()[-200:]}")
    found = {}
    for line in listed.stdout.split():
        if line.isdigit():
            command = pid_command(int(line))
            if command:
                found[int(line)] = command
    return found


def bench(found: dict[int, str], udid: str) -> list[int]:
    return sorted(pid for pid, command in found.items() if serves(command, udid))


def others(found: dict[int, str], udid: str) -> dict[str, str]:
    return {str(pid): command[:400] for pid, command in sorted(found.items())
            if not serves(command, udid)}


def registry_rows(home: Path, udid: str) -> list[dict[str, Any]]:
    path = home / "processes" / "processes.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("processes") if isinstance(payload, dict) else payload
    return [row for row in rows or []
            if row.get("kind") == "idb_companion" and row.get("udid") == udid]


class Autonom:
    def __init__(self, udid: str, home: Path, idb: str | None) -> None:
        self.udid = udid
        self.env = {key: value for key, value in os.environ.items() if key not in SCRUBBED}
        self.env["AUTONOM_HOME"] = str(home)
        # This case checks the session registry's companion matching on the idb
        # process route. With the iOS device daemon on (0.37.0), the daemon owns
        # the companion and `session stop` leaves it to the daemon's idle rule.
        self.env["AUTONOM_IOS_DAEMON"] = "off"
        if idb:
            self.env["AUTONOM_IDB"] = idb
        self.calls: list[dict[str, Any]] = []
        self.last_exit: int | None = None  # the exit code of the latest call

    def __call__(self, *argv: str, timeout: float = CLI_TIMEOUT) -> dict[str, Any]:
        started = time.monotonic()
        completed = run([sys.executable, str(CLI), "--udid", self.udid, *argv],
                        timeout=timeout, env=self.env, check=False)
        stream = completed.stdout if completed.stdout.strip() else completed.stderr
        try:
            payload = json.loads(stream)
        except ValueError:
            payload = {"ok": False, "error": (stream or "").strip()[-300:]}
        self.last_exit = completed.returncode
        self.calls.append({"argv": list(argv), "exit": completed.returncode,
                           "seconds": round(time.monotonic() - started, 1),
                           "ok": payload.get("ok"), "error_code": payload.get("error_code")})
        return payload


def wait_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def save(report: dict[str, Any], out: Path) -> None:
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def record_step(report: dict[str, Any], args: argparse.Namespace) -> None:
    load = host_load()
    report["host_load_start"] = load
    checks = report["checks"]
    checks["host_load_below_limit"] = bool(load) and load[0] < args.max_load
    if not checks["host_load_below_limit"]:
        raise StepFailed(f"host load {load} is not below {args.max_load}; nothing was started")
    found = companions()
    report["steps"]["record"] = {"bench_companions": bench(found, args.udid),
                                 "other_companions": others(found, args.udid)}
    checks["no_bench_companion_before"] = not bench(found, args.udid)
    if not checks["no_bench_companion_before"]:
        raise StepFailed(f"an idb_companion already serves {args.udid}; nothing was started")


def start_decoy(report: dict[str, Any], udid: str, decoys: list[subprocess.Popen],
                signals: Signals) -> subprocess.Popen:
    """Start the decoy. Its handle goes into `decoys` before anything that
    can fail or be interrupted (a signal waits until then), so the cleanup
    stops it however this step ends."""
    with signals.held():
        decoy = subprocess.Popen(
            [sys.executable, "-c", DECOY_SLEEP, "idb_companion", "--udid", udid],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        decoys.append(decoy)
    report["steps"]["decoy"] = {"pid": decoy.pid}
    shown = wait_until(lambda: udid in pid_command(decoy.pid), 10)
    command = pid_command(decoy.pid)
    report["steps"]["decoy"]["command"] = command[:400]
    report["checks"]["decoy_names_both"] = (shown and "idb_companion" in command
                                            and udid in command)
    report["checks"]["decoy_not_named_idb_companion"] = decoy.pid not in companions()
    return decoy


def ui_tree_attempts(report: dict[str, Any], autonom: Autonom, udid: str, *, retry: bool
                     ) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """`ui tree` once, or, with ``retry`` (this run booted the simulator),
    again UI_TREE_RETRY_EVERY_S after each attempt that failed with
    RETRIED_CODE or did not finish within CLI_TIMEOUT, until it succeeds, no
    attempt starting later than UI_TREE_RETRY_S after the first; any other
    failure ends it at once. Each attempt is recorded in
    ``steps.ui_tree_attempts`` with the companions serving `udid` after it
    (a timed-out one with ``timed_out``, also in the calls). Returns the
    last reply, the attempts, and why the simulator is taken as not ready
    when the retry ran out (else None)."""
    attempts: list[dict[str, Any]] = []
    report["steps"]["ui_tree_attempts"] = attempts
    began = clock()
    while True:
        autonom.last_exit = None
        started = clock()
        timed_out = False
        try:
            tree = autonom("ui", "tree")
        except subprocess.TimeoutExpired:
            timed_out = True
            tree = {"ok": False, "error": f"ui tree did not finish within {CLI_TIMEOUT} s"}
            autonom.calls.append({"argv": ["ui", "tree"], "exit": None,
                                  "seconds": round(clock() - started, 1), "ok": False,
                                  "error_code": None, "timed_out": True})
        attempt = {"seconds": round(clock() - began, 1), "exit": autonom.last_exit,
                   "error_code": tree.get("error_code")}
        if timed_out:
            attempt["timed_out"] = True
        attempt["bench_companions"] = bench(companions(), udid)
        attempts.append(attempt)
        if tree.get("ok") or not retry or not (timed_out
                                               or tree.get("error_code") == RETRIED_CODE):
            return tree, attempts, None
        elapsed = clock() - began
        if elapsed + UI_TREE_RETRY_EVERY_S > UI_TREE_RETRY_S:
            last = "timed out" if timed_out else f"error_code {tree.get('error_code')}"
            return tree, attempts, (
                f"ui tree did not succeed in {len(attempts)} attempts over {elapsed:.1f} s "
                f"after this run booted the simulator (last attempt: {last})")
        pause(UI_TREE_RETRY_EVERY_S)


def session_steps(report: dict[str, Any], autonom: Autonom, home: Path, udid: str,
                  decoy: subprocess.Popen, *, booted_here: bool = False) -> str | None:
    """Steps 2 and 3. Returns why the simulator this run booted is taken as
    not ready (the `ui tree` retry ran out), else None; the steps after the
    read run and are recorded either way."""
    steps, checks = report["steps"], report["checks"]
    report["session_attempted"] = True
    started = autonom("session", "start", "--app-id", SETTINGS, "--ui-backend", "idb")
    session_id = (started.get("session") or {}).get("session_id")
    steps["session_start"] = {"ok": started.get("ok"), "session_id": session_id,
                              "error_code": started.get("error_code")}
    checks["session_start_ok"] = bool(started.get("ok"))
    if not started.get("ok"):
        raise StepFailed(f"session start: {started.get('error_code')} {started.get('error')}")
    tree, attempts, not_ready = ui_tree_attempts(report, autonom, udid, retry=booted_here)
    steps["ui_tree"] = {"ok": tree.get("ok"), "ui_backend": tree.get("ui_backend"),
                        "nodes": len(tree.get("nodes") or []),
                        "error_code": tree.get("error_code"), "attempts": len(attempts),
                        "timed_out": bool(attempts[-1].get("timed_out"))}
    if not_ready:
        steps["ui_tree"]["not_ready"] = not_ready
    checks["ui_tree_ok"] = bool(tree.get("ok"))
    running = attempts[-1]["bench_companions"]
    # every companion that served the UDID after any attempt: a failed
    # attempt's companion that died and was replaced makes two
    seen = sorted({pid for attempt in attempts for pid in attempt["bench_companions"]})
    rows = registry_rows(home, udid)
    steps["registry"] = {"bench_companions": running, "bench_companions_seen": seen,
                         "rows": [{key: row.get(key) for key in
                                   ("pid", "owner", "udid", "signature", "spawned_by")}
                                  for row in rows]}
    checks["one_companion_started"] = len(running) == 1 and seen == running
    checks["companion_registered_to_session"] = (
        len(rows) == 1 and rows[0].get("owner") == session_id
        and rows[0].get("pid") in running)
    companion = rows[0].get("pid") if len(rows) == 1 else (running[0] if running else None)

    stopped = autonom("session", "stop")
    # only once the stop returned: a stop cut short is tried again in the cleanup
    report["session_attempted"] = False
    reaped = next((item.get("detail") or {} for item in stopped.get("teardown") or []
                   if item.get("action") == "session_processes"), {})
    terminated = [(item.get("pid"), item.get("result"))
                  for item in reaped.get("terminated") or []]
    left = [item.get("pid") for warning in stopped.get("warnings") or []
            if warning.get("code") == "companion_left_running"
            for item in warning.get("companions") or []]
    gone = wait_until(lambda: not bench(companions(), udid), STOP_WAIT_S)
    steps["session_stop"] = {"ok": stopped.get("ok"), "terminated": terminated,
                             "companion_left_running": left,
                             "bench_companions_after": bench(companions(), udid)}
    checks["session_stop_ok"] = bool(stopped.get("ok"))
    checks["session_stop_terminated_the_companion"] = (
        companion is not None and (companion, "terminated") in terminated)
    checks["no_bench_companion_after_stop"] = gone
    checks["decoy_alive_after_stop"] = decoy.poll() is None and pid_alive(decoy.pid)
    checks["decoy_not_reported"] = decoy.pid not in left
    checks["decoy_never_signalled"] = all(pid != decoy.pid for pid, _ in terminated)
    return not_ready


def cleanup_step(report: dict[str, Any], name: str, step) -> None:
    """Run one cleanup step; a failure is recorded and the next step still runs."""
    try:
        step()
    except Exception as exc:  # noqa: BLE001 - every later cleanup step must still run
        report["steps"]["cleanup"].setdefault("errors", {})[name] = (
            f"{type(exc).__name__}: {exc}"[-300:])


def stop_leftovers(udid: str) -> list[dict[str, Any]]:
    """Stop the companions still serving `udid` at the end. Only reached
    after step 1 saw none for it, so this run started each of them; the
    pid's command line is read again right before the signal."""
    stopped = []
    for pid in bench(companions(), udid):
        if not serves(pid_command(pid), udid):
            continue
        os.kill(pid, signal.SIGTERM)
        if not wait_until(lambda: not pid_alive(pid), 5):
            os.kill(pid, signal.SIGKILL)
        stopped.append({"pid": pid, "gone": wait_until(lambda: not pid_alive(pid), 5)})
    return stopped


def stop_decoy(decoy: subprocess.Popen) -> bool:
    if decoy.poll() is None:
        decoy.terminate()
        try:
            decoy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            decoy.kill()
            decoy.wait(timeout=10)
    return not pid_alive(decoy.pid)


def record_failure(report: dict[str, Any], exc: BaseException) -> int | None:
    """Record why the steps stopped under `error`; the exit code a signal
    or Ctrl-C asks for, None otherwise."""
    if isinstance(exc, Terminated):
        report["error"] = f"Terminated: {exc}"
        return 128 + exc.signum
    if isinstance(exc, KeyboardInterrupt):  # raised without our handler, e.g. before it was set
        report["error"] = "KeyboardInterrupt: the run was interrupted"
        return 128 + int(signal.SIGINT)
    report["error"] = f"{type(exc).__name__}: {exc}"[-400:]
    if not isinstance(exc, (StepFailed, subprocess.TimeoutExpired)):
        report["traceback"] = traceback.format_exc()[-2000:]  # a defect, not a failed step
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--udid", required=True, help="the private bench simulator")
    parser.add_argument("--boot", action="store_true",
                        help="boot the --udid simulator when it is Shutdown and shut it down "
                             "at the end only then; no other simulator is touched (a run "
                             "without --boot does the same)")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--idb", help="the real idb client (default: idb on PATH)")
    parser.add_argument("--max-load", type=float, default=60.0,
                        help="refuse to start at or above this 1-minute load (default 60)")
    parser.add_argument("--keep-home", action="store_true",
                        help="keep the temporary AUTONOM_HOME (sessions, registry)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_case(args)
    except Terminated as exc:
        # the one signal landed while a failed step was being recorded: the
        # cleanup and the report already ran in run_case's `finally`
        return 128 + exc.signum


def run_case(args: argparse.Namespace) -> int:
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    out = args.evidence_dir / "ios-companion.json"
    report: dict[str, Any] = {
        "case": "CASE-004", "verification": "VER-004", "udid": args.udid,
        "started_at": now(), "max_load": args.max_load, "boot_requested": bool(args.boot),
        "environment": {"python": sys.version.split()[0]},
        "steps": {}, "checks": {}, "steps_completed": False, "ok": False,
    }
    home = Path(tempfile.mkdtemp(prefix="autonom-ios-companion-"))
    autonom = Autonom(args.udid, home, args.idb)
    signals = Signals()
    decoys: list[subprocess.Popen] = []
    booted_here = False
    exit_code: int | None = None
    not_ready: str | None = None
    try:
        signals.catch()
        xcode = run(["xcodebuild", "-version"], timeout=60, check=False).stdout.split("\n")
        report["environment"]["xcode"] = " ".join(line.strip() for line in xcode if line.strip())
        idb = args.idb or shutil.which("idb")
        if not idb:
            raise StepFailed("idb not found; pass --idb")
        report["environment"]["idb"] = idb
        state = simulator_state(args.udid)
        report["initial_state"] = state
        if state not in ("Shutdown", "Booted"):
            raise StepFailed(f"simulator is {state}; wait until it is Booted or Shutdown")
        record_step(report, args)
        decoy = start_decoy(report, args.udid, decoys, signals)
        if state == "Shutdown":
            booted_here = True  # first: a boot cut short is still shut down
            boot_if_shutdown(args.udid, state)
            time.sleep(BOOT_SETTLE_S)
        report["steps"]["boot"] = {"booted_here": booted_here,
                                   "boot_requested": bool(args.boot)}
        not_ready = session_steps(report, autonom, home, args.udid, decoy,
                                  booted_here=booted_here)
        signals.disarm()
        report["steps_completed"] = True
    except BaseException as exc:  # noqa: BLE001 - every way out is recorded, never a pass
        signals.disarm()  # first: a signal from here on cannot skip the cleanup
        exit_code = record_failure(report, exc)
        if not isinstance(exc, (Exception, KeyboardInterrupt)):
            raise  # SystemExit and the like go on, after the cleanup below
    finally:
        signals.ignore()  # the cleanup runs to its end
        if signals.pending is not None and exit_code is None:
            exit_code = 128 + signals.pending  # it came while the held step failed
        report["calls"] = autonom.calls
        report["cleanup"] = "pending"
        cleanup: dict[str, Any] = {"booted_here": booted_here}
        report["steps"]["cleanup"] = cleanup
        checks = report["checks"]
        # evidence of a run killed during the cleanup; a failed write must not
        # keep the decoy or a companion running
        cleanup_step(report, "save_before_cleanup", lambda: save(report, out))
        if report.pop("session_attempted", False):
            cleanup_step(report, "session_stop", lambda: cleanup.update(
                session_stop_ok=autonom("session", "stop", timeout=60).get("ok")))
        if decoys:
            cleanup["decoy_pids"] = [decoy.pid for decoy in decoys]
            checks["decoy_stopped_by_script"] = False
            cleanup_step(report, "decoy", lambda: checks.update(
                decoy_stopped_by_script=all([stop_decoy(decoy) for decoy in decoys])))
        if checks.get("no_bench_companion_before"):
            cleanup_step(report, "leftover_companions", lambda: cleanup.update(
                leftover_companions_stopped=stop_leftovers(args.udid)))
        if booted_here:
            cleanup_step(report, "shutdown",
                         lambda: shutdown_if_booted_here(args.udid, booted_here))
        if "initial_state" in report:
            checks["state_restored"] = False
            report["final_state"] = None

            def final_state() -> None:
                report["final_state"] = simulator_state(args.udid)
                checks["state_restored"] = report["final_state"] == report["initial_state"]
            cleanup_step(report, "final_state", final_state)
        if "record" in report["steps"]:
            checks["other_companions_unchanged"] = False

            def compare_others() -> None:
                after = companions()
                recorded = report["steps"]["record"]["other_companions"]
                now_running = others(after, args.udid)
                cleanup["other_companions_after"] = now_running
                cleanup["other_companions_new"] = sorted(set(now_running) - set(recorded))
                checks["other_companions_unchanged"] = all(
                    now_running.get(pid) == command for pid, command in recorded.items())
                checks["no_bench_companion_at_end"] = not bench(after, args.udid)
            cleanup_step(report, "other_companions", compare_others)
        if args.keep_home:
            cleanup["autonom_home"] = str(home)
        else:
            shutil.rmtree(home, ignore_errors=True)
        report["calls"] = autonom.calls
        report["host_load_end"] = host_load()
        report["finished_at"] = now()
        report["cleanup"] = "done"
        missing = [name for name in REQUIRED_CHECKS if name not in checks]
        report["missing_checks"] = missing
        report["ok"] = (report["steps_completed"] is True and "error" not in report
                        and not cleanup.get("errors") and not missing
                        and all(value is True for value in checks.values()))
        if (not_ready is not None and exit_code is None and report["steps_completed"] is True
                and "error" not in report and not cleanup.get("errors") and not missing
                and not cleanup.get("leftover_companions_stopped")
                and checks.get("ui_tree_ok") is False
                and all(value is True for name, value in checks.items()
                        if name != "ui_tree_ok")):
            # only the read never succeeded: the rest of the case (CO-02,
            # the decoy, the cleanup) ran and passed, so nothing Autonom did
            # failed and the simulator was not ready
            exit_code = EXIT_ENVIRONMENT
            report["environment_fault"] = not_ready
        summary = {"ok": report["ok"], "report": str(out), "error": report.get("error"),
                   "missing_checks": missing, "checks": checks}
        if "environment_fault" in report:
            summary["environment_fault"] = report["environment_fault"]
        try:
            save(report, out)
            print(json.dumps(summary, separators=(",", ":")))
        finally:
            signals.restore()
    if exit_code is not None:
        return exit_code
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
