#!/usr/bin/env python3
"""Live end-to-end check of `autonom pretest` on one test emulator or Simulator (tp-029).

Not part of the unit suite: it installs and launches a real app, runs a flow, forces an
HTTP 500 through the MITM proxy and packs the result. Test devices only:

    python3 tests/live/pretest_live.py --platform android --serial emulator-5580 \
        --app-id com.example.app [--apk app-debug.apk] --flows DIR --evidence-dir DIR \
        [--mock-path PATTERN] [--proxy-port N] [--in-app-proxy] [--env KEY=VALUE]... \
        [--ui] [--keep-home] [--dry-run]
    python3 tests/live/pretest_live.py --platform ios --udid <UDID> --app-id <bundle> \
        [--app Runner.app] --flows DIR --evidence-dir DIR [...the same options]
    python3 tests/live/pretest_live.py --self-test   (pure helpers and refusals; no device)

Target rules, checked before anything else runs: the target is only the one named with
--serial (Android) or --udid (iOS); there is no default device, and emulator-5554 is always
refused. A named target that is not online (Android `adb get-state` is not `device`; iOS not
Booted or Shutdown) stops the run before any session starts. A Shutdown Simulator is booted
and shut down again at the end.

Every run uses its own temporary AUTONOM_HOME (sessions, journal, proxy, mock registry),
removed at the end unless --keep-home, and a temporary git repository whose one committed
change on branch `live-change` against `main` is covered by a flow from --flows: a file
matching the flow's `properties.covers` glob, or, for a flow without one, a copy of --flows
inside the repository in which the flow file itself changes.

Steps:
  1. `session start` (install --apk/--app when given, launch --app-id).
  2. Capture: with --proxy-port or --mock-path, `network start` (on --proxy-port when
     given) and, unless --in-app-proxy (the app routes itself through the proxy),
     `network attach`. Autonom starts the proxy with `connection_strategy=lazy`, so a
     mocked request is answered without connecting upstream; the script writes no
     mitmproxy config of its own. `proxy_lazy_connect` reports whether the running
     proxy's argv carries the setting (read from the proxy process named by
     `network start`/`network status`; left out when it cannot be read).
  3. `pretest start` with a generated ticket (holding a fake bearer marker that must not
     reach the pack) and the repository's diff.
  4. Check 1: the covering flow, through `pretest flows` (or `check add/begin` + `flow run`
     + `check end` when the context selected none).
  5. Check 2 (`--kind network`, kit case `server-error` with --mock-path): a mock forcing
     HTTP 500 on --mock-path (a plain path becomes `*<path>*`, so the full URL matches
     with any query string), then the navigation flow replayed under the mock with
     `flow run` inside the open check, so its actions and requests belong to the check:
     the first flow `pretest start` selected (the regression flow covering the diff),
     else the flow chosen for the diff. Many apps request an endpoint only when its
     screen opens, not at launch, so a bare relaunch would never hit the mock. The
     flow's own assertions are expected to fail under the forced 500; its run id and
     status are recorded (`nav_flow`, `nav_flow_run_id`, `nav_flow_status`) and do not
     stop the check. Without a flow file (or without --mock-path) a fresh relaunch
     takes its place. Then a settle wait, a screenshot and the captured mocked 500s; a
     finding with the screenshot (and request) as evidence. The check ends
     `inconclusive` when a mocked 500 was seen (a person judges the screen) and
     `blocked` when none was.
  5b. Check 3 (`--kind network`, kit case `slow`, with --mock-path only): the same mock
     is changed to `--delay 3` and the navigation flow replayed; `delayed_duration_s` is
     the longest captured duration of a request that rule answered. Then it is changed
     to `--hang`, the flow replayed again, and the script waits up to 130 s for the
     proxy's 120 s hang cap; `incomplete_requests` counts the records of that rule with
     `completed: false`. The mock is removed at the end of the check. The check ends
     `inconclusive` when both were seen, else `blocked`.
  6. `pretest pack --out <evidence-dir>/pack-<platform>-<run>`, `report verify`,
     `pretest review <pack> return-to-dev --finding <id>`, `report verify` again,
     `pretest review-status`.
  7. With --ui: tests/live/pretest_view_check.mjs on the pack (four screenshots).
  8. Restore, whatever failed: the pre-test stopped if still active, the mock removed (and
     the registry read back), the device proxy detached, capture stopped, the session
     stopped, the Simulator shut down again if booted here.

Writes <evidence-dir>/pretest-<platform>.json (owner-only) with every observed value. It
holds ids, counts, verdicts, codes and paths only: never the ticket text, request bodies or
the bearer marker (the report is checked for it before it is written). `ok` needs two or
more checks, the flow check run, the mock hit when --mock-path is given (and then a
delayed request of at least 3 s and one incomplete request), the pack verified
before and after the review, the review recorded, the marker absent from the pack's
ticket.md, no mock left in the registry, every restore step and (with --ui) the view check.
Exit 0 when ok, 1 otherwise, 2 on bad arguments or a refused target (a JSON error with
`error_code` and `hint` on stderr). --dry-run prints the plan and touches no device.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "autonom.py"
VIEW_CHECK = Path(__file__).resolve().parent / "pretest_view_check.mjs"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from autonom_lib import errors  # noqa: E402  (stdlib only)
from autonom_lib.contracts import redact_value  # noqa: E402
from autonom_lib.network.proxy import LAZY_CONNECT_OPTION  # noqa: E402

SCHEMA = "autonom.pretest-live/v1"
REFUSED_TARGET = "emulator-5554"
TARGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
APP_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.]{0,254}$")
ENV_PAIR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
MOCK_PATH = re.compile(r"^[^\s\x00-\x1f\x7f]{1,300}$")
CLI_TIMEOUT = 180
INSTALL_TIMEOUT = 900
FLOW_TIMEOUT = 1200
VIEW_TIMEOUT = 600
SETTLE_SECONDS = 8
MOCK_STATUS = 500
DELAY_SECONDS = 3
# The proxy kills a held request after 120 s (`mocks.MAX_DELAY_S`); wait a little longer.
HANG_WAIT_SECONDS = 130
POLL_SECONDS = 5
# A measured delay may be a few milliseconds short of the configured one at most.
DELAY_TOLERANCE_S = 0.1
# Variables that could point the CLI at state outside the temporary home.
ISOLATED_ENV = ("AUTONOM_IDB_STATE_FILE", "AUTONOM_CAPTURES_DIR", "AUTONOM_UI_BACKEND")
GIT = ["git", "-c", "user.name=pretest-live", "-c", "user.email=pretest-live@localhost",
       "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
       "-c", "init.defaultBranch=main"]
FILE_MODE = 0o600
DIR_MODE = 0o700


class StepFailed(Exception):
    pass


class UsageError(Exception):
    """argparse refused the command line (exit 2 with a JSON error)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message or "invalid arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="pretest_live.py", description=__doc__.splitlines()[0],
                     epilog="Test devices only: emulator-5554 is always refused.")
    parser.add_argument("--platform", choices=("android", "ios"))
    parser.add_argument("--serial", help="the Android emulator to use (never emulator-5554)")
    parser.add_argument("--udid", help="the iOS Simulator to use")
    parser.add_argument("--app-id", help="package (Android) or bundle id (iOS) under test")
    parser.add_argument("--apk", type=Path, help="Android: install this APK at session start")
    parser.add_argument("--app", type=Path, help="iOS: install this .app at session start")
    parser.add_argument("--flows", type=Path, help="Flow v1 directory; one flow covers the diff")
    parser.add_argument("--evidence-dir", type=Path, help="where pretest-<platform>.json and the pack go")
    parser.add_argument("--mock-path", metavar="PATTERN",
                        help="force HTTP 500 on this path ('/api/x' or a URL glob); a plain "
                             "path matches the full URL with any query string, and also "
                             "longer paths with that prefix")
    parser.add_argument("--proxy-port", type=int, metavar="N", help="start capture on this port")
    parser.add_argument("--in-app-proxy", action="store_true",
                        help="the app routes itself through the proxy: do not attach the device")
    parser.add_argument("--env", action="append", metavar="KEY=VALUE", default=[],
                        help="non-secret flow variable (repeatable)")
    parser.add_argument("--ui", action="store_true", help="run pretest_view_check.mjs on the pack")
    parser.add_argument("--keep-home", action="store_true", help="keep the temporary AUTONOM_HOME")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; touch no device")
    parser.add_argument("--self-test", action="store_true", help="run the built-in tests and exit")
    return parser


# --- pure helpers (covered by --self-test) -------------------------------------------------

def proxy_lazy_connect(pid: Any, runner=None) -> bool | None:
    """Whether the proxy process `pid` runs with `connection_strategy=lazy`
    (Autonom sets it at `network start`), read from its argv with `ps`.
    None when there is no pid or the argv cannot be read."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    runner = runner or (lambda argv: run(argv, timeout=30, check=False))
    try:
        completed = runner(["ps", "-o", "command=", "-p", str(pid)])
    except (OSError, subprocess.TimeoutExpired):
        return None
    argv = (getattr(completed, "stdout", "") or "").strip()
    if getattr(completed, "returncode", 1) != 0 or not argv:
        return None
    return LAZY_CONNECT_OPTION in argv


def proxy_pid(*payloads: dict[str, Any]) -> int | None:
    """The proxy pid from `network start` (top level) or `network status` (`proxy`)."""
    for payload in payloads:
        for source in (payload, payload.get("proxy") if isinstance(payload, dict) else None):
            if isinstance(source, dict):
                pid = source.get("pid")
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                    return pid
    return None


def rule_records(listing: dict[str, Any], mock_id: str | None,
                 since_ms: int) -> list[dict[str, Any]]:
    """Captured records the rule `mock_id` answered (or held) since `since_ms`."""
    return [item for item in listing.get("requests") or []
            if isinstance(item, dict) and mock_id and item.get("mock_id") == mock_id
            and isinstance(item.get("started_at_ms"), int)
            and item["started_at_ms"] >= since_ms]


def delayed_duration_s(records: list[dict[str, Any]]) -> float | None:
    """The longest duration (seconds) of the completed records, or None."""
    durations = [item["duration_ms"] for item in records
                 if item.get("completed") is not False
                 and isinstance(item.get("duration_ms"), int)
                 and not isinstance(item.get("duration_ms"), bool)]
    return round(max(durations) / 1000, 3) if durations else None


def incomplete_count(records: list[dict[str, Any]]) -> int:
    return sum(1 for item in records if item.get("completed") is False)


def refuse(code: str, message: str, hint: str, **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(code, message, hint, **extra)


def target_of(args: argparse.Namespace) -> str:
    """The one named target, or a refusal. No default device exists."""
    if args.platform not in ("android", "ios"):
        raise refuse(errors.UNKNOWN_PLATFORM, "--platform must be android or ios",
                     "Pass --platform android --serial <emulator> or --platform ios --udid <udid>.")
    own, other = ("serial", "udid") if args.platform == "android" else ("udid", "serial")
    if getattr(args, other):
        raise refuse(errors.CONFLICTING_TARGET_FLAGS,
                     f"--{other} does not apply to --platform {args.platform}",
                     f"Name the target with --{own} only.")
    target = getattr(args, own)
    if not target:
        raise refuse(errors.NO_TARGET, f"--platform {args.platform} needs --{own}",
                     f"Name the test device explicitly with --{own}; this script never "
                     "picks a device on its own.")
    if target == REFUSED_TARGET:
        raise refuse(errors.INVALID_VALUE, f"{REFUSED_TARGET} is never used by this check",
                     "Run it on a dedicated test emulator such as emulator-5580.",
                     refused_target=target)
    if not TARGET_ID.match(target):
        raise refuse(errors.INVALID_VALUE, f"--{own} is not a device id",
                     f"Pass the id exactly as 'adb devices' or 'xcrun simctl list' shows it.")
    return target


def path_for_glob(pattern: str) -> str | None:
    """A concrete repo-relative file path that `fnmatch` matches against `pattern`."""
    pattern = pattern.strip()
    if not pattern or pattern.startswith("/"):
        return None
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            while index < len(pattern) and pattern[index] == "*":
                index += 1
            out.append("pretest_live")
            continue
        if char == "?":
            out.append("x")
        elif char == "[":
            end = pattern.find("]", index + 2)
            if end < 0:
                out.append("[")
            else:
                members = pattern[index + 1:end]
                if members[:1] in ("!", "^"):
                    banned = members[1:]
                    out.append(next((c for c in "zqxkj" if c not in banned), "_"))
                else:
                    out.append(members[0])
                index = end
        else:
            out.append(char)
        index += 1
    path = "".join(out)
    if path.endswith("/"):
        path += "pretest_live.txt"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts) or path.startswith(".git/"):
        return None
    return path if fnmatch.fnmatch(path, pattern) else None


def covers_globs(flow: Any) -> list[str]:
    raw = (getattr(flow, "properties", None) or {}).get("covers") or ""
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def mock_glob(pattern: str) -> str:
    """The `network mock add --match` glob for --mock-path.

    The glob is matched against the full request URL, query string included, so a plain
    path ('/api/v1/catalog') becomes '*/api/v1/catalog*': it matches the path with or
    without a query string ('...catalog?filters='), and also any longer path with that
    prefix ('...catalog/items'). An explicit URL ('://') or glob ('*', '?', '[') is kept
    as given.
    """
    if "://" in pattern or any(char in pattern for char in "*?["):
        return pattern
    return ("*" if pattern.startswith("/") else "*/") + pattern + "*"


def flow_verdict(payload: dict[str, Any]) -> str:
    """pass|fail|blocked for one `flow run` payload (the mapping of `pretest flows`)."""
    status = payload.get("status")
    if status in ("passed", "replayed"):
        steps = payload.get("steps") or []
        if steps and all(isinstance(s, dict) and s.get("status") == "skipped" for s in steps):
            return "inconclusive"
        return "pass"
    failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
    if status == "failed" and failure.get("failure_class") in (None, "test_failure"):
        return "fail"
    return "blocked"


def parse_payload(stdout: str, stderr: str) -> dict[str, Any]:
    """The CLI's JSON answer: stdout first (a failed flow run exits 1 there), else stderr."""
    for stream in (stdout, stderr):
        text = (stream or "").strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            # `--events` style or noise before the JSON: try the last line
            try:
                value = json.loads(text.splitlines()[-1])
            except (ValueError, IndexError):
                continue
        if isinstance(value, dict):
            return value
    tail = ((stderr or "") + (stdout or "")).strip()[-300:]
    return {"ok": False, "error_code": None, "error": tail or "no output"}


def evaluate(report: dict[str, Any]) -> list[str]:
    """Every reason the run is not ok (an empty list means ok)."""
    failures: list[str] = []
    if report.get("error"):
        failures.append("error")
    if not (report.get("session") or {}).get("started"):
        failures.append("session_not_started")
    checks = report.get("checks") or []
    if len(checks) < 2:
        failures.append("fewer_than_two_checks")
    flow_check = report.get("check_flow") or {}
    if not flow_check.get("run_id"):
        failures.append("flow_not_run")
    network_check = report.get("check_network") or {}
    if report.get("options", {}).get("mock_path") and not network_check.get("mocked_500_seen"):
        failures.append("mock_not_hit")
    if report.get("options", {}).get("mock_path"):
        delayed = report.get("delayed_duration_s")
        if not isinstance(delayed, (int, float)) or delayed < DELAY_SECONDS - DELAY_TOLERANCE_S:
            failures.append("delay_not_observed")
        if not report.get("incomplete_requests"):
            failures.append("no_incomplete_request")
    if not (report.get("finding") or {}).get("finding_id"):
        failures.append("no_finding")
    pack = report.get("pack") or {}
    if not (pack.get("verify_before") or {}).get("ok"):
        failures.append("verify_before_review")
    if not (pack.get("verify_after") or {}).get("ok"):
        failures.append("verify_after_review")
    review = report.get("review") or {}
    if review.get("status_decision") != "return-to-dev":
        failures.append("review_not_recorded")
    if report.get("ticket_redacted") is not True:
        failures.append("ticket_not_redacted")
    if report.get("mocks_left") != 0:
        failures.append("mock_left_in_registry")
    restore = report.get("restore") or {}
    if not restore.get("steps") or not all(step.get("ok") for step in restore["steps"]):
        failures.append("restore")
    if report.get("options", {}).get("ui") and not (report.get("view") or {}).get("ok"):
        failures.append("view_check")
    return failures


def nav_flow_path(selected: list[str], plan: dict[str, Any], repo: str | None) -> str | None:
    """The flow replayed under the mock in the network check, or None for a bare relaunch.

    The first flow `pretest start` selected (absolute paths, the regression flows that
    cover the diff), else the flow `choose_flow` picked (its copy inside the temporary
    repository when the diff changes the flow file). Only an existing file counts."""
    candidates = [str(item) for item in selected if item]
    chosen = plan.get("flow") or {}
    if chosen.get("diff_mode") == "flow_file_changed":
        if repo and chosen.get("changed_file"):
            candidates.append(str(Path(repo) / chosen["changed_file"]))
    elif chosen.get("flow"):
        candidates.append(str(chosen["flow"]))
    return next((item for item in candidates if Path(item).is_file()), None)


def network_verdict(mock_glob: str | None, seen: int) -> tuple[str, str]:
    """The network check's verdict and note: blocked when a mock was set and never hit."""
    if mock_glob and not seen:
        return "blocked", "no request matched the mock during the check"
    return "inconclusive", "the error was forced and the screen captured; a person judges the state"


def scrub(report: dict[str, Any], marker: str) -> tuple[str, bool]:
    """The report as JSON text with sensitive keys masked and `marker` never present."""
    text = json.dumps(redact_value(report), indent=2, ensure_ascii=False) + "\n"
    leaked = bool(marker) and marker in text
    if leaked:
        text = text.replace(marker, "<redacted>")
    return text, leaked


# --- validation and the plan ----------------------------------------------------------------


def choose_flow(flows_dir: Path, platform: str) -> dict[str, Any]:
    """The flow for check 1 and how the diff will be made to select it."""
    from autonom_lib.flow import validator as flow_validator

    runnable: list[tuple[Path, Any]] = []
    invalid: list[dict[str, str]] = []
    for file in flow_validator.discover(flows_dir):
        try:
            flow = flow_validator.load_flow(file)
        except errors.AutonomError as exc:
            invalid.append({"flow": file.name, "error_code": exc.code})
            continue
        try:
            platforms = flow_validator.flow_platforms(flow)
        except errors.AutonomError:
            platforms = []
        if platform in platforms:
            runnable.append((file, flow))
    for file, flow in runnable:
        for pattern in covers_globs(flow):
            path = path_for_glob(pattern)
            if path:
                return {"flow": str(file), "flow_name": flow.name or file.name,
                        "flow_app_id": flow.app_id, "diff_mode": "covers",
                        "covers": pattern, "changed_file": path, "invalid": invalid}
    if runnable:
        file, flow = runnable[0]
        relative = file.resolve().relative_to(flows_dir.resolve()).as_posix()
        return {"flow": str(file), "flow_name": flow.name or file.name,
                "flow_app_id": flow.app_id, "diff_mode": "flow_file_changed",
                "covers": None, "changed_file": f".autonom/flows/{relative}",
                "flow_relative": relative, "invalid": invalid}
    raise refuse(errors.FLOW_NO_FLOWS_FOUND,
                 f"no flow under --flows runs on {platform}",
                 "Point --flows at a Flow v1 directory with a flow for this platform "
                 "('autonom flow list <dir>' shows them).", invalid=invalid)


def validate(args: argparse.Namespace) -> dict[str, Any]:
    """Refuse a bad command line before any device, proxy or directory is touched."""
    target = target_of(args)
    if not args.app_id or not APP_ID.match(args.app_id):
        raise refuse(errors.INVALID_VALUE, "--app-id must be a package or bundle id",
                     "For example --app-id com.example.app.")
    install: Path | None = None
    if args.apk is not None and args.platform != "android":
        raise refuse(errors.INVALID_VALUE, "--apk is for --platform android",
                     "Pass the iOS build with --app <path>.app.")
    if args.app is not None and args.platform != "ios":
        raise refuse(errors.INVALID_VALUE, "--app is for --platform ios",
                     "Pass the Android build with --apk <path>.apk.")
    if args.apk is not None:
        install = args.apk.expanduser().resolve()
        if not install.is_file():
            raise refuse(errors.INSTALL_PATH_NOT_FOUND, f"no APK at {install}",
                         "Build the debug APK first, or drop --apk to use the installed app.")
    if args.app is not None:
        install = args.app.expanduser().resolve()
        if not install.is_dir() or install.suffix != ".app":
            raise refuse(errors.INSTALL_PATH_NOT_FOUND, f"no .app bundle at {install}",
                         "Pass the Simulator build's .app directory, or drop --app.")
    if args.flows is None or not args.flows.expanduser().is_dir():
        raise refuse(errors.FLOW_FILE_NOT_FOUND, "--flows must be a directory of flows",
                     "Pass the app's Flow v1 directory, e.g. --flows .autonom/flows.")
    if args.evidence_dir is None:
        raise refuse(errors.INVALID_VALUE, "--evidence-dir is required",
                     "Name a directory for pretest-<platform>.json and the pack.")
    evidence = args.evidence_dir.expanduser().resolve()
    if evidence.exists() and not evidence.is_dir():
        raise refuse(errors.OUTPUT_NOT_WRITABLE, f"{evidence} is not a directory",
                     "Pass a new or existing directory to --evidence-dir.")
    if args.proxy_port is not None and not 1 <= args.proxy_port <= 65535:
        raise refuse(errors.INVALID_VALUE, "--proxy-port must be 1..65535",
                     "Pass the port the proxy listens on, e.g. --proxy-port 8899.")
    if args.in_app_proxy and args.proxy_port is None:
        raise refuse(errors.INVALID_VALUE, "--in-app-proxy needs --proxy-port",
                     "Name the port the app's own proxy setting points at.")
    if args.mock_path is not None and not MOCK_PATH.match(args.mock_path):
        raise refuse(errors.INVALID_VALUE, "--mock-path must be a path or URL glob without spaces",
                     "For example --mock-path /api/catalog.")
    for pair in args.env:
        if not ENV_PAIR.match(pair):
            raise refuse(errors.INVALID_VALUE, f"--env {pair.split('=')[0]!r} is not KEY=VALUE",
                         "Pass flow variables as --env NAME=value.")
    flows = args.flows.expanduser().resolve()
    chosen = choose_flow(flows, args.platform)
    capture = args.proxy_port is not None or args.mock_path is not None
    plan = {
        "platform": args.platform, "target_id": target, "app_id": args.app_id,
        "install": str(install) if install else None, "flows": str(flows),
        "flow": chosen, "evidence_dir": str(evidence),
        "report": str(evidence / f"pretest-{args.platform}.json"),
        "capture": capture, "proxy_port": args.proxy_port,
        "attach_device_proxy": capture and not args.in_app_proxy,
        "in_app_proxy": bool(args.in_app_proxy), "mock_path": args.mock_path,
        "mock_glob": mock_glob(args.mock_path) if args.mock_path else None,
        # set by Autonom at `network start`; the run reads it back from the proxy argv
        "proxy_lazy_connect": "set by Autonom" if capture else None,
        "delay_s": DELAY_SECONDS if args.mock_path is not None else None,
        "hang_wait_s": HANG_WAIT_SECONDS if args.mock_path is not None else None,
        "env_names": [pair.split("=", 1)[0] for pair in args.env], "ui": bool(args.ui),
        "nav_flow": Path(chosen["flow"]).name if args.mock_path is not None else None,
        "steps": [
            "device online check" + (" (boot when Shutdown)" if args.platform == "ios" else ""),
            "temporary AUTONOM_HOME and git repo (main + live-change)",
            "session start" + (" --install" if install else "") + " --launch",
            *(["network start" + (f" --port {args.proxy_port}" if args.proxy_port else "")
               + " (Autonom sets connection_strategy=lazy; read back from the proxy argv)"]
              if capture else []),
            *(["network attach"] if capture and not args.in_app_proxy else []),
            "pretest start (generated ticket, --base main --head live-change)",
            "check 1: pretest flows (or check begin + flow run)",
            ("check 2: network with HTTP {status} mock, then flow run of the first selected "
             "regression flow under the mock (planned: {flow}; its assertion failure is "
             "tolerated; a fresh relaunch only when no flow file is available), settle, "
             "screenshot, mocked {status} count, finding".format(
                 status=MOCK_STATUS, flow=Path(chosen["flow"]).name)
             if args.mock_path else
             "check 2: network, relaunch, settle, screenshot, finding"),
            *(["check 3: slow (kit case slow): network mock update --delay {delay}, flow run "
               "of the same flow, delayed_duration_s from the captured request; network mock "
               "update --hang, flow run, wait up to {wait} s for the hang cap, "
               "incomplete_requests (completed: false); network mock remove".format(
                   delay=DELAY_SECONDS, wait=HANG_WAIT_SECONDS)]
              if args.mock_path else []),
            "pretest pack, report verify, review return-to-dev, report verify, review-status",
            *(["view check (4 screenshots)"] if args.ui else []),
            "restore: pretest stop if active, mock remove, network detach/stop, session stop",
        ],
    }
    return plan


# --- running the CLI and the device checks ----------------------------------------------------


def run(argv: list[str], *, timeout: float, env: dict[str, str] | None = None,
        cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               env=env, cwd=cwd, stdin=subprocess.DEVNULL, check=False)
    if check and completed.returncode != 0:
        raise StepFailed(f"{' '.join(argv[:3])} failed ({completed.returncode}): "
                         f"{(completed.stderr or completed.stdout).strip()[-300:]}")
    return completed


class Autonom:
    """`scripts/autonom.py` bound to the one target and the temporary home."""

    def __init__(self, platform: str, target: str, home: Path) -> None:
        self.flags = ["--platform", platform,
                      "--serial" if platform == "android" else "--udid", target]
        self.env = {**os.environ, "AUTONOM_HOME": str(home)}
        for name in ISOLATED_ENV:
            self.env.pop(name, None)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *argv: str, timeout: float = CLI_TIMEOUT,
                 expect_ok: bool = True) -> dict[str, Any]:
        started = time.monotonic()
        try:
            completed = run([sys.executable, str(CLI), *self.flags, *argv],
                            timeout=timeout, env=self.env, check=False)
            payload = parse_payload(completed.stdout, completed.stderr)
            code = completed.returncode
        except subprocess.TimeoutExpired:
            payload = {"ok": False, "error_code": "timeout",
                       "error": f"no answer within {timeout} s"}
            code = None
        self.calls.append({"argv": list(argv[:3]), "exit": code,
                           "seconds": round(time.monotonic() - started, 1),
                           "ok": bool(payload.get("ok")),
                           "error_code": payload.get("error_code")})
        if expect_ok and not payload.get("ok"):
            raise StepFailed(f"autonom {' '.join(argv[:3])}: {payload.get('error_code')} "
                             f"{str(payload.get('error'))[:300]}")
        return payload


def adb_binary() -> str:
    return os.environ.get("AUTONOM_ADB") or shutil.which("adb") or "adb"


def android_state(serial: str) -> str:
    try:
        completed = run([adb_binary(), "-s", serial, "get-state"], timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed(f"adb is not usable: {exc}") from None
    return (completed.stdout or "").strip() or "offline"


def simulator_state(udid: str) -> str | None:
    listing = json.loads(run(["xcrun", "simctl", "list", "devices", "--json"],
                             timeout=60).stdout or "{}")
    for devices in (listing.get("devices") or {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return device.get("state")
    return None


def owner_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    with contextlib.suppress(OSError):
        path.chmod(DIR_MODE)
    return path


def write_owner_file(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    with contextlib.suppress(OSError):
        path.chmod(FILE_MODE)


def make_repo(root: Path, plan: dict[str, Any], run_id: str) -> Path:
    """`main` with a README (and, for a flow without covers, a copy of --flows), then one
    commit on `live-change` that changes the file the chosen flow covers."""
    repo = owner_dir(root / "repo")
    git = [*GIT, "-C", str(repo)]
    run([*git, "init", "-q", "-b", "main"], timeout=60)
    (repo / "README.md").write_text("# pre-test live repository\n", encoding="utf-8")
    flow = plan["flow"]
    if flow["diff_mode"] == "flow_file_changed":
        shutil.copytree(plan["flows"], repo / ".autonom" / "flows", symlinks=False)
    run([*git, "add", "-A"], timeout=60)
    run([*git, "commit", "-q", "-m", "base"], timeout=60)
    run([*git, "checkout", "-q", "-b", "live-change"], timeout=60)
    changed = repo / flow["changed_file"]
    if flow["diff_mode"] == "flow_file_changed":
        with changed.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# pre-test live change {run_id}\n")
    else:
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text(f"// pre-test live change {run_id}\n", encoding="utf-8")
    run([*git, "add", "-A"], timeout=60)
    run([*git, "commit", "-q", "-m", "live change"], timeout=60)
    return repo


def ticket_text(plan: dict[str, Any], run_id: str, marker: str) -> str:
    lines = [f"# LIVE-{run_id} Pre-test live check", "",
             f"The change touches `{plan['flow']['changed_file']}`; the covering flow must "
             "still pass."]
    if plan["mock_glob"]:
        lines.append(f"When the server answers HTTP {MOCK_STATUS} on `{plan['mock_glob']}`, "
                     "the app shows an error state, not a blank screen.")
    lines += ["", f"Debug: Authorization: Bearer {marker}", ""]
    return "\n".join(lines)


# --- the live run ---------------------------------------------------------------------------


def step_check_flow(autonom: Autonom, report: dict[str, Any], plan: dict[str, Any],
                    env_args: list[str]) -> None:
    selected = report["pretest"].get("selected_flows") or []
    result: dict[str, Any] = {"selected": len(selected)}
    report["check_flow"] = result
    if selected:
        answer = autonom("pretest", "flows", *env_args, timeout=FLOW_TIMEOUT)
        result["mode"] = "pretest flows"
        runs = [item for item in answer.get("runs") or [] if isinstance(item, dict)]
        result["runs"] = [{key: item.get(key) for key in (
            "check_id", "run_id", "status", "verdict")} | {
            "error_code": (item.get("failure") or {}).get("error_code")} for item in runs]
        mine = next((item for item in runs
                     if Path(str(item.get("flow") or "")).name == Path(plan["flow"]["flow"]).name),
                    runs[0] if runs else {})
        result.update({key: mine.get(key) for key in ("check_id", "run_id", "status", "verdict")})
        return
    result["mode"] = "check begin + flow run"
    flow_path = (str(Path(report["repo"]["path"]) / plan["flow"]["changed_file"])
                 if plan["flow"]["diff_mode"] == "flow_file_changed" else plan["flow"]["flow"])
    added = autonom("pretest", "check", "add", f"Regression flow: {plan['flow']['flow_name']}",
                    "--kind", "regression", "--flow", flow_path)
    check_id = added["check_id"]
    result["check_id"] = check_id
    autonom("pretest", "check", "begin", check_id)
    try:
        ran = autonom("flow", "run", flow_path, *env_args, timeout=FLOW_TIMEOUT,
                      expect_ok=False)
        result.update({"run_id": ran.get("run_id"), "status": ran.get("status"),
                       "error_code": ran.get("error_code")})
        verdict = flow_verdict(ran) if ran.get("ok") else "blocked"
    except BaseException:
        autonom("pretest", "check", "end", check_id, "--verdict", "blocked",
                "--note", "the flow run stopped", expect_ok=False)
        raise
    ended = autonom("pretest", "check", "end", check_id, "--verdict", verdict,
                    "--note", f"flow run {result.get('status')}")
    result["verdict"] = ended.get("verdict")


def step_check_network(autonom: Autonom, report: dict[str, Any], plan: dict[str, Any],
                       state: dict[str, Any], run_id: str, env_args: list[str]) -> None:
    result: dict[str, Any] = {"mock_glob": plan["mock_glob"]}
    report["check_network"] = result
    title = (f"Forced HTTP {MOCK_STATUS} on {plan['mock_glob']} shows an error state"
             if plan["mock_glob"] else "Network capture across a fresh launch")
    extra = ["--case", "server-error"] if plan["mock_glob"] else []
    added = autonom("pretest", "check", "add", title, "--kind", "network", *extra,
                    "--why", "the ticket's server-error acceptance point")
    check_id = added["check_id"]
    result["check_id"] = check_id
    autonom("pretest", "check", "begin", check_id)
    verdict, note = "blocked", "the network check stopped early"
    try:
        if plan["mock_glob"]:
            mock = autonom("network", "mock", "add", "--match", plan["mock_glob"],
                           "--status", str(MOCK_STATUS), "--json", '{"error":"pretest-live"}',
                           "--note", f"pretest-live-{run_id}")
            state["mock_id"] = (mock.get("mock") or {}).get("id")
            result["mock_id"] = state["mock_id"]
        # The mocked endpoint is often requested only when its screen opens (WoolBox
        # asks for /api/v1/catalog on the Catalog tab, not at launch): drive the app
        # there with the covering flow, inside this open check.
        nav = (nav_flow_path(state.get("selected_flow_paths") or [], plan,
                             (report.get("repo") or {}).get("path"))
               if plan["mock_glob"] else None)
        if nav:
            result["nav_flow"] = Path(nav).name
            # the forced 500 is expected to break the flow's assertions: never fatal
            ran = autonom("flow", "run", nav, *env_args, timeout=FLOW_TIMEOUT,
                          expect_ok=False)
            result["nav_flow_run_id"] = ran.get("run_id")
            result["nav_flow_status"] = ran.get("status")
            result["nav_flow_error_code"] = (ran.get("error_code")
                                             or (ran.get("failure") or {}).get("error_code"))
        else:
            result["nav_flow"] = None
            launched = autonom("session", "launch", plan["app_id"], "--fresh", expect_ok=False)
            result["relaunch_ok"] = bool(launched.get("ok"))
            result["relaunch_error_code"] = launched.get("error_code")
        time.sleep(SETTLE_SECONDS)
        shot = autonom("screenshot", "--label", f"check {check_id}: after the forced error")
        shot_name = Path(str(shot.get("path") or "")).name
        result["screenshot"] = {"file": shot_name, "width": shot.get("width"),
                                "height": shot.get("height"),
                                "task": (shot.get("metadata") or {}).get("task"),
                                "mocks_active": (shot.get("metadata") or {}).get("mocks_active")}
        evidence = ["--evidence", f"shot:{shot_name}"]
        if plan["capture"]:
            listed = autonom("network", "requests", "list", "--max", "500")
            result["requests_captured"] = listed.get("total_matched")
            mocked = autonom("network", "requests", "list", "--status", str(MOCK_STATUS),
                             "--mocked", "true")
            items = [item for item in mocked.get("requests") or [] if isinstance(item, dict)]
            result["mocked_500_seen"] = mocked.get("total_matched", len(items))
            if items and items[0].get("id"):
                result["request_id"] = items[0]["id"]
                evidence += ["--evidence", f"request:{items[0]['id']}"]
        seen = result.get("mocked_500_seen") or 0
        actual = (f"The app received {seen} forced HTTP {MOCK_STATUS} response(s) on "
                  f"{plan['mock_glob']}; the screen after them is in the screenshot."
                  if plan["mock_glob"] else
                  "The app was relaunched with capture on; the screen is in the screenshot.")
        finding = autonom(
            "pretest", "finding", "add", "--check", check_id, "--severity", "minor",
            "--title", "Screen state after a forced server error" if plan["mock_glob"]
            else "Screen state after a fresh launch",
            "--expected", "An error state with a retry, not a blank or frozen screen",
            "--actual", actual, *evidence)
        report["finding"] = {key: finding.get(key) for key in (
            "finding_id", "check_id", "severity", "origin", "confirmed", "evidence")}
        verdict, note = network_verdict(plan["mock_glob"], seen)
    finally:
        ended = autonom("pretest", "check", "end", check_id, "--verdict", verdict,
                        "--note", note, expect_ok=False)
        result["verdict"] = ended.get("verdict") if ended.get("ok") else None


def step_check_slow(autonom: Autonom, report: dict[str, Any], plan: dict[str, Any],
                    state: dict[str, Any], env_args: list[str], *,
                    clock=time.time, sleep=time.sleep) -> None:
    """Check 3: the --mock-path rule delayed by DELAY_SECONDS, then held (`--hang`)
    until the proxy's cap records the request incomplete; the rule is removed at the
    end. Sets `delayed_duration_s` and `incomplete_requests` on the report."""
    result: dict[str, Any] = {"mock_glob": plan["mock_glob"], "delay_s": DELAY_SECONDS}
    report["check_slow"] = result
    mock_id = state.get("mock_id")
    if not plan["mock_glob"] or not mock_id:
        result["skipped"] = "no mock on --mock-path"
        return
    added = autonom("pretest", "check", "add",
                    f"A {DELAY_SECONDS} s delay and a held response on {plan['mock_glob']} "
                    "do not freeze the screen", "--kind", "network", "--case", "slow",
                    "--why", "a slow or stuck server must leave the app usable")
    check_id = added["check_id"]
    result["check_id"] = check_id
    autonom("pretest", "check", "begin", check_id)
    verdict, note = "blocked", "the slow-response check stopped early"
    nav = nav_flow_path(state.get("selected_flow_paths") or [], plan,
                        (report.get("repo") or {}).get("path"))
    result["nav_flow"] = Path(nav).name if nav else None

    def replay(prefix: str) -> None:
        if nav:
            ran = autonom("flow", "run", nav, *env_args, timeout=FLOW_TIMEOUT, expect_ok=False)
            result[f"{prefix}_flow_run_id"] = ran.get("run_id")
            result[f"{prefix}_flow_status"] = ran.get("status")
        else:
            launched = autonom("session", "launch", plan["app_id"], "--fresh", expect_ok=False)
            result[f"{prefix}_relaunch_ok"] = bool(launched.get("ok"))

    def records(since_ms: int) -> list[dict[str, Any]]:
        listed = autonom("network", "requests", "list", "--mocked", "true", "--max", "500")
        return rule_records(listed, mock_id, since_ms)

    try:
        delay_from = int(clock() * 1000)
        autonom("network", "mock", "update", mock_id, "--delay", str(DELAY_SECONDS))
        replay("delay")
        delayed = records(delay_from)
        report["delayed_duration_s"] = delayed_duration_s(delayed)
        result["delayed_requests"] = sum(1 for item in delayed if item.get("completed") is not False)
        hang_from = int(clock() * 1000)
        autonom("network", "mock", "update", mock_id, "--hang")
        replay("hang")
        # Wait from the end of the replay: a request held late in the replay is
        # capped 120 s after it started, which is still inside this window.
        deadline = clock() + HANG_WAIT_SECONDS
        polls = 0
        while True:
            held = records(hang_from)
            polls += 1
            if incomplete_count(held) or clock() >= deadline:
                break
            sleep(POLL_SECONDS)
        report["incomplete_requests"] = incomplete_count(held)
        result["hang_polls"] = polls
        result["incomplete_error_kinds"] = sorted({str(item.get("error_kind"))
                                                   for item in held
                                                   if item.get("completed") is False})
        if report["delayed_duration_s"] is not None and report["incomplete_requests"]:
            verdict, note = "inconclusive", (
                f"a {DELAY_SECONDS} s delay ({report['delayed_duration_s']} s measured) and "
                f"{report['incomplete_requests']} held request(s) were served; a person "
                "judges the screens")
        else:
            verdict, note = "blocked", "no delayed or no held request was captured"
    finally:
        # every mock of this run goes now; restore still checks the registry
        removed = autonom("network", "mock", "remove", mock_id, expect_ok=False)
        if removed.get("ok"):
            state["mock_id"] = None
            result["mock_removed"] = True
        ended = autonom("pretest", "check", "end", check_id, "--verdict", verdict,
                        "--note", note, expect_ok=False)
        result["verdict"] = ended.get("verdict") if ended.get("ok") else None


def step_pack_review(autonom: Autonom, report: dict[str, Any], plan: dict[str, Any],
                     evidence: Path, run_id: str, marker: str) -> Path:
    listed = autonom("pretest", "check", "list")
    report["checks"] = [{key: item.get(key) for key in (
        "check_id", "kind", "case", "state", "verdict", "duration_s")}
        | {"run_ids": item.get("run_ids") or []}
        for item in listed.get("checks") or [] if isinstance(item, dict)]
    out = evidence / f"pack-{plan['platform']}-{run_id}"
    packed = autonom("pretest", "pack", "--out", str(out), timeout=FLOW_TIMEOUT)
    pack = Path(str(packed.get("pack") or out))
    report["pack"] = {"path": str(pack), "integrity_sha256": packed.get("integrity_sha256"),
                      "verdict": packed.get("verdict"), "idempotent": packed.get("idempotent")}
    report["pretest"]["packed"] = True
    before = autonom("report", "verify", str(pack), expect_ok=False)
    report["pack"]["verify_before"] = {"ok": bool(before.get("ok")), "files": before.get("files"),
                                       "nested": before.get("nested"),
                                       "error_code": before.get("error_code")}
    summary_file = pack / "summary.json"
    with contextlib.suppress(OSError, ValueError):
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        timing = summary.get("timing") or {}
        report["summary"] = {
            "verdict": summary.get("verdict"), "checks": len(summary.get("checks") or []),
            "findings": len(summary.get("findings") or []),
            "auto_findings": timing.get("auto_findings"), "actions": timing.get("actions"),
            "wall_s": timing.get("wall_s"), "not_covered": summary.get("not_covered"),
            "blocked": summary.get("blocked"),
            "evidence_items": len(summary.get("evidence") or [])}
    with contextlib.suppress(OSError):
        ticket = (pack / "ticket.md").read_text(encoding="utf-8", errors="replace")
        report["ticket_redacted"] = marker not in ticket and "<redacted>" in ticket
    finding_id = (report.get("finding") or {}).get("finding_id")
    review_args = ["--finding", finding_id] if finding_id else []
    reviewed = autonom("pretest", "review", str(pack), "return-to-dev", *review_args,
                       "--note", "pre-test live run: check the server-error state",
                       "--author", "pretest-live", expect_ok=False)
    report["review"] = {"ok": bool(reviewed.get("ok")), "decision": reviewed.get("decision"),
                        "path": reviewed.get("path"), "error_code": reviewed.get("error_code")}
    after = autonom("report", "verify", str(pack), expect_ok=False)
    report["pack"]["verify_after"] = {"ok": bool(after.get("ok")), "files": after.get("files"),
                                      "error_code": after.get("error_code")}
    status = autonom("pretest", "review-status", str(pack), expect_ok=False)
    report["review"]["status_decision"] = status.get("decision")
    report["review"]["decisions"] = len(status.get("decisions") or [])
    return pack


def step_view(report: dict[str, Any], pack: Path, evidence: Path, platform: str) -> None:
    node = shutil.which("node")
    if not node:
        report["view"] = {"ok": False, "error": "node is not on PATH"}
        return
    out = evidence / f"view-{platform}"
    try:
        completed = run([node, str(VIEW_CHECK), str(pack), "--out", str(out)],
                        timeout=VIEW_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        report["view"] = {"ok": False, "error": f"no answer within {VIEW_TIMEOUT} s"}
        return
    answer = parse_payload(completed.stdout, completed.stderr)
    report["view"] = {"ok": completed.returncode == 0 and bool(answer.get("ok")),
                      "exit": completed.returncode, "report": answer.get("report"),
                      "screenshots": answer.get("screenshots"), "runs": answer.get("runs"),
                      "error": answer.get("error")}


def restore(autonom: Autonom | None, report: dict[str, Any], state: dict[str, Any],
            run_id: str) -> None:
    steps: list[dict[str, Any]] = report["restore"]["steps"]

    def step(name: str, body) -> None:
        try:
            steps.append({"step": name, "ok": bool(body())})
        except (StepFailed, OSError, subprocess.TimeoutExpired, ValueError, KeyError,
                TypeError) as exc:
            steps.append({"step": name, "ok": False, "error": str(exc)[-300:]})

    if not state.get("session"):
        report["mocks_left"] = 0  # no session: no mock was added to the temporary registry
    if autonom is not None and state.get("session"):
        if state.get("pretest") and not report["pretest"].get("packed"):
            step("pre-test stopped",
                 lambda: autonom("pretest", "stop", expect_ok=False).get("ok")
                 or autonom("pretest", "status", expect_ok=False).get("error_code")
                 == "no_active_pretest")
        if state.get("mock_id"):
            step("mock removed",
                 lambda: autonom("network", "mock", "remove", state["mock_id"]).get("ok"))

        def registry_empty() -> bool:
            listed = autonom("network", "mock", "list", "--all")
            mine = [rule for rule in listed.get("mocks") or []
                    if isinstance(rule, dict) and (
                        rule.get("id") == state.get("mock_id")
                        or f"pretest-live-{run_id}" in str(rule.get("note") or ""))]
            report["mocks_left"] = len(mine)
            report["registry_mocks_total"] = listed.get("count")
            return not mine
        step("no mock left in the registry", registry_empty)
        if state.get("attached"):
            step("device proxy detached",
                 lambda: autonom("network", "detach").get("ok"))
        if state.get("capture"):
            step("capture stopped", lambda: autonom("network", "stop").get("ok"))
    if autonom is not None and state.get("session_attempted"):
        def session_stopped() -> bool:
            answer = autonom("session", "stop", expect_ok=False)
            # a start that failed before the session existed leaves nothing to stop
            return bool(answer.get("ok")) or (
                not state.get("session") and answer.get("error_code") == "no_active_session")
        step("session stopped", session_stopped)
    if state.get("booted_here"):
        step("Simulator shut down again",
             lambda: run(["xcrun", "simctl", "shutdown", report["target_id"]],
                         timeout=120, check=False).returncode == 0)
    report["restore"]["ok"] = all(item["ok"] for item in steps)


def live(args: argparse.Namespace, plan: dict[str, Any]) -> int:
    run_id = secrets.token_hex(4)
    marker = "pretestlive" + secrets.token_hex(16)
    evidence = owner_dir(Path(plan["evidence_dir"]))
    report: dict[str, Any] = {
        "schema": SCHEMA, "platform": plan["platform"], "target_id": plan["target_id"],
        "app_id": plan["app_id"], "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "options": {"install": plan["install"] is not None, "mock_path": plan["mock_path"],
                    "mock_glob": plan["mock_glob"], "proxy_port": plan["proxy_port"],
                    "in_app_proxy": plan["in_app_proxy"], "ui": plan["ui"],
                    "env_names": plan["env_names"]},
        "device": {}, "repo": {}, "session": {"started": False}, "network": {},
        "pretest": {}, "checks": [], "check_flow": None, "check_network": None,
        "finding": None, "pack": None, "review": None, "summary": None,
        "ticket_redacted": None, "view": None, "mocks_left": None,
        "check_slow": None, "delayed_duration_s": None, "incomplete_requests": None,
        "restore": {"ok": False, "steps": []}, "ok": False,
    }
    state: dict[str, Any] = {}
    home = Path(tempfile.mkdtemp(prefix="autonom-pretest-live-home-"))
    scratch = Path(tempfile.mkdtemp(prefix="autonom-pretest-live-repo-"))
    autonom: Autonom | None = None
    env_args = [item for pair in args.env for item in ("--env", pair)]
    try:
        if plan["platform"] == "android":
            device_state = android_state(plan["target_id"])
            report["device"] = {"initial_state": device_state}
            if device_state != "device":
                raise StepFailed(f"{plan['target_id']} is {device_state}, not online")
        else:
            device_state = simulator_state(plan["target_id"])
            report["device"] = {"initial_state": device_state, "booted_here": False}
            if device_state is None:
                raise StepFailed(f"no Simulator {plan['target_id']}")
            if device_state == "Shutdown":
                state["booted_here"] = True
                report["device"]["booted_here"] = True
                run(["xcrun", "simctl", "bootstatus", plan["target_id"], "-b"], timeout=600)
            elif device_state != "Booted":
                raise StepFailed(f"the Simulator is {device_state}; wait for Booted or Shutdown")
        repo = make_repo(scratch, plan, run_id)
        report["repo"] = {"path": str(repo), "diff_mode": plan["flow"]["diff_mode"],
                          "changed_file": plan["flow"]["changed_file"],
                          "covers": plan["flow"]["covers"], "flow": plan["flow"]["flow_name"]}
        ticket = scratch / "ticket.md"
        write_owner_file(ticket, ticket_text(plan, run_id, marker))
        autonom = Autonom(plan["platform"], plan["target_id"], home)
        start = ["session", "start", "--app-id", plan["app_id"], "--launch"]
        if plan["install"]:
            start += ["--install", plan["install"]]
        state["session_attempted"] = True
        started = autonom(*start, timeout=INSTALL_TIMEOUT)
        state["session"] = True
        report["session"] = {"started": True, "session_id": started.get("session_id")}
        if plan["capture"]:
            network = ["network", "start", "--i-understand-mitm"]
            if plan["proxy_port"]:
                network += ["--port", str(plan["proxy_port"])]
            state["capture"] = True  # set first: a half-started proxy is stopped too
            proxy = autonom(*network)
            report["network"] = {"started": True, "port": proxy.get("port"),
                                 "in_app_proxy": plan["in_app_proxy"], "attached": False}
            # set by Autonom, never by this script: read back from the proxy's argv
            pid = proxy_pid(proxy) or proxy_pid(autonom("network", "status", expect_ok=False))
            lazy = proxy_lazy_connect(pid)
            if lazy is not None:
                report["proxy_lazy_connect"] = lazy
                report["proxy_lazy_connect_source"] = "set by Autonom (proxy argv)"
            if plan["attach_device_proxy"]:
                state["attached"] = True  # set first: a half-applied attach is undone too
                attached = autonom("network", "attach", "--i-understand-mitm")
                report["network"]["attached"] = bool(attached.get("ok"))
        flows_dir = (str(repo / ".autonom" / "flows")
                     if plan["flow"]["diff_mode"] == "flow_file_changed" else plan["flows"])
        begun = autonom("pretest", "start", "--ticket", str(ticket), "--base", "main",
                        "--head", "live-change", "--repo", str(repo), "--flows", flows_dir,
                        "--ticket-ref", f"LIVE-{run_id}", "--app-id", plan["app_id"],
                        timeout=FLOW_TIMEOUT)
        state["pretest"] = True
        report["pretest"] = {key: begun.get(key) for key in (
            "pretest_id", "status", "changed_files_count", "diff_truncated", "baseline")}
        state["selected_flow_paths"] = [str(item) for item in begun.get("selected_flows") or []
                                        if item]
        report["pretest"]["selected_flows"] = [Path(str(item)).name
                                               for item in begun.get("selected_flows") or []]
        step_check_flow(autonom, report, plan, env_args)
        step_check_network(autonom, report, plan, state, run_id, env_args)
        if plan["mock_glob"]:
            step_check_slow(autonom, report, plan, state, env_args)
        pack = step_pack_review(autonom, report, plan, evidence, run_id, marker)
        if plan["ui"]:
            step_view(report, pack, evidence, plan["platform"])
    except (StepFailed, errors.AutonomError, OSError, subprocess.TimeoutExpired, ValueError,
            KeyError, TypeError) as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[-500:]}"
    finally:
        restore(autonom, report, state, run_id)
        report["calls"] = autonom.calls if autonom is not None else []
        if args.keep_home:
            report["autonom_home"] = str(home)
        else:
            shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report["failures"] = evaluate(report)
        report["ok"] = not report["failures"]
        text, leaked = scrub(report, marker)
        if leaked:
            report["ok"] = False
            report["failures"].append("marker_in_report")
            text, _ = scrub(report, marker)
        out = Path(plan["report"])
        write_owner_file(out, text)
        print(json.dumps({"ok": report["ok"], "report": str(out),
                          "failures": report["failures"]}))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.self_test:
            return self_test()
        plan = validate(args)
    except UsageError as exc:
        print(json.dumps({"ok": False, "error_code": errors.USAGE_ERROR, "error": str(exc),
                          "hint": parser.format_usage().strip()}), file=sys.stderr)
        return 2
    except errors.AutonomError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2, ensure_ascii=False))
        return 0
    return live(args, plan)


# --- self-test --------------------------------------------------------------------------------

_FLOW = """schema: autonom.dev/flow/v1
appId: com.example.app
name: Results visible
properties:
  covers: "{covers}"
---
- assertVisible:
    selector:
      text: Results
"""


class SelfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pretest-live-selftest-"))
        self.flows = self.tmp / "flows"
        self.flows.mkdir()
        (self.flows / "results.yaml").write_text(_FLOW.format(covers="lib/**/search_*.dart"),
                                                 encoding="utf-8")
        self.evidence = self.tmp / "evidence"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def argv(self, *extra: str, platform: str = "android") -> list[str]:
        target = ["--serial", "emulator-5580"] if platform == "android" else [
            "--udid", "3760A5A8-E59D-4AE6-B1AF-A626908D7B61"]
        return ["--platform", platform, *target, "--app-id", "com.example.app",
                "--flows", str(self.flows), "--evidence-dir", str(self.evidence), *extra]

    def refused(self, argv: list[str]) -> dict[str, Any]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(argv)
        self.assertEqual(code, 2)
        payload = json.loads(err.getvalue())
        self.assertFalse(payload["ok"])
        self.assertTrue(payload.get("hint"), payload)
        self.assertFalse(self.evidence.exists(), "a refusal must create nothing")
        return payload

    def test_refuses_emulator_5554(self) -> None:
        argv = self.argv()
        argv[argv.index("emulator-5580")] = "emulator-5554"
        payload = self.refused(argv)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual(payload["refused_target"], "emulator-5554")

    def test_refuses_missing_and_conflicting_targets(self) -> None:
        payload = self.refused(["--platform", "android", "--app-id", "com.example.app",
                                "--flows", str(self.flows), "--evidence-dir", str(self.evidence)])
        self.assertEqual(payload["error_code"], errors.NO_TARGET)
        payload = self.refused(self.argv("--udid", "ABC"))
        self.assertEqual(payload["error_code"], errors.CONFLICTING_TARGET_FLAGS)
        payload = self.refused(["--platform", "ios", "--serial", "emulator-5580",
                                "--app-id", "a.b", "--flows", str(self.flows),
                                "--evidence-dir", str(self.evidence)])
        self.assertEqual(payload["error_code"], errors.CONFLICTING_TARGET_FLAGS)
        payload = self.refused(["--app-id", "a.b", "--flows", str(self.flows),
                                "--evidence-dir", str(self.evidence)])
        self.assertEqual(payload["error_code"], errors.UNKNOWN_PLATFORM)
        argv = self.argv()
        argv[argv.index("emulator-5580")] = "emu;rm -rf"
        self.assertEqual(self.refused(argv)["error_code"], errors.INVALID_VALUE)

    def test_refuses_bad_options(self) -> None:
        cases = [
            (self.argv("--apk", str(self.tmp / "missing.apk")), errors.INSTALL_PATH_NOT_FOUND),
            (self.argv("--app", str(self.tmp)), errors.INVALID_VALUE),
            (self.argv("--app", str(self.tmp / "x.app"), platform="ios"),
             errors.INSTALL_PATH_NOT_FOUND),
            (self.argv("--proxy-port", "70000"), errors.INVALID_VALUE),
            (self.argv("--in-app-proxy"), errors.INVALID_VALUE),
            (self.argv("--mock-path", "/a b"), errors.INVALID_VALUE),
            (self.argv("--env", "1BAD=x"), errors.INVALID_VALUE),
        ]
        for argv, code in cases:
            with self.subTest(argv=argv[-2:]):
                self.assertEqual(self.refused(argv)["error_code"], code)
        argv = self.argv()
        argv[argv.index("com.example.app")] = "com example"
        self.assertEqual(self.refused(argv)["error_code"], errors.INVALID_VALUE)
        argv = self.argv()
        argv[argv.index(str(self.flows))] = str(self.tmp / "none")
        self.assertEqual(self.refused(argv)["error_code"], errors.FLOW_FILE_NOT_FOUND)
        self.evidence.write_text("x", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(self.argv()), 2)
        self.assertEqual(json.loads(err.getvalue())["error_code"], errors.OUTPUT_NOT_WRITABLE)
        self.evidence.unlink()

    def test_usage_error_is_json(self) -> None:
        payload = self.refused(self.argv("--proxy-port", "many"))
        self.assertEqual(payload["error_code"], errors.USAGE_ERROR)

    def test_no_runnable_flow_is_refused(self) -> None:
        (self.flows / "results.yaml").write_text("not: a flow\n", encoding="utf-8")
        payload = self.refused(self.argv())
        self.assertEqual(payload["error_code"], errors.FLOW_NO_FLOWS_FOUND)
        self.assertIsInstance(payload["invalid"], list)
        # a flow for the other platform only is not runnable here either
        (self.flows / "results.yaml").write_text(
            _FLOW.format(covers="src/*").replace("---\n", "requires:\n  platform: [ios]\n---\n"),
            encoding="utf-8")
        self.assertEqual(self.refused(self.argv())["error_code"], errors.FLOW_NO_FLOWS_FOUND)

    def test_dry_run_plans_without_touching(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(self.argv("--dry-run", "--mock-path", "/api/catalog", "--ui"))
        self.assertEqual(code, 0)
        plan = json.loads(out.getvalue())
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["flow"]["diff_mode"], "covers")
        self.assertTrue(fnmatch.fnmatch(plan["flow"]["changed_file"], "lib/**/search_*.dart"))
        self.assertEqual(plan["mock_glob"], "*/api/catalog*")
        self.assertTrue(plan["capture"] and plan["attach_device_proxy"])
        self.assertEqual(plan["proxy_lazy_connect"], "set by Autonom")
        self.assertFalse(self.evidence.exists())

    def test_lazy_connect_is_autonoms_not_a_file_written_here(self) -> None:
        # A.1: Autonom starts the proxy lazily; the script writes no mitmproxy config
        self.assertNotIn("write_lazy_connect", globals())
        self.assertNotIn("MITM_CONFIG", globals())
        for extra in (("--mock-path", "/api/catalog"), ("--proxy-port", "8899")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(self.argv("--dry-run", *extra))
            plan = json.loads(out.getvalue())
            self.assertFalse(any("config.yaml" in step for step in plan["steps"]), extra)
            start = next(step for step in plan["steps"] if step.startswith("network start"))
            self.assertIn("Autonom sets connection_strategy=lazy", start)
        from autonom_lib.network import proxy as proxy_mod
        argv = proxy_mod.build_argv("mitmdump", port=8899, directory=self.tmp,
                                    confdir=self.tmp, capture_bodies=False)
        self.assertIn(LAZY_CONNECT_OPTION, argv)

    def test_no_lazy_connect_without_capture(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(self.argv("--dry-run"))
        plan = json.loads(out.getvalue())
        self.assertIsNone(plan["proxy_lazy_connect"])
        self.assertFalse(any("connection_strategy" in step for step in plan["steps"]))

    def test_proxy_lazy_connect_reads_the_proxy_argv(self) -> None:
        def ps(stdout: str, code: int = 0):
            return lambda argv: subprocess.CompletedProcess(argv, code, stdout, "")
        seen: list[list[str]] = []

        def recording(argv):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "mitmdump --set "
                                               "connection_strategy=lazy -q\n", "")
        self.assertTrue(proxy_lazy_connect(4242, recording))
        self.assertEqual(seen, [["ps", "-o", "command=", "-p", "4242"]])
        self.assertFalse(proxy_lazy_connect(4242, ps("mitmdump -q")))
        # unknown: no pid, a failed or empty ps, ps missing -> omitted from the report
        self.assertIsNone(proxy_lazy_connect(None, ps("x")))
        self.assertIsNone(proxy_lazy_connect(True, ps("x")))
        self.assertIsNone(proxy_lazy_connect(4242, ps("", 1)))
        self.assertIsNone(proxy_lazy_connect(4242, ps("  ")))

        def missing(argv):
            raise FileNotFoundError("ps")
        self.assertIsNone(proxy_lazy_connect(4242, missing))
        self.assertEqual(proxy_pid({"pid": 7}), 7)
        self.assertEqual(proxy_pid({"ok": True}, {"proxy": {"pid": 9}}), 9)
        self.assertIsNone(proxy_pid({"pid": "7"}, {"proxy": None}))

    def test_dry_run_plans_the_delay_and_hang_phase(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(self.argv("--dry-run", "--mock-path", "/api/catalog"))
        plan = json.loads(out.getvalue())
        self.assertEqual((plan["delay_s"], plan["hang_wait_s"]), (3, 130))
        steps = plan["steps"]
        index = {name: next(i for i, step in enumerate(steps) if step.startswith(name))
                 for name in ("check 2", "check 3", "pretest pack")}
        self.assertLess(index["check 2"], index["check 3"])
        self.assertLess(index["check 3"], index["pretest pack"])
        third = steps[index["check 3"]]
        for text in ("--delay 3", "delayed_duration_s", "--hang", "130 s",
                     "incomplete_requests", "completed: false", "network mock remove"):
            self.assertIn(text, third)
        self.assertLess(third.index("--delay"), third.index("--hang"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(self.argv("--dry-run", "--proxy-port", "8899"))
        plan = json.loads(out.getvalue())
        self.assertIsNone(plan["delay_s"])
        self.assertFalse(any(step.startswith("check 3") for step in plan["steps"]))

    def test_flow_without_covers_changes_the_flow_file(self) -> None:
        (self.flows / "results.yaml").write_text(
            _FLOW.replace('properties:\n  covers: "{covers}"\n', ""), encoding="utf-8")
        chosen = choose_flow(self.flows, "android")
        self.assertEqual(chosen["diff_mode"], "flow_file_changed")
        self.assertEqual(chosen["changed_file"], ".autonom/flows/results.yaml")

    def test_in_app_proxy_does_not_attach(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(self.argv("--dry-run", "--proxy-port", "8899", "--in-app-proxy"))
        plan = json.loads(out.getvalue())
        self.assertTrue(plan["capture"])
        self.assertFalse(plan["attach_device_proxy"])

    def test_path_for_glob(self) -> None:
        for pattern in ("src/*", "lib/**/x.dart", "a/?b/[cd]e.txt", "a/[!a]x", "*.kt",
                        "lib/features/search/**", "README.md"):
            with self.subTest(pattern=pattern):
                path = path_for_glob(pattern)
                self.assertIsNotNone(path)
                self.assertTrue(fnmatch.fnmatch(path, pattern))
        # proof matches covers with fnmatch as written: these can never match a file
        for pattern in ("", "/abs/*", "../x", ".git/*", "a//b", "./src/*", "docs/"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(path_for_glob(pattern))

    def test_mock_glob(self) -> None:
        self.assertEqual(mock_glob("/api/catalog"), "*/api/catalog*")
        self.assertEqual(mock_glob("api/x"), "*/api/x*")
        glob = mock_glob("/api/v1/catalog")
        for url in ("https://backend.woolbox.app/api/v1/catalog",
                    "https://backend.woolbox.app/api/v1/catalog?filters=",
                    "https://backend.woolbox.app/api/v1/catalog?filters=a&page=2"):
            with self.subTest(url=url):
                self.assertTrue(fnmatch.fnmatchcase(url, glob))
        self.assertFalse(fnmatch.fnmatchcase("https://h/api/v1/cart?x=1", glob))
        self.assertEqual(mock_glob("*/v1/login"), "*/v1/login")
        self.assertEqual(mock_glob("https://h/x"), "https://h/x")

    def test_flow_verdict(self) -> None:
        self.assertEqual(flow_verdict({"status": "passed", "steps": [{"status": "passed"}]}),
                         "pass")
        self.assertEqual(flow_verdict({"status": "passed", "steps": [{"status": "skipped"}]}),
                         "inconclusive")
        self.assertEqual(flow_verdict({"status": "failed", "failure": {
            "failure_class": "test_failure"}}), "fail")
        self.assertEqual(flow_verdict({"status": "failed", "failure": {
            "failure_class": "infrastructure"}}), "blocked")
        self.assertEqual(flow_verdict({"status": "broken"}), "blocked")

    def test_parse_payload(self) -> None:
        self.assertEqual(parse_payload('{"ok": true}', ""), {"ok": True})
        self.assertEqual(parse_payload("", '{"ok": false, "error_code": "x"}')["error_code"], "x")
        self.assertEqual(parse_payload('noise\n{"ok": true}', ""), {"ok": True})
        broken = parse_payload("Traceback", "boom")
        self.assertFalse(broken["ok"])
        self.assertIn("boom", broken["error"])

    def good_report(self) -> dict[str, Any]:
        return {"session": {"started": True}, "checks": [{}, {}],
                "check_flow": {"run_id": "r1"}, "check_network": {"mocked_500_seen": 2},
                "finding": {"finding_id": "f-1"},
                "pack": {"verify_before": {"ok": True}, "verify_after": {"ok": True}},
                "review": {"status_decision": "return-to-dev"}, "ticket_redacted": True,
                "mocks_left": 0, "restore": {"steps": [{"ok": True}]},
                "delayed_duration_s": 3.012, "incomplete_requests": 1,
                "options": {"mock_path": "/api/x", "ui": True}, "view": {"ok": True}}

    def test_evaluate(self) -> None:
        self.assertEqual(evaluate(self.good_report()), [])
        cases = {
            "error": ("error", "boom"),
            "fewer_than_two_checks": ("checks", [{}]),
            "flow_not_run": ("check_flow", {}),
            "mock_not_hit": ("check_network", {"mocked_500_seen": 0}),
            "no_finding": ("finding", None),
            "verify_before_review": ("pack", {"verify_after": {"ok": True}}),
            "review_not_recorded": ("review", {"status_decision": "approve"}),
            "ticket_not_redacted": ("ticket_redacted", False),
            "mock_left_in_registry": ("mocks_left", 1),
            "restore": ("restore", {"steps": [{"ok": False}]}),
            "view_check": ("view", {"ok": False}),
            "session_not_started": ("session", {"started": False}),
            "delay_not_observed": ("delayed_duration_s", 2.5),
            "no_incomplete_request": ("incomplete_requests", 0),
        }
        for reason, (key, value) in cases.items():
            with self.subTest(reason=reason):
                report = self.good_report()
                report[key] = value
                self.assertIn(reason, evaluate(report))
        report = self.good_report()
        report["mocks_left"] = None
        self.assertIn("mock_left_in_registry", evaluate(report))
        report = self.good_report()
        report["restore"] = {"steps": []}
        self.assertIn("restore", evaluate(report))
        report = self.good_report()
        report["delayed_duration_s"] = None
        self.assertIn("delay_not_observed", evaluate(report))
        self.assertEqual(evaluate({**self.good_report(), "delayed_duration_s": 2.95}), [])
        # without --mock-path neither the delay nor the hang is asked for
        report = self.good_report()
        report.update(options={"mock_path": None, "ui": False}, delayed_duration_s=None,
                      incomplete_requests=None)
        self.assertEqual(evaluate(report), [])

    def test_scrub(self) -> None:
        text, leaked = scrub({"note": "x secretmarker y", "api_token": "abc"}, "secretmarker")
        self.assertTrue(leaked)
        self.assertNotIn("secretmarker", text)
        self.assertNotIn("abc", text)
        _, leaked = scrub({"note": "clean"}, "secretmarker")
        self.assertFalse(leaked)

    def test_ticket_carries_the_marker_for_the_redaction_check(self) -> None:
        plan = {"flow": {"changed_file": "src/a.dart"}, "mock_glob": "*/api/x"}
        text = ticket_text(plan, "abcd", "pretestlivemarker")
        self.assertIn("Authorization: Bearer pretestlivemarker", text)
        self.assertIn("*/api/x", text)

    def test_owner_only_report_file(self) -> None:
        target = owner_dir(self.tmp / "owned") / "r.json"
        write_owner_file(target, "{}")
        self.assertEqual(target.stat().st_mode & 0o777, FILE_MODE)
        self.assertEqual(target.parent.stat().st_mode & 0o777, DIR_MODE)

    def test_dry_run_plans_the_nav_flow_under_the_mock(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(self.argv("--dry-run", "--mock-path", "/api/catalog"))
        plan = json.loads(out.getvalue())
        self.assertEqual(plan["nav_flow"], "results.yaml")
        step = next(s for s in plan["steps"] if s.startswith("check 2"))
        self.assertIn("flow run", step)
        self.assertIn("results.yaml", step)
        self.assertLess(step.index("mock"), step.index("flow run"))
        self.assertLess(step.index("flow run"), step.index("screenshot"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(self.argv("--dry-run", "--proxy-port", "8899"))
        plan = json.loads(out.getvalue())
        self.assertIsNone(plan["nav_flow"])
        self.assertIn("relaunch", next(s for s in plan["steps"] if s.startswith("check 2")))

    def test_nav_flow_path(self) -> None:
        plan = {"flow": choose_flow(self.flows, "android")}
        flow = str(self.flows / "results.yaml")
        other = self.tmp / "first.yaml"
        other.write_text("x", encoding="utf-8")
        # the first selected flow wins; missing files are skipped
        self.assertEqual(nav_flow_path([str(other), flow], plan, None), str(other))
        self.assertEqual(nav_flow_path([str(self.tmp / "gone.yaml")], plan, None), flow)
        self.assertEqual(nav_flow_path([], plan, None), flow)
        # a changed flow file is replayed from the temporary repository's copy
        copy = self.tmp / "repo" / ".autonom" / "flows" / "results.yaml"
        copy.parent.mkdir(parents=True)
        copy.write_text("x", encoding="utf-8")
        changed = {"flow": {"diff_mode": "flow_file_changed", "flow": flow,
                            "changed_file": ".autonom/flows/results.yaml"}}
        self.assertEqual(nav_flow_path([], changed, str(self.tmp / "repo")), str(copy))
        self.assertIsNone(nav_flow_path([], changed, None))
        self.assertIsNone(nav_flow_path([], {"flow": {"diff_mode": "covers",
                                                      "flow": str(self.tmp / "none")}}, None))

    def test_network_verdict(self) -> None:
        self.assertEqual(network_verdict("*/api/x*", 2)[0], "inconclusive")
        self.assertEqual(network_verdict("*/api/x*", 0)[0], "blocked")
        self.assertEqual(network_verdict(None, 0)[0], "inconclusive")

    def run_network_check(self, plan: dict[str, Any], state: dict[str, Any],
                          mocked: int) -> tuple[list[tuple[str, ...]], dict[str, Any]]:
        calls: list[tuple[str, ...]] = []

        def fake(*argv: str, timeout: float = CLI_TIMEOUT,
                 expect_ok: bool = True) -> dict[str, Any]:
            calls.append(argv)
            head = argv[:3]
            if head[:3] == ("pretest", "check", "add"):
                return {"ok": True, "check_id": "chk-4"}
            if head[:3] == ("pretest", "check", "end"):
                return {"ok": True, "verdict": argv[argv.index("--verdict") + 1]}
            if head[:3] == ("network", "mock", "add"):
                return {"ok": True, "mock": {"id": "m_1"}}
            if head[:2] == ("flow", "run"):
                # the forced 500 breaks the flow's assertions: a failed run, ok false
                return {"ok": False, "run_id": "fr_nav", "status": "failed",
                        "failure": {"error_code": "flow_assertion_timeout",
                                    "failure_class": "test_failure"}}
            if head[:1] == ("screenshot",):
                return {"ok": True, "path": "/x/0001.png", "width": 1, "height": 2,
                        "metadata": {"task": "chk-4", "mocks_active": 1}}
            if head[:3] == ("network", "requests", "list"):
                if "--mocked" in argv:
                    items = [{"id": f"r{i}"} for i in range(mocked)]
                    return {"ok": True, "total_matched": mocked, "requests": items}
                return {"ok": True, "total_matched": 4}
            if head[:3] == ("pretest", "finding", "add"):
                return {"ok": True, "finding_id": "f-1", "check_id": "chk-4"}
            return {"ok": True}

        report: dict[str, Any] = {"repo": {"path": None}}
        with mock.patch.object(time, "sleep"):
            step_check_network(fake, report, plan, state, "abcd", ["--env", "A=1"])
        return calls, report

    def test_network_check_replays_the_nav_flow_under_the_mock(self) -> None:
        flow = str(self.flows / "results.yaml")
        plan = {"mock_glob": "*/api/v1/catalog*", "capture": True, "app_id": "com.example.app",
                "flow": choose_flow(self.flows, "android")}
        calls, report = self.run_network_check(plan, {"selected_flow_paths": [flow]}, mocked=2)
        heads = [" ".join(argv[:3]) if argv[0] != "screenshot" else "screenshot"
                 for argv in calls]
        order = ["pretest check add", "pretest check begin", "network mock add",
                 f"flow run {flow}", "screenshot", "network requests list",
                 "network requests list", "pretest finding add", "pretest check end"]
        self.assertEqual([h for h in heads if h in order], order)
        self.assertNotIn("session launch com.example.app", heads)
        run = next(argv for argv in calls if argv[:2] == ("flow", "run"))
        self.assertEqual(run[3:], ("--env", "A=1"))
        result = report["check_network"]
        self.assertEqual(result["nav_flow"], "results.yaml")
        self.assertEqual(result["nav_flow_run_id"], "fr_nav")
        self.assertEqual(result["nav_flow_status"], "failed")
        self.assertEqual(result["nav_flow_error_code"], "flow_assertion_timeout")
        self.assertEqual(result["mocked_500_seen"], 2)
        self.assertEqual(result["verdict"], "inconclusive")
        finding = next(a for a in calls if a[:3] == ("pretest", "finding", "add"))
        self.assertIn("request:r0", finding)
        # no mocked 500 seen: the check is blocked, the run is still recorded
        _, report = self.run_network_check(plan, {"selected_flow_paths": [flow]}, mocked=0)
        self.assertEqual(report["check_network"]["verdict"], "blocked")
        self.assertEqual(report["check_network"]["nav_flow_run_id"], "fr_nav")

    def test_network_check_relaunches_without_a_flow(self) -> None:
        plan = {"mock_glob": "*/api/v1/catalog*", "capture": True, "app_id": "com.example.app",
                "flow": {"diff_mode": "covers", "flow": str(self.tmp / "none.yaml")}}
        calls, report = self.run_network_check(plan, {}, mocked=1)
        heads = [" ".join(argv[:2]) for argv in calls]
        self.assertIn("session launch", heads)
        self.assertNotIn("flow run", heads)
        self.assertLess(heads.index("network mock"), heads.index("session launch"))
        result = report["check_network"]
        self.assertIsNone(result["nav_flow"])
        self.assertTrue(result["relaunch_ok"])
        self.assertEqual(result["verdict"], "inconclusive")

    def run_slow_check(self, plan: dict[str, Any], state: dict[str, Any], *,
                       delayed_ms: int | None, incomplete_after: int | None,
                       incomplete_after_s: float | None = None
                       ) -> tuple[list[tuple[str, ...]], dict[str, Any], list[float]]:
        """step_check_slow against a fake CLI and a fake clock. `incomplete_after`
        is the number of polls before the held request shows up as incomplete
        (None: never); `incomplete_after_s` the same in seconds after the hang
        replay returned (None: not by time); `delayed_ms` the duration of the
        delayed request (None: none). The update takes 1 s, each replay 20 s."""
        calls: list[tuple[str, ...]] = []
        now = [1000.0]
        slept: list[float] = []
        phase: dict[str, Any] = {"name": None, "polls": 0, "replayed_at": None}

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        def fake(*argv: str, timeout: float = CLI_TIMEOUT,
                 expect_ok: bool = True) -> dict[str, Any]:
            calls.append(argv)
            if argv[:3] == ("pretest", "check", "add"):
                return {"ok": True, "check_id": "chk-3"}
            if argv[:3] == ("pretest", "check", "end"):
                return {"ok": True, "verdict": argv[argv.index("--verdict") + 1]}
            if argv[:3] == ("network", "mock", "update"):
                phase["name"] = "delay" if "--delay" in argv else "hang"
                now[0] += 1
                return {"ok": True}
            if argv[:2] == ("flow", "run"):
                now[0] += 20
                if phase["name"] == "hang":
                    phase["replayed_at"] = now[0]
                return {"ok": False, "run_id": f"fr_{phase['name']}", "status": "failed"}
            if argv[:3] == ("network", "requests", "list"):
                start = int(now[0] * 1000) - 5000
                other = {"id": "f_9", "mock_id": "m_9", "started_at_ms": start,
                         "completed": False}
                old = {"id": "f_0", "mock_id": "m_1", "started_at_ms": 1, "completed": False}
                items = [other, old]
                if phase["name"] == "delay" and delayed_ms is not None:
                    items.append({"id": "f_1", "mock_id": "m_1", "started_at_ms": start,
                                  "completed": True, "duration_ms": delayed_ms})
                if phase["name"] == "hang":
                    phase["polls"] += 1
                    late = (incomplete_after_s is not None
                            and now[0] - phase["replayed_at"] >= incomplete_after_s)
                    if late or (incomplete_after is not None
                                and phase["polls"] >= incomplete_after):
                        items.append({"id": "f_2", "mock_id": "m_1", "started_at_ms": start,
                                      "completed": False, "error_kind": "hang_cap",
                                      "status": None})
                return {"ok": True, "requests": items}
            return {"ok": True}

        report: dict[str, Any] = {"repo": {"path": None}}
        step_check_slow(fake, report, plan, state, ["--env", "A=1"], clock=clock, sleep=sleep)
        return calls, report, slept

    def slow_plan(self) -> dict[str, Any]:
        return {"mock_glob": "*/api/v1/catalog*", "capture": True, "app_id": "com.example.app",
                "flow": choose_flow(self.flows, "android")}

    def test_slow_check_measures_the_delay_and_the_held_request(self) -> None:
        flow = str(self.flows / "results.yaml")
        state = {"mock_id": "m_1", "selected_flow_paths": [flow]}
        calls, report, slept = self.run_slow_check(self.slow_plan(), state, delayed_ms=3104,
                                                   incomplete_after=3)
        heads = [" ".join(argv[:3]) for argv in calls]
        order = ["pretest check add", "pretest check begin", "network mock update",
                 f"flow run {flow}", "network requests list", "network mock update",
                 f"flow run {flow}", "network requests list", "network mock remove",
                 "pretest check end"]
        relevant = [h for h in heads if h in order]
        # the hang phase polls the request list until the held request is recorded
        collapsed = [h for i, h in enumerate(relevant)
                     if not (i and h == relevant[i - 1] == "network requests list")]
        self.assertEqual(collapsed, order)
        add = calls[0]
        self.assertEqual(add[add.index("--case") + 1], "slow")
        updates = [argv for argv in calls if argv[:3] == ("network", "mock", "update")]
        self.assertEqual(updates, [("network", "mock", "update", "m_1", "--delay", "3"),
                                   ("network", "mock", "update", "m_1", "--hang")])
        self.assertEqual(report["delayed_duration_s"], 3.104)
        self.assertEqual(report["incomplete_requests"], 1)  # only m_1's, only after --hang
        result = report["check_slow"]
        self.assertEqual(result["hang_polls"], 3)
        self.assertEqual(slept, [POLL_SECONDS, POLL_SECONDS])
        self.assertEqual(result["incomplete_error_kinds"], ["hang_cap"])
        self.assertEqual((result["delay_flow_run_id"], result["hang_flow_run_id"]),
                         ("fr_delay", "fr_hang"))
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertTrue(result["mock_removed"])
        self.assertIsNone(state["mock_id"], "a removed mock is not removed again in restore")
        run = next(argv for argv in calls if argv[:2] == ("flow", "run"))
        self.assertEqual(run[3:], ("--env", "A=1"))

    def test_slow_check_gives_up_after_the_hang_wait(self) -> None:
        state = {"mock_id": "m_1", "selected_flow_paths": [str(self.flows / "results.yaml")]}
        _, report, slept = self.run_slow_check(self.slow_plan(), state, delayed_ms=None,
                                               incomplete_after=None)
        self.assertIsNone(report["delayed_duration_s"])
        self.assertEqual(report["incomplete_requests"], 0)
        # waited the full HANG_WAIT_SECONDS after the hang replay, in POLL_SECONDS steps
        self.assertEqual(sum(slept), HANG_WAIT_SECONDS)
        self.assertEqual(set(slept), {POLL_SECONDS})
        self.assertEqual(report["check_slow"]["verdict"], "blocked")
        self.assertIn("delay_not_observed", evaluate({**self.good_report(), **{
            k: report[k] for k in ("delayed_duration_s", "incomplete_requests")}}))

    def test_slow_check_waits_after_the_replay_for_a_late_held_request(self) -> None:
        # The held request starts 15 s into the 20 s replay; the proxy caps it 120 s
        # later, 115 s after the replay returned. The wait must still be running then,
        # although that is 136 s after the --hang update.
        state = {"mock_id": "m_1", "selected_flow_paths": [str(self.flows / "results.yaml")]}
        calls, report, slept = self.run_slow_check(self.slow_plan(), state, delayed_ms=3104,
                                                   incomplete_after=None,
                                                   incomplete_after_s=115)
        self.assertEqual(report["incomplete_requests"], 1)
        self.assertEqual(sum(slept), 115)
        self.assertGreater(sum(slept) + 20 + 1, HANG_WAIT_SECONDS,
                           "the wait outlasts HANG_WAIT_SECONDS from the --hang update")
        self.assertEqual(report["check_slow"]["verdict"], "inconclusive")
        heads = [argv[:3] for argv in calls]
        self.assertLess(max(i for i, h in enumerate(heads) if h[:2] == ("flow", "run")),
                        heads.index(("network", "mock", "remove")))

    def test_slow_check_ends_the_check_and_removes_the_mock_on_failure(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake(*argv: str, timeout: float = CLI_TIMEOUT,
                 expect_ok: bool = True) -> dict[str, Any]:
            calls.append(argv)
            if argv[:3] == ("pretest", "check", "add"):
                return {"ok": True, "check_id": "chk-3"}
            if argv[:3] == ("network", "mock", "update"):
                raise StepFailed("autonom network mock update: invalid_value")
            return {"ok": True, "verdict": "blocked"}

        state = {"mock_id": "m_1"}
        report: dict[str, Any] = {"repo": {"path": None}}
        with self.assertRaises(StepFailed):
            step_check_slow(fake, report, self.slow_plan(), state, [])
        self.assertEqual(calls[-2][:3], ("network", "mock", "remove"))
        self.assertEqual(calls[-1][:3], ("pretest", "check", "end"))
        self.assertEqual(calls[-1][calls[-1].index("--verdict") + 1], "blocked")

    def test_slow_check_is_skipped_without_a_mock(self) -> None:
        calls: list[tuple[str, ...]] = []
        report: dict[str, Any] = {}
        step_check_slow(lambda *argv, **_: calls.append(argv) or {"ok": True}, report,
                        {**self.slow_plan(), "mock_glob": None}, {}, [])
        self.assertEqual(calls, [])
        self.assertEqual(report["check_slow"]["skipped"], "no mock on --mock-path")

    def test_rule_records_and_measures(self) -> None:
        listing = {"requests": [
            {"mock_id": "m_1", "started_at_ms": 10, "completed": True, "duration_ms": 3000},
            {"mock_id": "m_1", "started_at_ms": 20, "completed": True, "duration_ms": 3300},
            {"mock_id": "m_1", "started_at_ms": 5, "completed": True, "duration_ms": 9000},
            {"mock_id": "m_2", "started_at_ms": 30, "completed": False},
            {"mock_id": "m_1", "started_at_ms": 40, "completed": False, "duration_ms": 120000},
            {"mock_id": "m_1", "started_at_ms": "50"}, "junk"]}
        mine = rule_records(listing, "m_1", 10)
        self.assertEqual(len(mine), 3)
        self.assertEqual(delayed_duration_s(mine), 3.3)  # incomplete ones are not "delayed"
        self.assertEqual(incomplete_count(mine), 1)
        self.assertEqual(rule_records(listing, None, 0), [])
        self.assertIsNone(delayed_duration_s([]))

    def test_make_repo_diff_selects_the_flow(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git is not on PATH")
        from autonom_lib import proof

        for covers in (True, False):
            with self.subTest(covers=covers):
                if not covers:
                    (self.flows / "results.yaml").write_text(
                        _FLOW.replace('properties:\n  covers: "{covers}"\n', ""),
                        encoding="utf-8")
                plan = {"flows": str(self.flows),
                        "flow": choose_flow(self.flows, "android")}
                root = self.tmp / f"repo-{covers}"
                root.mkdir()
                repo = make_repo(root, plan, "abcd")
                changed = proof.changed_files(repo, "main", "live-change")
                self.assertEqual(changed, [plan["flow"]["changed_file"]])
                flows_dir = (repo / ".autonom" / "flows") if not covers else self.flows
                suite = proof.select_suite(flows_dir, repo, changed)
                self.assertEqual(len(suite["selected"]), 1)


def self_test() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTest)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
