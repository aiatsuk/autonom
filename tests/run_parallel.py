#!/usr/bin/env python3
"""Run the unit suite one module per process, in parallel, under the TTY guard.

The serial `unittest discover` took seven minutes, and `tty_guard.py` ran the
whole suite a second time. This runner does both jobs in one pass:

- every test module runs in its own interpreter, `os.cpu_count()` at a time
  (`AUTONOM_TEST_JOBS` overrides), slowest modules first;
- each worker gets its own scratch `AUTONOM_HOME`, so modules never share a
  machine store;
- each worker loads `test_aa_env_snapshot` first and `test_zz_env_hygiene`
  last around its module, so the environment-restoration guard still sees
  every test;
- each worker's stdin claims to be a TTY and raises on read (see
  `tty_guard.py`), so a test that would block a developer's terminal fails.

Exit 0 = every module passed. A failing module's full output is printed.
Usage: `python3 tests/run_parallel.py [module ...]`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FIRST = "test_aa_env_snapshot"
LAST = "test_zz_env_hygiene"

# Runs inside each worker: install the refusing TTY, then hand over to unittest.
WORKER = """
import sys, unittest
sys.path.insert(0, {tests!r})
from tty_guard import TtyThatRefusesToBeRead
sys.stdin = TtyThatRefusesToBeRead()
unittest.main(module=None, argv=["worker", *sys.argv[1:]])
"""

# Modules measured slowest; started first so the long tail runs alongside
# the short ones instead of after them.
SLOW_FIRST = (
    "test_fix_ios_logs_uuid", "test_cli_hardening", "test_capture_hardening",
    "test_flow_executor", "test_network", "test_device_sweep_fixes",
    "test_fix_cli", "test_tour", "test_fix_ios_dev", "test_bare_host",
    "test_flow_report", "test_capture_state", "test_contract_golden",
    "test_follow",
)

RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")


def discover() -> list[str]:
    names = sorted(path.stem for path in TESTS.glob("test_*.py"))
    return [name for name in names if name not in (FIRST, LAST)]


def order(modules: list[str]) -> list[str]:
    rank = {name: index for index, name in enumerate(SLOW_FIRST)}
    return sorted(modules, key=lambda name: (rank.get(name, len(rank)), name))


def run_module(module: str) -> tuple[str, int, str, float]:
    home = tempfile.mkdtemp(prefix="autonom-test-")
    env = {**os.environ, "AUTONOM_HOME": home}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", WORKER.format(tests=str(TESTS)),
             FIRST, module, LAST],
            cwd=TESTS, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return module, proc.returncode, proc.stdout, time.monotonic() - started


def main(argv: list[str]) -> int:
    modules = order(argv or discover())
    jobs = int(os.environ.get("AUTONOM_TEST_JOBS") or os.cpu_count() or 2)
    started = time.monotonic()
    failed: list[str] = []
    total = skipped = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for module, code, output, seconds in pool.map(run_module, modules):
            ran = RAN.search(output)
            total += int(ran.group(1)) if ran else 0
            skip = SKIPPED.search(output.rsplit("\n", 3)[-2] if output else "")
            skipped += int(skip.group(1)) if skip else 0
            status = "ok" if code == 0 else "FAILED"
            print(f"{status:6} {seconds:6.1f}s  {module}", flush=True)
            if code != 0:
                failed.append(module)
                print(output, flush=True)
    elapsed = time.monotonic() - started
    print(f"\nRan {total} tests (incl. env guards) in {len(modules)} modules, "
          f"{jobs} jobs, {elapsed:.1f}s; skipped={skipped}.")
    if failed:
        print("FAILED modules: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("OK — no test read the terminal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
