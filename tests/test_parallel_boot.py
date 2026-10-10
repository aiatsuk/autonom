"""Several emulator boots at once: the boot lock, `--port`, and the serial claim.

A Canvas workspace can boot two AVDs at the same moment. Each boot must end
up with its own emulator's serial: `emulator-<port>` when a port is given,
otherwise a new serial whose console names the AVD this boot launched. An AVD
that already runs is returned as it is. Everything runs against the fake
emulator and fake adb; no real device is touched.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_EMULATOR = ROOT / "tests/fakes/fake_emulator.py"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import emulator as emulator_mod, errors, processes  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def _bindable(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_console_ports(count: int) -> list[int]:
    """Even console ports whose console and adb ports are both free here
    (a real emulator on this machine may hold some of them)."""
    found = [port for port in reversed(emulator_mod.EMULATOR_PORTS)
             if _bindable(port) and _bindable(port + 1)]
    if len(found) < count:
        raise unittest.SkipTest("not enough free emulator ports on this machine")
    return found[:count]


class BootCase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.home = self.root / "home"
        self.set_state(devices=[])
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_HOME=str(self.home), AUTONOM_EMULATOR=None, AUTONOM_ADB=None)
        self.env = dict(os.environ)

    def set_state(self, **values) -> None:
        values.setdefault("avds", ["Pixel_9", "Pixel_10"])
        self.state.write_text(json.dumps(values), encoding="utf-8")

    def emulator_calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"] for row in rows
                if row.get("tool") == "emulator" and row["argv"][:1] == ["-avd"]]

    def boot(self, name: str = "Pixel_9", **kwargs):
        kwargs.setdefault("timeout", 20)
        return emulator_mod.boot_avd(str(FAKE_EMULATOR), str(FAKE_ADB), name, **kwargs)

    def cli_argv(self, *argv: str) -> list[str]:
        return [sys.executable, str(CLI), "devices", "boot", "--adb", str(FAKE_ADB),
                "--emulator", str(FAKE_EMULATOR), *argv]

    def cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(self.cli_argv(*argv), capture_output=True, text=True,
                                   env=self.env, timeout=120, stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        return completed.returncode, json.loads(stream)


class PortBootTests(BootCase):
    def test_two_port_boots_in_parallel_get_their_own_serials(self) -> None:
        first, second = free_console_ports(2)
        self.set_state(devices=[], boot_delay=1.0)
        children = [
            subprocess.Popen(self.cli_argv("--avd", name, "--port", str(port), "--timeout", "60"),
                             env=self.env, text=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for name, port in (("Pixel_9", first), ("Pixel_10", second))]
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual([(item["avd"], item["serial"], item["port"]) for item in results],
                         [("Pixel_9", f"emulator-{first}", first),
                          ("Pixel_10", f"emulator-{second}", second)])
        for item in results:
            self.assertTrue(item["booted"])
            self.assertIs(item["already_running"], False)
        # no boot lost the other's device row
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(sorted(row[0] for row in state["devices"]),
                         sorted([f"emulator-{first}", f"emulator-{second}"]))
        self.assertEqual(sorted(call[call.index("-port") + 1] for call in self.emulator_calls()),
                         sorted([str(first), str(second)]))
        self.assertFalse(list(self.root.glob("state.json.lock*")))

    def test_the_port_is_on_argv_and_in_the_registry_at_launch(self) -> None:
        [port] = free_console_ports(1)
        detail = self.boot(port=port, wait=False)
        self.assertEqual(detail["port"], port)
        self.assertIs(detail["already_running"], False)
        deadline = time.monotonic() + 15  # the fake logs its argv as it starts
        while not self.emulator_calls() and time.monotonic() < deadline:
            time.sleep(0.05)
        [call] = self.emulator_calls()
        self.assertEqual(call[:4], ["-avd", "Pixel_9", "-port", str(port)])
        [row] = [entry for entry in processes.entries() if entry.get("kind") == "emulator"]
        self.assertEqual(row["serial"], f"emulator-{port}")
        self.assertEqual(row["owner"], processes.HARNESS_OWNER)

    def test_a_port_another_emulator_holds_is_unavailable(self) -> None:
        [port] = free_console_ports(1)
        self.set_state(devices=[[f"emulator-{port}", "offline", ""]])
        with self.assertRaises(errors.AutonomError) as caught:
            self.boot(port=port)
        self.assertEqual(caught.exception.code, errors.PORT_UNAVAILABLE)
        self.assertEqual(caught.exception.extra,
                         {"port": port, "serial": f"emulator-{port}"})
        self.assertEqual(self.emulator_calls(), [])

    def test_a_bound_console_or_adb_port_is_unavailable(self) -> None:
        [port] = free_console_ports(1)
        for taken in (port, port + 1):
            with self.subTest(taken=taken), socket.socket() as busy:
                busy.bind(("127.0.0.1", taken))
                busy.listen()
                with self.assertRaises(errors.AutonomError) as caught:
                    self.boot(port=port)
                self.assertEqual(caught.exception.code, errors.PORT_UNAVAILABLE)
        self.assertEqual(self.emulator_calls(), [])

    def test_two_boots_on_one_port_during_startup_leave_one_winner(self) -> None:
        [port] = free_console_ports(1)
        self.set_state(devices=[], boot_delay=1.0)
        children = [
            subprocess.Popen(self.cli_argv("--avd", name, "--port", str(port), "--timeout", "60"),
                             env=self.env, text=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for name in ("Pixel_9", "Pixel_10")]
        outcomes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=120)
            outcomes.append((child.returncode, json.loads(stdout if child.returncode == 0
                                                          else stderr)))
        self.assertEqual(sorted(code for code, _ in outcomes), [0, 2], outcomes)
        [won] = [payload for code, payload in outcomes if code == 0]
        [lost] = [payload for code, payload in outcomes if code == 2]
        self.assertEqual(lost["error_code"], "port_unavailable")
        self.assertEqual(won["serial"], f"emulator-{port}")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["avd_names"], {f"emulator-{port}": won["avd"]})
        self.assertEqual(len(self.emulator_calls()), 1, "the loser launched an emulator")

    def test_a_launch_still_starting_on_the_port_makes_it_unavailable(self) -> None:
        [port] = free_console_ports(1)
        self.set_state(devices=[], boot_delay=2.0)
        self.boot(port=port, wait=False)  # registered, not listed by adb yet
        with self.assertRaises(errors.AutonomError) as caught:
            self.boot("Pixel_10", port=port)
        self.assertEqual(caught.exception.code, errors.PORT_UNAVAILABLE)
        self.assertIn("still starting", caught.exception.message)

    def test_a_registry_row_whose_pid_is_gone_does_not_block(self) -> None:
        [port] = free_console_ports(1)
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=30)
        processes.register("emulator", gone.pid, avd="Old", serial=f"emulator-{port}",
                           owner=processes.HARNESS_OWNER)
        detail = self.boot(port=port)
        self.assertEqual(detail["serial"], f"emulator-{port}")

    def test_an_emulator_that_took_the_port_first_is_never_claimed(self) -> None:
        # an emulator launched elsewhere takes the port during this boot; ours
        # fails to bind and the serial runs the other AVD
        [port] = free_console_ports(1)
        self.set_state(devices=[], boot_first=[f"emulator-{port}", "Other_AVD"])
        with self.assertRaises(errors.AutonomError) as caught:
            self.boot(port=port)
        self.assertEqual(caught.exception.code, errors.PORT_UNAVAILABLE)
        self.assertIn("Other_AVD", caught.exception.message)
        self.assertEqual([e for e in processes.entries() if e.get("kind") == "emulator"], [])

    def test_a_port_left_in_time_wait_is_free(self) -> None:
        [port] = free_console_ports(1)
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen()
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        accepted, _ = server.accept()
        accepted.close()  # the server side closes first: TIME_WAIT on `port`
        time.sleep(0.1)
        client.close()
        server.close()
        self.assertTrue(emulator_mod._loopback_port_free(port))
        with socket.socket() as busy:
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            busy.bind(("127.0.0.1", port))
            busy.listen()
            self.assertFalse(emulator_mod._loopback_port_free(port))

    def test_invalid_ports_are_refused_before_anything_runs(self) -> None:
        for port in (5555, 5552, 5684, 0, 80, -5554):
            with self.subTest(port=port):
                with self.assertRaises(errors.AutonomError) as caught:
                    self.boot(port=port)
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        code, payload = self.cli("--avd", "Pixel_9", "--port", "5555")
        self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))
        code, payload = self.cli("--port", "5580")
        self.assertEqual((code, payload["error_code"]), (2, "invalid_value"))
        self.assertIn("--avd", payload["error"])
        self.assertEqual(self.emulator_calls(), [])
        self.assertFalse(self.log.exists() and "-list-avds" in self.log.read_text())


class ClaimTests(BootCase):
    @staticmethod
    def stop_emulator(pid: int) -> None:
        """Stop a lingering fake emulator: `boot_avd` leaves a timed-out
        emulator running (it may still be booting)."""
        processes.terminate_group(pid, timeout=5.0)  # also reaps our child

    def test_without_a_port_the_serial_is_claimed_by_avd_name(self) -> None:
        # another boot's emulator appears first; it is not this boot's
        self.set_state(devices=[], boot_first=["emulator-5558", "Other_AVD"],
                       boot_serials={"Pixel_9": "emulator-5562"}, boot_delay=1.5)
        detail = self.boot()
        self.assertEqual((detail["serial"], detail["port"]), ("emulator-5562", 5562))
        [row] = [entry for entry in processes.entries() if entry.get("kind") == "emulator"]
        self.assertEqual(row["serial"], "emulator-5562")

    def test_two_boots_without_ports_in_parallel(self) -> None:
        self.set_state(devices=[], boot_delay=0.5,
                       boot_serials={"Pixel_9": "emulator-5562", "Pixel_10": "emulator-5564"})
        children = [subprocess.Popen(self.cli_argv("--avd", name, "--timeout", "60"),
                                     env=self.env, text=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for name in ("Pixel_9", "Pixel_10")]
        serials = {}
        for child in children:
            stdout, stderr = child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0, stderr)
            payload = json.loads(stdout)
            serials[payload["avd"]] = payload["serial"]
        self.assertEqual(serials, {"Pixel_9": "emulator-5562", "Pixel_10": "emulator-5564"})

    def test_a_console_that_never_names_its_avd_is_never_claimed(self) -> None:
        # The emulator keeps running, as a real one does. A fake that exited
        # at once raced the boot's own rule — an emulator gone for more than
        # 3 s before a device is claimed is `backend_failed` — against this
        # 3 s timeout, and under load the exit rule won.
        self.set_state(devices=[], boot_no_name=True, boot_linger=60)
        with self.assertRaises(errors.AutonomError) as caught:
            self.boot(timeout=3)
        [row] = [entry for entry in processes.entries() if entry.get("kind") == "emulator"]
        self.addCleanup(self.stop_emulator, row["pid"])
        self.assertEqual(caught.exception.code, errors.BOOT_TIMEOUT)
        self.assertNotIn("waiting_for", caught.exception.extra)
        self.assertNotIn("serial", row)
        # the timeout came while the emulator still ran: the console rule,
        # not the emulator's exit, kept the serial unclaimed
        self.assertTrue(session_mod.pid_alive(row["pid"]))


class AlreadyRunningTests(BootCase):
    def test_a_running_avd_is_returned_without_a_launch(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]],
                       avd_names={"emulator-5554": "Pixel_9"})
        detail = self.boot()
        self.assertEqual(detail, {"avd": "Pixel_9", "pid": None, "booted": False,
                                  "waited": False, "already_running": True,
                                  "serial": "emulator-5554", "target_id": "emulator-5554",
                                  "port": 5554})
        self.assertEqual(self.emulator_calls(), [])

    def test_the_cli_reports_already_running(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]],
                       avd_names={"emulator-5554": "Pixel_9"})
        code, payload = self.cli("--avd", "Pixel_9", "--timeout", "20")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["already_running"], payload["serial"], payload["port"]),
                         (True, "emulator-5554", 5554))
        self.assertEqual(payload["platform"], "android")
        self.assertEqual(self.emulator_calls(), [])

    def test_another_running_avd_does_not_count(self) -> None:
        self.set_state(devices=[["emulator-5554", "device", ""]],
                       avd_names={"emulator-5554": "Pixel_10"},
                       boot_serials={"Pixel_9": "emulator-5562"})
        detail = self.boot()
        self.assertIs(detail["already_running"], False)
        self.assertEqual(detail["serial"], "emulator-5562")


@unittest.skipIf(fcntl is None, "flock is POSIX-only")
class BootLockTests(BootCase):
    def hold_lock(self) -> int:
        descriptor = os.open(emulator_mod.boot_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def test_the_lock_lives_in_the_private_state_base(self) -> None:
        path = emulator_mod.boot_lock_path()
        self.assertEqual(path, self.home / "locks" / "emulator-boot.lock")
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_a_held_lock_times_out_as_boot_timeout(self) -> None:
        self.hold_lock()
        with self.assertRaises(errors.AutonomError) as caught:
            self.boot(timeout=0.5)
        self.assertEqual(caught.exception.code, errors.BOOT_TIMEOUT)
        self.assertEqual(caught.exception.extra.get("waiting_for"), "boot_lock")
        self.assertEqual(self.emulator_calls(), [])

    def test_the_context_manager_times_out_and_releases(self) -> None:
        with emulator_mod.boot_lock(1):
            pass
        descriptor = self.hold_lock()
        with self.assertRaises(errors.AutonomError) as caught:
            with emulator_mod.boot_lock(0.2):
                self.fail("the lock was taken twice")
        self.assertEqual(caught.exception.extra.get("waiting_for"), "boot_lock")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        with emulator_mod.boot_lock(1):
            pass

    def test_the_lock_is_free_again_after_a_boot(self) -> None:
        [port] = free_console_ports(1)
        self.boot(port=port, timeout=20)
        with emulator_mod.boot_lock(0.5):
            pass
        self.set_state(devices=[], boot_hang=True)
        with self.assertRaises(errors.AutonomError):
            self.boot(timeout=2)
        with emulator_mod.boot_lock(0.5):  # released after a failed boot too
            pass


if __name__ == "__main__":
    unittest.main()
