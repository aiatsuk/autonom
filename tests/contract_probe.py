#!/usr/bin/env python3
"""Record and compare the Android JSON contract of the Autonom CLI.

The golden file this produces is the oracle for VER-001 / RISK-002: it is
*recorded* from a working build rather than hand-written, so a key that is
silently renamed during a refactor fails here even though every hand-written
assertion still passes.

Only key paths and JSON types are compared. Values (session ids, timestamps,
absolute paths) legitimately vary between runs.

Capture the golden:

    python3 tests/contract_probe.py --write

Compare the current build against it (also run by tests/test_contract_golden.py):

    python3 tests/contract_probe.py
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Overridable so the golden can be recorded from a pristine 0.4.0 export.
CLI = Path(os.environ.get("AUTONOM_CONTRACT_CLI") or (ROOT / "scripts/autonom.py"))
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
GOLDEN = ROOT / "tests/fixtures/android_contract_golden.json"

SELECTOR = ("--desc", "Flutter Save Button", "--mode", "exact")

# Pin the target. Without this the probe depends on whatever else the machine
# has booted: a running iOS simulator makes resolution ambiguous, and the
# comparison would report a product regression that is really a test defect.
PIN = ("--serial", "emulator-5554")

# Ordered: session-creating commands must run before the ones that read it.
PROBES: list[tuple[str, tuple[str, ...]]] = [
    ("version", ("version",)),
    ("devices", ("devices",)),
    ("session_start", ("session", "start", "--app-id", "com.example.app")),
    ("session_show", ("session", "show",)),
    ("ui_tree_dump", ("ui", "tree", "--dump", str(UI_FIXTURE))),
    ("ui_tree_live", ("ui", "tree")),
    ("ui_find_dump", ("ui", "find", "--dump", str(UI_FIXTURE)) + SELECTOR),
    ("ui_tap_selector", ("ui", "tap") + SELECTOR),
    ("ui_tap_coords", ("ui", "tap", "--x", "100", "--y", "200")),
    ("ui_type", ("ui", "type", "hello world")),
    ("ui_key", ("ui", "key", "KEYCODE_BACK")),
    ("screenshot", ("screenshot",)),
    ("logs_tail", ("logs", "tail", "--package", "com.example.app", "--max-lines", "20")),
    ("flow_check", ("flow", "check", str(ROOT / "tests/fixtures/flows/contract_pass.yaml"))),
    ("flow_run_pass", ("flow", "run", str(ROOT / "tests/fixtures/flows/contract_pass.yaml"))),
    ("flow_run_test_failure",
     ("flow", "run", str(ROOT / "tests/fixtures/flows/contract_fail.yaml"))),
    ("session_stop", ("session", "stop")),
]

FAKE_STATE = {
    "devices": [["emulator-5554", "device", "product:sdk_gphone64_arm64"]],
    "ui_dump": str(UI_FIXTURE),
    "pidof": {"com.example.app": "4242"},
    "logcat": [
        "01-01 00:00:00.000  4242  4242 I Example: hello from the app",
        "01-01 00:00:01.000  4242  4242 W Example: something to look at",
    ],
}


def key_paths(value: Any, prefix: str = "") -> set[str]:
    """Flatten a JSON value into a set of ``path:type`` strings."""
    kind = type(value).__name__
    if isinstance(value, dict):
        found = {f"{prefix}:object"} if prefix else set()
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else key
            found |= key_paths(item, child)
        return found
    if isinstance(value, list):
        found = {f"{prefix}:array"}
        for item in value:
            found |= key_paths(item, f"{prefix}[]")
        return found
    if value is None:
        # A null carries no type information; record presence only, so a field
        # that is null in one run and populated in another does not flap.
        return {f"{prefix}:null-or-value"}
    if isinstance(value, bool):
        return {f"{prefix}:bool"}
    if isinstance(value, (int, float)):
        return {f"{prefix}:number"}
    return {f"{prefix}:{kind}"}


def normalize(paths: set[str]) -> set[str]:
    """Collapse nullable fields so ``null`` and a real value compare equal."""
    plain = {path.rsplit(":", 1)[0] for path in paths if path.endswith(":null-or-value")}
    result = set()
    for path in paths:
        head, _, _tail = path.rpartition(":")
        result.add(f"{head}:nullable" if head in plain else path)
    return result


def run(argv: tuple[str, ...], workdir: Path, env: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(CLI), "--adb", str(FAKE_ADB), *PIN, *argv],
        cwd=workdir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload: dict[str, Any] = {"exit_code": completed.returncode}
    # Exit 1 is a *reported* failure (doctor --strict, flow run test failures):
    # the report lands on stdout. Exit 2 is an error envelope on stderr.
    stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
    try:
        payload["body"] = json.loads(stream)
    except json.JSONDecodeError:
        payload["body"] = {"__unparsed__": stream[:400]}
    return payload


def collect() -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        state_path = workdir / "fake-state.json"
        state_path.write_text(json.dumps(FAKE_STATE), encoding="utf-8")
        env = dict(os.environ)
        env["AUTONOM_FAKE_STATE"] = str(state_path)
        # Sessions are machine-global; keep the probe's session inside its own
        # tempdir so it neither reads nor pollutes the developer's real store.
        env["AUTONOM_HOME"] = str(workdir / "home")
        env.pop("AUTONOM_FAKE_LOG", None)

        recorded: dict[str, dict[str, Any]] = {}
        root_prefix = str(ROOT) + os.sep
        for name, argv in PROBES:
            result = run(argv, workdir, env)
            recorded[name] = {
                # Store argv repo-relative so the committed golden is portable
                # and carries no absolute home path. argv is informational only —
                # `compare()` checks keys and exit codes, never argv.
                "argv": [token.replace(root_prefix, "") for token in argv],
                "exit_code": result["exit_code"],
                "keys": sorted(normalize(key_paths(result["body"]))),
            }
        return recorded


def compare(current: dict[str, Any], golden: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, expected in golden.items():
        actual = current.get(name)
        if actual is None:
            problems.append(f"{name}: probe missing from the current build")
            continue
        if actual["exit_code"] != expected["exit_code"]:
            problems.append(
                f"{name}: exit code {expected['exit_code']} -> {actual['exit_code']}"
            )
        missing = sorted(set(expected["keys"]) - set(actual["keys"]))
        for key in missing:
            problems.append(f"{name}: contract key removed or retyped: {key}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="record a new golden file")
    args = parser.parse_args()

    current = collect()
    if args.write:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        total = sum(len(entry["keys"]) for entry in current.values())
        print(f"Recorded {len(current)} probes / {total} contract keys -> {GOLDEN}")
        return 0

    if not GOLDEN.exists():
        print(f"golden file missing: {GOLDEN}", file=sys.stderr)
        return 1
    problems = compare(current, json.loads(GOLDEN.read_text(encoding="utf-8")))
    for problem in problems:
        print(f"ERROR {problem}", file=sys.stderr)
    if problems:
        print(f"Contract check failed with {len(problems)} problem(s).", file=sys.stderr)
        return 1
    print(f"Contract preserved across {len(current)} probes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
