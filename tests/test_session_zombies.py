"""An exited process whose parent has not reaped it yet is not alive.

A zombie still answers kill(pid, 0). Before this, terminate_pid waited out
its whole timeout for every such process (and SIGKILLed a corpse), and
pid_alive reported an exited process as running (so `logs follow` tailed the
file of a dead writer and doctor stayed quiet about it).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import session  # noqa: E402


def _ps_says_zombie(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True, check=False).stdout
    return state.strip().startswith("Z")


def _orphaned_zombie(test: unittest.TestCase) -> int:
    """A grandchild that exits while its parent (a sleeping middleman that
    never waits) is alive: a zombie this test process cannot reap."""
    middleman = subprocess.Popen(
        [sys.executable, "-c",
         "import os, sys, time\n"
         "pid = os.fork()\n"
         "if pid == 0:\n"
         "    time.sleep(0.2)\n"
         "    os._exit(0)\n"
         "print(pid, flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)

    def stop() -> None:
        middleman.kill()
        middleman.wait(timeout=10)
        middleman.stdout.close()

    test.addCleanup(stop)
    zombie = int(middleman.stdout.readline())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not _ps_says_zombie(zombie):
        time.sleep(0.05)
    test.assertTrue(_ps_says_zombie(zombie), "the grandchild never became a zombie")
    return zombie


class ZombieTests(unittest.TestCase):
    def test_a_zombie_is_not_alive(self) -> None:
        zombie = _orphaned_zombie(self)
        os.kill(zombie, 0)  # the kernel still answers for it
        self.assertFalse(session.pid_alive(zombie))

    def test_terminating_a_zombie_returns_at_once(self) -> None:
        zombie = _orphaned_zombie(self)
        started = time.monotonic()
        self.assertTrue(session.terminate_pid(zombie, timeout=5.0))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_live_process_is_alive_and_is_terminated(self) -> None:
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (live.poll() is None and live.kill(), live.wait(timeout=10)))
        self.assertTrue(session.pid_alive(live.pid))
        self.assertTrue(session.terminate_pid(live.pid, timeout=5.0))
        self.assertIsNotNone(live.poll())

    def test_no_pid_and_a_gone_pid_are_not_alive(self) -> None:
        self.assertFalse(session.pid_alive(None))
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=10)
        self.assertFalse(session.pid_alive(gone.pid))


if __name__ == "__main__":
    unittest.main()
