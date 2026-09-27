"""A stop that lands while the bounded log writer starts its child.

The writer used to start `log stream` first and install its SIGTERM handler
after: a stop in between killed only the writer and left the child streaming
with nobody to stop it. The fault hook below delivers SIGTERM right after the
fork, before the writer has the child's handle.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import ios_simctl  # noqa: E402

FAULT = r'''
import os, signal, subprocess, sys
_child_file = sys.argv.pop(1)
_real_popen = subprocess.Popen
def _popen_then_stop(*args, **kwargs):
    child = _real_popen(*args, **kwargs)
    with open(_child_file, "w") as handle:
        handle.write(str(child.pid))
    os.kill(os.getpid(), signal.SIGTERM)
    return child
subprocess.Popen = _popen_then_stop
'''

PRODUCER = "import time; time.sleep(60)"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True, check=False).stdout
    return not state.strip().startswith("Z")


class StopWhileStartingTests(unittest.TestCase):
    def test_a_stop_during_the_childs_start_stops_the_child_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child_file = Path(tmp) / "child.pid"
            dest = Path(tmp) / "stream.ndjson"
            writer = subprocess.Popen(
                [sys.executable, "-c", FAULT + ios_simctl._BOUNDED_WRITER,  # noqa: SLF001
                 str(child_file), str(dest), "10000", sys.executable, "-c", PRODUCER],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            code = writer.wait(timeout=30)
            self.assertTrue(child_file.exists(), "the writer never started its child")
            child = int(child_file.read_text())

            def reap_child() -> None:
                if _alive(child):
                    os.kill(child, signal.SIGKILL)

            self.addCleanup(reap_child)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _alive(child):
                time.sleep(0.05)
            self.assertFalse(_alive(child), "the writer's child outlived a stop during its start")
            self.assertEqual(code, 0, "the writer's own handler carried out the stop")


if __name__ == "__main__":
    unittest.main()
