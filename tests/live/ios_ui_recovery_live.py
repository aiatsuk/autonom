#!/usr/bin/env python3
"""Live check of the optional XCUITest UI backend on one iOS Simulator.

Not part of the unit suite: it boots (if needed), builds and runs the bundled
runner on the one explicit --udid and taps in Settings there. Use an isolated
test simulator only.

    python3 tests/live/ios_ui_recovery_live.py --udid <UDID> --evidence-dir DIR \
        [--idb PATH] [--keep-home] [--expect-xcode-major N]

Every run uses its own temporary AUTONOM_HOME, so it never reads, stops or
writes the user's sessions; the runner build is cached inside it and removed
with it (unless --keep-home). The simulator is booted only when it is
Shutdown, and shut down at the end only when this run booted it.

Steps, each against Settings (com.apple.Preferences), relaunched first:

0. idb (run first, on the fresh boot): a normal `--ui-backend idb` session
   returns a tree. It runs before any runner session so that the baseline
   cannot depend on what a runner left behind.
1. explicit: `--ui-backend xcuitest` session; the tree is served by the
   runner (`ui_backend: xcuitest`, nodes carry `xcuitest_ref`).
2. fallback: `auto` session with AUTONOM_IDB pointing to a temporary wrapper
   that forwards every idb call to the real idb except `ui describe-all`,
   which answers an empty tree. The tree must come from the runner with
   `fallback_reason: empty_accessibility_tree`; then one semantic tap on the
   General row, and a following tree must show the General page.
3. xcuitest_input: one `--ui-backend xcuitest` session, each sub-step a
   pass/fail field of its own:
   - text_entry: tap the Settings search field, type a fixed non-secret
     string; the field's value in the next runner tree must equal it.
   - long_press: a 1 s press on that field; the next tree must show the text
     edit menu ("Select All", "Select", "Copy" or "Paste"), absent before.
     The strongest oracle Settings offers: a plain tap on the field opens no
     such menu here, and the press is the only input between the two trees.
   - swipe: Settings relaunched; an upward swipe over the list must reveal at
     least one row absent from the tree before it and move the General row
     up by more than 50 points (or off the tree).
   - home: Home through the runner; the tree before it must hold the
     Settings application and the next runner tree, answered ok, must not:
     the runner serves an app's tree only while the app is running, not
     suspended, and Settings, sent to the background, stops being served.
     (idb is not asked: a direct idb call would start a companion no
     session owns.)
4. runner stopped: after each `session stop`, no runner `xcodebuild` process
   for this UDID is left.

`ok` also requires the Simulator to end in the state it started in
(`final_state == initial_state`) and `xcodebuild -version` to report the
expected Xcode major version (`--expect-xcode-major`, default 27).

Writes <evidence-dir>/ios_recovery.json with per-step results and `ok`. It
records backends, counts and a few element labels; never runner logs, and of
the typed text only its length (the command line is recorded with the text
replaced, and the report is checked for it before it is written). Exit 0
when every step passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
# Typed into the Settings search field: fixed and non-secret, yet distinctive
# enough that the evidence can be checked for it. Only its length is recorded.
TYPED = "kestrel 42"
# The text edit menu a long press on a field with text opens.
EDIT_MENU = ("Select All", "Select", "Copy", "Paste")


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
                 expect_ok: bool = True, secret: str | None = None) -> dict:
        command = [sys.executable, str(CLI), *argv, "--udid", self.udid]
        started = time.monotonic()
        completed = run(command, timeout=timeout, env=env or self.env, check=False)
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        try:
            payload = json.loads(stream)
        except ValueError:
            payload = {"ok": False, "error": (stream or "").strip()[-300:]}
        # A typed value never reaches the evidence: only its length.
        recorded = [f"<{len(secret)} chars>" if secret is not None and item == secret else item
                    for item in argv]
        if secret is not None:
            payload = {key: value for key, value in payload.items()
                       if key in ("ok", "backend", "error_code", "input_backend")}
        self.calls.append({"argv": recorded, "exit": completed.returncode,
                           "seconds": round(time.monotonic() - started, 1),
                           "error_code": payload.get("error_code")})
        if expect_ok and not payload.get("ok"):
            raise StepFailed(f"autonom {' '.join(recorded)}: {payload.get('error_code')} "
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


def xcode_major(version: str) -> int | None:
    match = re.search(r"\bXcode\s+(\d+)", version)
    return int(match.group(1)) if match else None


def row_labels(nodes: list[dict]) -> set[str]:
    """Settings' top-level rows: buttons whose id is com.apple.settings.*."""
    return {str(node.get("desc")) for node in nodes
            if str(node.get("resource_id") or "").startswith("com.apple.settings.")
            and node.get("desc")}


def row_top(nodes: list[dict], label: str) -> int | None:
    for node in nodes:
        if node.get("desc") == label and str(node.get("resource_id") or "").startswith(
                "com.apple.settings.") and node.get("bounds"):
            return int(node["bounds"][1])
    return None


def search_field(nodes: list[dict]) -> dict | None:
    return next((n for n in nodes if n.get("role") in ("textfield", "searchfield")
                 and n.get("desc") == "Search"), None)


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


def _substep(results: dict, name: str, body) -> None:
    try:
        results[name] = body()
    except (StepFailed, subprocess.TimeoutExpired) as exc:
        results[name] = {"ok": False, "error": str(exc)[-400:]}


def step_inputs(autonom: Autonom, udid: str) -> dict:
    relaunch_settings(udid)
    autonom("session", "start", "--app-id", SETTINGS, "--ui-backend", "xcuitest")
    result: dict = {}
    try:
        def text_entry() -> dict:
            before = autonom("ui", "tree", timeout=FIRST_UI_TIMEOUT).get("nodes") or []
            field = search_field(before)
            out: dict = {"field_found": field is not None, "typed_length": len(TYPED)}
            tap = autonom("ui", "tap", "--role", (field or {}).get("role") or "textfield",
                          "--desc", "Search", "--mode", "exact", "--index", "0",
                          expect_ok=False)
            out["tap_backend"] = tap.get("backend")
            time.sleep(1.5)
            typed = autonom("ui", "type", TYPED, "--sensitive", expect_ok=False, secret=TYPED)
            out["type_ok"] = bool(typed.get("ok"))
            out["type_backend"] = typed.get("backend")
            out["type_error_code"] = typed.get("error_code")
            time.sleep(1.5)
            after = autonom("ui", "tree").get("nodes") or []
            value = (search_field(after) or {}).get("text") or ""
            out["field_value_length"] = len(value)
            out["field_value_matches"] = value == TYPED
            out["ok"] = (out["field_found"] and tap.get("ok") is True
                         and out["tap_backend"] == "xcuitest" and out["type_ok"]
                         and out["type_backend"] == "xcuitest" and out["field_value_matches"])
            return out

        def long_press() -> dict:
            before = labels(autonom("ui", "tree").get("nodes") or [])
            press = autonom("ui", "tap", "--role", "textfield", "--desc", "Search",
                            "--mode", "exact", "--index", "0", "--duration", "1000",
                            expect_ok=False)
            time.sleep(1.5)
            after = labels(autonom("ui", "tree").get("nodes") or [])
            out = {"press_ok": bool(press.get("ok")), "backend": press.get("backend"),
                   "error_code": press.get("error_code"),
                   "menu_before": sorted(before & set(EDIT_MENU)),
                   "menu_after": sorted(after & set(EDIT_MENU))}
            out["ok"] = (out["press_ok"] and out["backend"] == "xcuitest"
                         and not out["menu_before"] and bool(out["menu_after"]))
            return out

        def swipe() -> dict:
            relaunch_settings(udid)  # search closed, list at the top
            before = autonom("ui", "tree").get("nodes") or []
            move = autonom("ui", "swipe", "--from", "201,650", "--to", "201,250",
                           "--duration", "0.3", expect_ok=False)
            time.sleep(2)
            after = autonom("ui", "tree").get("nodes") or []
            revealed = sorted(row_labels(after) - row_labels(before))
            top_before, top_after = row_top(before, ROW), row_top(after, ROW)
            out = {"swipe_ok": bool(move.get("ok")), "backend": move.get("backend"),
                   "error_code": move.get("error_code"), "rows_before": len(row_labels(before)),
                   "rows_revealed": revealed, "general_top_before": top_before,
                   "general_top_after": top_after}
            moved = top_before is not None and (top_after is None or top_after < top_before - 50)
            out["ok"] = (out["swipe_ok"] and out["backend"] == "xcuitest" and bool(revealed)
                         and moved)
            return out

        def home() -> dict:
            before = autonom("ui", "tree").get("nodes") or []
            press = autonom("ui", "key", "HOME", expect_ok=False)
            time.sleep(3)
            after = autonom("ui", "tree")
            nodes = after.get("nodes") or []

            def holds_settings(items: list[dict]) -> bool:
                return any(n.get("role") == "app" and n.get("desc") == "Settings" for n in items)
            out = {"key_ok": bool(press.get("ok")), "backend": press.get("backend"),
                   "error_code": press.get("error_code"),
                   "settings_in_tree_before": holds_settings(before),
                   "tree_ok": bool(after.get("ok")), "tree_backend": after.get("ui_backend"),
                   "settings_in_tree_after": holds_settings(nodes)}
            out["ok"] = (out["key_ok"] and out["backend"] == "xcuitest"
                         and out["settings_in_tree_before"] and out["tree_ok"]
                         and out["tree_backend"] == "xcuitest"
                         and not out["settings_in_tree_after"])
            return out

        _substep(result, "text_entry", text_entry)
        _substep(result, "long_press", long_press)
        _substep(result, "swipe", swipe)
        _substep(result, "home", home)
        result["ok"] = all(result[name].get("ok")
                           for name in ("text_entry", "long_press", "swipe", "home"))
    finally:
        stopped = autonom("session", "stop", expect_ok=False)
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
    parser.add_argument("--expect-xcode-major", type=int, default=27,
                        help="the Xcode major version xcodebuild must report (default 27)")
    args = parser.parse_args(argv)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"udid": args.udid, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "steps": {}, "ok": False}
    xcode = run(["xcodebuild", "-version"], timeout=30, check=False).stdout.split("\n")
    report["xcode"] = " ".join(line.strip() for line in xcode if line.strip())
    report["xcode_major"] = xcode_major(report["xcode"])
    report["expected_xcode_major"] = args.expect_xcode_major
    report["xcode_ok"] = report["xcode_major"] == args.expect_xcode_major
    steps_ok = False
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
        # The idb baseline goes first, on the fresh boot, so that it cannot
        # depend on what a runner session left behind.
        steps = [
            ("idb_tree", lambda: step_idb(autonom, args.udid)),
            ("explicit_xcuitest", lambda: step_explicit(autonom, args.udid)),
            ("auto_fallback", lambda: step_fallback(autonom, args.udid,
                                                    fake_idb(scratch, real_idb))),
            ("xcuitest_input", lambda: step_inputs(autonom, args.udid)),
        ]
        for name, step in steps:
            try:
                report["steps"][name] = step()
            except (StepFailed, subprocess.TimeoutExpired) as exc:
                report["steps"][name] = {"ok": False, "error": str(exc)[-400:]}
        left = wait_no_runner(args.udid, 5)
        report["steps"]["runner_stopped"] = {"ok": not left, "runner_left": left}
        steps_ok = all(step.get("ok") for step in report["steps"].values())
    except (StepFailed, subprocess.TimeoutExpired) as exc:
        report["error"] = str(exc)[-400:]
    finally:
        # Leave no session behind in the temporary home, whatever failed.
        autonom("session", "stop", expect_ok=False)
        report["calls"] = autonom.calls
        if booted_here:
            run(["xcrun", "simctl", "shutdown", args.udid], timeout=120, check=False)
        report["booted_here"] = booted_here
        try:
            report["final_state"] = simulator_state(args.udid)
        except (StepFailed, subprocess.TimeoutExpired, ValueError) as exc:
            report["final_state"] = None
            report.setdefault("error", str(exc)[-400:])
        report["state_restored"] = (report.get("initial_state") is not None
                                    and report["final_state"] == report.get("initial_state"))
        report["ok"] = bool(steps_ok and report["xcode_ok"] and report["state_restored"])
        shutil.rmtree(scratch, ignore_errors=True)
        if args.keep_home:
            report["autonom_home"] = str(home)
        else:
            shutil.rmtree(home, ignore_errors=True)
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out = args.evidence_dir / "ios_recovery.json"
        text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if TYPED in text:
            # Belt and braces: a typed value that slipped into an error string.
            report["ok"] = False
            report["typed_text_redacted"] = True
            text = json.dumps(report, indent=2, ensure_ascii=False).replace(
                TYPED, f"<{len(TYPED)} chars>") + "\n"
        out.write_text(text, encoding="utf-8")
        print(json.dumps({"ok": report["ok"], "report": str(out)}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
