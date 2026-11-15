"""`metrics simulator`: the memory of a whole booted iOS Simulator.

Hermetic: the host's `pgrep`, `ps` and `top` are an injected runner, the
device list is the fake `simctl`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_slim  # noqa: E402
from autonom_lib.metrics import simulator_tree  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

PS = """  PID  PPID  %CPU COMM
    1     0   0,1 /sbin/launchd
  500     1   1.5 /Library/Developer/CoreSimulator/launchd_sim
  501   500   0.5 /Runtime Root/usr/libexec/backboardd
  502   500   2.0 /Runtime Root/System/Library/CoreServices/SpringBoard.app/SpringBoard
  503   502   0.0 /Runtime Root/usr/libexec/assetsd
  504   500   0.0 /Runtime Root/usr/libexec/late_starter
  900     1  50.0 /Applications/Other.app/Contents/MacOS/Other
  901   900   9.0 helper
garbage line
"""
TOP = """Processes: 600 total, 3 running
PhysMem: 20G used

PID    MEM
1      20M
500    4096K
501    1.5G+
502    512M-
503    100M
900    8G
901    1G
x      1M
"""


def runner_for(pgrep: str = "500\n777\n", ps: str = PS, top: str = TOP):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        out = {"pgrep": pgrep, "ps": ps, "top": top}[argv[0]]
        return subprocess.CompletedProcess(argv, 0 if out else 1, out, "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


class ParseTests(unittest.TestCase):
    def test_memory_suffixes_are_binary_and_signs_are_dropped(self) -> None:
        cases = {"0B": 0, "512B": 512, "1234K": 1234 * 1024, "56M+": 56 << 20,
                 "1.5G-": int(1.5 * (1 << 30)), "2G": 2 << 30, "1T": 1 << 40,
                 "17": 17, " 3m ": 3 << 20}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(simulator_tree.parse_memory(text), expected)
        for text in ("", "N/A", "-", "1.2.3M", "12Q", "+"):
            with self.subTest(text=text):
                self.assertIsNone(simulator_tree.parse_memory(text))

    def test_ps_table(self) -> None:
        table = simulator_tree.parse_ps(PS)
        self.assertEqual(table[500], (1, 1.5, "/Library/Developer/CoreSimulator/launchd_sim"))
        self.assertEqual(table[1][1], 0.1)  # a decimal comma locale
        self.assertEqual(table[501][2], "/Runtime Root/usr/libexec/backboardd")
        self.assertEqual(len(table), 8)
        self.assertEqual(simulator_tree.parse_ps(""), {})

    def test_top_table(self) -> None:
        memory = simulator_tree.parse_top(TOP)
        self.assertEqual(memory[500], 4096 * 1024)
        self.assertEqual(memory[501], int(1.5 * (1 << 30)))
        self.assertEqual(memory[502], 512 << 20)
        self.assertNotIn(600, memory)
        self.assertEqual(len(memory), 7)

    def test_tree_is_the_root_and_its_descendants_only(self) -> None:
        table = simulator_tree.parse_ps(PS)
        self.assertEqual(simulator_tree.tree_pids(500, table), [500, 501, 502, 503, 504])
        self.assertEqual(simulator_tree.tree_pids(900, table), [900, 901])
        # a pid cycle in a torn ps sample does not loop
        self.assertEqual(simulator_tree.tree_pids(1, {1: (2, 0.0, "a"), 2: (1, 0.0, "b")}),
                         [1, 2])


class TakeTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.set_device("Booted")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.root / "log.jsonl"),
                     AUTONOM_HOME=str(self.root / "home"),
                     AUTONOM_SIMDEVICE_TMP_ROOT=str(self.root / "var-tmp"))
        self.env = dict(os.environ)
        self.target = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    def set_device(self, state: str) -> None:
        self.state.write_text(json.dumps({"simctl_devices": {"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": state,
                 "isAvailable": True}]}}}), encoding="utf-8")

    def error(self, target=None, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            simulator_tree.take(target or self.target, **kwargs)
        error = caught.exception
        self.assertTrue(error.code and error.message and error.hint, error.as_dict())
        return error

    def test_sums_the_tree(self) -> None:
        run = runner_for()
        reply = simulator_tree.take(self.target, runner=run)
        self.assertEqual(reply["metric_semantics"], "ios_simulator_tree_footprint_v1")
        self.assertEqual(reply["processes"], 5)
        self.assertEqual(reply["root_pid"], 500)
        self.assertEqual(reply["footprint_bytes"],
                         4096 * 1024 + int(1.5 * (1 << 30)) + (512 << 20) + (100 << 20))
        self.assertEqual(reply["cpu_percent"], 4.0)
        self.assertEqual(reply["top"], [
            {"name": "backboardd", "pid": 501, "bytes": int(1.5 * (1 << 30))},
            {"name": "SpringBoard", "pid": 502, "bytes": 512 << 20},
            {"name": "assetsd", "pid": 503, "bytes": 100 << 20},
            {"name": "launchd_sim", "pid": 500, "bytes": 4096 * 1024}])
        self.assertEqual(reply["slim"], {"state": "stock", "disabled": 0,
                                         "total": len(ios_slim.CATALOG_LABELS),
                                         "source": "store"})
        # one process had no footprint in the top sample: said, not hidden
        self.assertTrue(any("1 of 5" in line for line in reply["limitations"]))
        self.assertEqual(run.calls, [
            ["pgrep", "-f", f"{UDID}/data/var/run/launchd_bootstrap"],
            ["ps", "-axo", "pid,ppid,%cpu,comm"],
            ["top", "-l", "1", "-stats", "pid,mem"]])

    def test_top_five_only(self) -> None:
        ps = "PID PPID %CPU COMM\n500 1 0.0 launchd_sim\n" + "".join(
            f"{600 + index} 500 0.0 /x/daemon{index}\n" for index in range(8))
        top = "PID MEM\n500 1M\n" + "".join(
            f"{600 + index} {index + 2}M\n" for index in range(8))
        reply = simulator_tree.take(self.target, runner=runner_for(ps=ps, top=top))
        self.assertEqual([item["name"] for item in reply["top"]],
                         ["daemon7", "daemon6", "daemon5", "daemon4", "daemon3"])
        self.assertEqual(reply["processes"], 9)

    def test_slim_state_comes_from_the_store(self) -> None:
        labels = sorted(ios_slim.resolve({"preset": "minimal"}))
        ios_slim.write_store(UDID, {label: True for label in labels})
        reply = simulator_tree.take(self.target, runner=runner_for())
        self.assertEqual(reply["slim"]["state"], "slim")
        self.assertEqual(reply["slim"]["disabled"], len(labels))

    def test_unreadable_store_is_unknown_not_a_failure(self) -> None:
        path = ios_slim.store_path(UDID)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a plist")
        reply = simulator_tree.take(self.target, runner=runner_for())
        self.assertEqual(reply["slim"]["state"], "unknown")
        self.assertIsNone(reply["slim"]["disabled"])

    def test_shutdown_simulator_is_refused_before_any_host_call(self) -> None:
        self.set_device("Shutdown")
        run = runner_for()
        error = self.error(runner=run)
        self.assertEqual(error.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(error.extra["state"], "Shutdown")
        self.assertIn("devices boot", error.hint)
        self.assertEqual(run.calls, [])

    def test_missing_root_process(self) -> None:
        error = self.error(runner=runner_for(pgrep=""))
        self.assertEqual(error.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertIn("launchd_sim", error.message)
        error = self.error(runner=runner_for(pgrep="not-a-pid\n"))
        self.assertEqual(error.code, errors.UNSUPPORTED_CAPABILITY)

    def test_top_without_a_usable_sample_is_an_error_not_zero_bytes(self) -> None:
        for top in ("", "PID MEM\n", "PID MEM\n900 8G\n"):
            with self.subTest(top=top):
                error = self.error(runner=runner_for(top=top))
                self.assertEqual(error.code, errors.BACKEND_FAILED)
                self.assertEqual(error.extra["processes"], 5)

    def test_root_gone_between_pgrep_and_ps(self) -> None:
        error = self.error(runner=runner_for(pgrep="4242\n"))
        self.assertEqual(error.code, errors.UNSUPPORTED_CAPABILITY)

    def test_unknown_udid_and_android(self) -> None:
        other = Target(IOS, "CCCCCCCC-1111-2222-3333-DDDDDDDDDDDD", str(FAKE_SIMCTL), {})
        self.assertEqual(self.error(other, runner=runner_for()).code, errors.NO_TARGET)
        android = self.error(Target(ANDROID, "emulator-5554", "adb", {}))
        self.assertEqual(android.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(android.extra["capability"], "metrics.simulator")

    def test_missing_host_tool_is_tool_missing(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            simulator_tree.run_host(["autonom-no-such-tool-xyz", "-f", "x"])
        error = caught.exception
        self.assertEqual(error.code, errors.TOOL_MISSING)
        self.assertEqual(error.extra["tool"], "autonom-no-such-tool-xyz")
        self.assertTrue(error.hint)

    def test_cli_refuses_a_shutdown_simulator_as_json(self) -> None:
        self.set_device("Shutdown")
        done = subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--udid", UDID,
             "metrics", "simulator"], capture_output=True, text=True, env=self.env,
            timeout=120)
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        self.assertNotEqual(done.returncode, 0)
        payload = json.loads(done.stdout or done.stderr)
        self.assertEqual(payload["error_code"], errors.UNSUPPORTED_CAPABILITY)
        self.assertTrue(payload["hint"])


if __name__ == "__main__":
    unittest.main()
