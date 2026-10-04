#!/usr/bin/env python3
"""Live check of the optional XCUITest UI backend on one iOS Simulator.

Not part of the unit suite: it boots (if needed), builds and runs the bundled
runner on the one explicit --udid and taps in Settings there. Use an isolated
test simulator only.

    python3 tests/live/ios_ui_recovery_live.py --udid <UDID> --evidence-dir DIR \
        [--idb PATH] [--keep-home]

Every run uses its own temporary AUTONOM_HOME, so it never reads, stops or
writes the user's sessions; the runner build is cached inside it and removed
with it (unless --keep-home). The simulator is booted only when it is
Shutdown, and shut down at the end only when this run booted it.

Steps, each against Settings (com.apple.Preferences), relaunched first:

1. explicit: `--ui-backend xcuitest` session; the tree is served by the
   runner (`ui_backend: xcuitest`, nodes carry `xcuitest_ref`).
2. fallback: `auto` session with AUTONOM_IDB pointing to a temporary wrapper
   that forwards every idb call to the real idb except `ui describe-all`,
   which answers an empty tree. The tree must come from the runner with
   `fallback_reason: empty_accessibility_tree`; then one semantic tap on the
   General row, and a following tree must show the General page.
3. idb: a normal `--ui-backend idb` session still returns a tree.
4. runner stopped: after each `session stop`, no runner `xcodebuild` process
   for this UDID is left.

Writes <evidence-dir>/ios_recovery.json with per-step results and `ok`. It
records backends, counts and a few element labels; never typed text (nothing
is typed) and never runner logs. Exit 0 when every step passed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "autonom.py"
SETTINGS = "com.apple.Preferences"
ROW = "General"
# Labels only the General page shows; any one proves the tap opened it.
GENERAL_PAGE = ("About", "Software Update", "AirDrop", "iPhone Storage")
# The first runner use builds it (minutes on a cold cache) and installs it.
FIRST_UI_TIMEOUT = 900
CLI_TIMEOUT = 180


class StepFailed(Exception):
    pass


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
                return device.get("state", "")
    raise StepFailed(f"no simulator {udid}")


class Autonom:
    def __init__(self, udid: str, home: Path, idb: str | None) -> None:
        self.udid = udid
        self.env = {**os.environ, "AUTONOM_HOME": str(home)}
        for name in ("AUTONOM_IDB", "AUTONOM_UI_BACKEND", "AUTONOM_SIMCTL"):
            self.env.pop(name, None)
        if idb:
            self.env["AUTONOM_IDB"] = idb
        self.calls: list[dict] = []

    def __call__(self, *argv: str, timeout: float = CLI_TIMEOUT, env: dict | None = None,
                 expect_ok: bool = True) -> dict:
        command = [sys.executable, str(CLI), *argv, "--udid", self.udid]
        started = time.monotonic()
        completed = run(command, timeout=timeout, env=env or self.env, check=False)
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        try:
            payload = json.loads(stream)
        except ValueError:
            payload = {"ok": False, "error": (stream or "").strip()[-300:]}
        self.calls.append({"argv": list(argv), "exit": completed.returncode,
                           "seconds": round(time.monotonic() - started, 1),
                           "error_code": payload.get("error_code")})
        if expect_ok and not payload.get("ok"):
            raise StepFailed(f"autonom {' '.join(argv)}: {payload.get('error_code')} "
                             f"{payload.get('error')}")
        return payload


def runner_processes(udid: str) -> list[str]:
    listing = run(["ps", "-axo", "pid=,command="], timeout=30).stdout
    return [line.strip() for line in listing.splitlines()
            if "xcodebuild" in line and "test-without-building" in line and udid in line]


def wait_no_runner(udid: str, seconds: float = 30) -> list[str]:
    deadline = time.monotonic() + seconds
    left = runner_processes(udid)
    while left and time.monotonic() < deadline:
        time.sleep(1)
        left = runner_processes(udid)
    return left


def relaunch_settings(udid: str) -> None:
    run(["xcrun", "simctl", "terminate", udid, SETTINGS], timeout=60, check=False)
    run(["xcrun", "simctl", "launch", udid, SETTINGS], timeout=60)
    time.sleep(2)


def labels(nodes: list[dict]) -> set[str]:
    return {str(node.get(key)) for node in nodes for key in ("desc", "text")
            if node.get(key)}


def fake_idb(directory: Path, real_idb: str) -> Path:
    wrapper = directory / "idb"
    wrapper.write_text(
        "#!/bin/sh\n"
        "# Forwards to the real idb, except `ui describe-all`: an empty tree.\n"
        'if [ "$1" = "ui" ] && [ "$2" = "describe-all" ]; then\n'
        "  echo '[]'\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real_idb}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def step_explicit(autonom: Autonom, udid: str) -> dict:
    relaunch_settings(udid)
    autonom("session", "start", "--app-id", SETTINGS, "--ui-backend", "xcuitest")
    try:
        tree = autonom("ui", "tree", timeout=FIRST_UI_TIMEOUT)
        nodes = tree.get("nodes") or []
        result = {"ui_backend": tree.get("ui_backend"), "nodes": len(nodes),
                  "nodes_with_xcuitest_ref": sum(1 for n in nodes if n.get("xcuitest_ref"))}
        result["ok"] = (result["ui_backend"] == "xcuitest" and result["nodes"] > 0
                        and result["nodes_with_xcuitest_ref"] > 0)
    finally:
        stopped = autonom("session", "stop", expect_ok=False)
    result["session_stop_ok"] = bool(stopped.get("ok"))
    result["runner_left"] = wait_no_runner(udid)
    result["ok"] = result["ok"] and result["session_stop_ok"] and not result["runner_left"]
    return result


def step_fallback(autonom: Autonom, udid: str, wrapper: Path) -> dict:
    env = {**autonom.env, "AUTONOM_IDB": str(wrapper)}
    relaunch_settings(udid)
    autonom("session", "start", "--app-id", SETTINGS, env=env)
    result: dict = {}
    try:
        before = autonom("ui", "tree", timeout=FIRST_UI_TIMEOUT, env=env)
        before_nodes = before.get("nodes") or []
        before_labels = labels(before_nodes)
        result["tree"] = {"ui_backend": before.get("ui_backend"),
                          "fallback_reason": before.get("fallback_reason"),
                          "nodes": len(before_nodes),
                          "row_present": ROW in before_labels,
                          "general_page_before": sorted(before_labels & set(GENERAL_PAGE))}
        candidates = [n for n in before_nodes if n.get("desc") == ROW or n.get("text") == ROW]
        role = next((r for r in ("cell", "button", "text")
                     if any(n.get("role") == r for n in candidates)), None)
        selector = ["--desc", ROW, "--mode", "exact"] + (["--role", role] if role else [])
        tap = autonom("ui", "tap", *selector, "--index", "0", env=env, expect_ok=False)
        result["tap"] = {"ok": bool(tap.get("ok")), "backend": tap.get("backend"),
                         "input_backend": tap.get("input_backend"),
                         "error_code": tap.get("error_code"), "selector": selector}
        time.sleep(2)
        after = autonom("ui", "tree", env=env)
        after_labels = labels(after.get("nodes") or [])
        result["after"] = {"ui_backend": after.get("ui_backend"),
                           "general_page": sorted(after_labels & set(GENERAL_PAGE))}
        result["ok"] = (result["tree"]["ui_backend"] == "xcuitest"
                        and result["tree"]["fallback_reason"] == "empty_accessibility_tree"
                        and result["tree"]["nodes"] > 0 and result["tree"]["row_present"]
                        and not result["tree"]["general_page_before"]
                        and result["tap"]["ok"] and result["tap"]["backend"] == "xcuitest"
                        and bool(result["after"]["general_page"]))
    finally:
        stopped = autonom("session", "stop", env=env, expect_ok=False)
    result["session_stop_ok"] = bool(stopped.get("ok"))
    result["runner_left"] = wait_no_runner(udid)
    result["ok"] = bool(result.get("ok")) and result["session_stop_ok"] and not result["runner_left"]
    return result


def step_idb(autonom: Autonom, udid: str) -> dict:
    relaunch_settings(udid)
    autonom("session", "start", "--app-id", SETTINGS, "--ui-backend", "idb")
    try:
        tree = autonom("ui", "tree")
        nodes = tree.get("nodes") or []
        result = {"ui_backend": tree.get("ui_backend"), "nodes": len(nodes),
                  "row_present": ROW in labels(nodes)}
        result["ok"] = result["ui_backend"] == "idb" and result["nodes"] > 0
    finally:
        stopped = autonom("session", "stop", expect_ok=False)
    result["session_stop_ok"] = bool(stopped.get("ok"))
    result["ok"] = result["ok"] and result["session_stop_ok"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--udid", required=True, help="an isolated test simulator")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--idb", help="the real idb client (default: idb on PATH)")
    parser.add_argument("--keep-home", action="store_true",
                        help="keep the temporary AUTONOM_HOME (runner build cache, sessions)")
    args = parser.parse_args(argv)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"udid": args.udid, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "steps": {}, "ok": False}
    xcode = run(["xcodebuild", "-version"], timeout=30, check=False).stdout.split("\n")
    report["xcode"] = " ".join(line.strip() for line in xcode if line.strip())
    real_idb = args.idb or shutil.which("idb")
    home = Path(tempfile.mkdtemp(prefix="autonom-ios-recovery-"))
    scratch = Path(tempfile.mkdtemp(prefix="autonom-fake-idb-"))
    booted_here = False
    autonom = Autonom(args.udid, home, args.idb)
    try:
        state = simulator_state(args.udid)
        report["initial_state"] = state
        if state == "Shutdown":
            run(["xcrun", "simctl", "bootstatus", args.udid, "-b"], timeout=600)
            booted_here = True
        elif state != "Booted":
            raise StepFailed(f"simulator is {state}; wait until it is Booted or Shutdown")
        if not real_idb:
            raise StepFailed("idb not found; pass --idb")
        steps = [
            ("explicit_xcuitest", lambda: step_explicit(autonom, args.udid)),
            ("auto_fallback", lambda: step_fallback(autonom, args.udid,
                                                    fake_idb(scratch, real_idb))),
            ("idb_tree", lambda: step_idb(autonom, args.udid)),
        ]
        for name, step in steps:
            try:
                report["steps"][name] = step()
            except (StepFailed, subprocess.TimeoutExpired) as exc:
                report["steps"][name] = {"ok": False, "error": str(exc)[-400:]}
        left = wait_no_runner(args.udid, 5)
        report["steps"]["runner_stopped"] = {"ok": not left, "runner_left": left}
        report["ok"] = all(step.get("ok") for step in report["steps"].values())
    except (StepFailed, subprocess.TimeoutExpired) as exc:
        report["error"] = str(exc)[-400:]
    finally:
        # Leave no session behind in the temporary home, whatever failed.
        autonom("session", "stop", expect_ok=False)
        report["calls"] = autonom.calls
        if booted_here:
            run(["xcrun", "simctl", "shutdown", args.udid], timeout=120, check=False)
        report["booted_here"] = booted_here
        report["final_state"] = simulator_state(args.udid)
        shutil.rmtree(scratch, ignore_errors=True)
        if args.keep_home:
            report["autonom_home"] = str(home)
        else:
            shutil.rmtree(home, ignore_errors=True)
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out = args.evidence_dir / "ios_recovery.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"ok": report["ok"], "report": str(out)}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
