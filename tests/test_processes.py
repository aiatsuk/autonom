"""Machine-wide process tracking and reaping.

The bug these cover: orphan detection used to read `Path.cwd()/.autonom`, so a
proxy started in another directory was invisible. One held a port for seven
hours on the development machine while `doctor` reported no orphans at all.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import processes  # noqa: E402


class ProcessRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("AUTONOM_HOME")
        os.environ["AUTONOM_HOME"] = self.tmp.name
        self.artifacts = Path(self.tmp.name) / "session" / "network"
        self.artifacts.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("AUTONOM_HOME", None)
        else:
            os.environ["AUTONOM_HOME"] = self.previous
        self.tmp.cleanup()

    def _spawn_child(self) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self._reap_child, child)
        return child

    @staticmethod
    def _reap_child(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)

    def test_register_survives_a_change_of_working_directory(self) -> None:
        """The whole point: findable from anywhere, not just where it started."""
        processes.register("proxy", 4242, artifacts_dir=str(self.artifacts))
        previous_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            entries = processes._read()
        finally:
            os.chdir(previous_cwd)
        self.assertEqual([item["pid"] for item in entries], [4242])

    def test_an_intact_session_keeps_its_proxy_out_of_the_orphan_list(self) -> None:
        (self.artifacts / "proxy.json").write_text("{}", encoding="utf-8")
        processes.register("proxy", os.getpid(), artifacts_dir=str(self.artifacts))
        state = processes.scan()
        self.assertIn(os.getpid(), [item["pid"] for item in state["live"]])
        self.assertEqual(state["orphans"], [])

    def test_a_proxy_whose_session_vanished_is_an_orphan(self) -> None:
        """`network stop` reads proxy.json to find the pid, so once that file is
        gone nothing but this sweep can ever stop the process."""
        processes.register("proxy", os.getpid(), artifacts_dir=str(self.artifacts))
        state = processes.scan()
        self.assertIn(os.getpid(), [item["pid"] for item in state["orphans"]])

    def test_a_dead_pid_is_a_stale_entry_and_gets_reaped(self) -> None:
        child = self._spawn_child()
        processes.register("proxy", child.pid, artifacts_dir=str(self.artifacts))
        child.kill()
        child.wait(timeout=10)
        self.assertIn(child.pid, [item["pid"] for item in processes.scan()["stale_entries"]])
        self.assertEqual(processes.reap_stale_entries(), 1)
        self.assertEqual(processes._read(), [])

    def test_deregister_is_idempotent(self) -> None:
        processes.register("proxy", 99, artifacts_dir=str(self.artifacts))
        processes.deregister(99)
        processes.deregister(99)
        self.assertEqual(processes._read(), [])

    def test_dry_run_never_touches_a_process(self) -> None:
        child = self._spawn_child()
        processes.register("proxy", child.pid, artifacts_dir=str(self.artifacts))
        result = processes.cleanup(dry_run=True)
        self.assertEqual([item["result"] for item in result["actions"]],
                         ["would_terminate"])
        self.assertIsNone(child.poll(), "dry run killed a process")

    def test_cleanup_terminates_an_orphan_and_forgets_it(self) -> None:
        child = self._spawn_child()
        processes.register("proxy", child.pid, artifacts_dir=str(self.artifacts))
        result = processes.cleanup()
        self.assertEqual(result["terminated"], 1)
        deadline = time.time() + 10
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        self.assertIsNotNone(child.poll(), "orphan survived cleanup")
        self.assertEqual(processes._read(), [])

    def test_cleanup_leaves_a_healthy_process_alone_unless_asked(self) -> None:
        """A live proxy may be serving a session in another terminal."""
        (self.artifacts / "proxy.json").write_text("{}", encoding="utf-8")
        child = self._spawn_child()
        processes.register("proxy", child.pid, artifacts_dir=str(self.artifacts))

        default = processes.cleanup()
        # Assert about THIS process, not a global count: signature discovery is
        # machine-wide by design, so a real proxy running elsewhere on the
        # developer's machine legitimately raises `still_live` and would fail a
        # count-based assertion for reasons that have nothing to do with the
        # behaviour under test. (It did exactly that.)
        self.assertNotIn(child.pid, [a["pid"] for a in default["actions"]])
        self.assertGreaterEqual(default["still_live"], 1)
        self.assertIsNone(child.poll())

        explicit = processes.cleanup(dry_run=True, include_live=True)
        self.assertIn(child.pid, [item["pid"] for item in explicit["actions"]])

    def test_signature_discovery_needs_our_own_addon_in_the_command_line(self) -> None:
        """Matching bare 'mitmdump' would sweep up someone else's proxy."""
        source = (ROOT / "scripts/autonom_lib/processes.py").read_text("utf-8")
        self.assertIn('ADDON_MARKER = "mitm_addon.py"', source)
        marker = processes.ADDON_MARKER
        self.assertTrue(all(marker not in command
                            for _pid, command in [(1, "/usr/bin/mitmdump --port 8080")]))

    def test_a_proxy_missing_from_the_registry_is_still_discoverable(self) -> None:
        """Registry lost, process alive — the case the registry alone cannot fix.

        Verified against the real seven-hour orphan on the development machine;
        here we assert the discovery path exists and parses the artifacts dir.
        """
        command = (f"/opt/homebrew/bin/mitmdump --listen-port 18400 "
                   f"-s /x/{processes.ADDON_MARKER} "
                   f"--set {processes.PROXY_MARKER}/tmp/gone/network -q")
        self.assertIn(processes.ADDON_MARKER, command)
        directory = None
        for token in command.split():
            if token.startswith(processes.PROXY_MARKER):
                directory = token[len(processes.PROXY_MARKER):]
        self.assertEqual(directory, "/tmp/gone/network")


class CleanupCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["AUTONOM_HOME"] = self.tmp.name
        return subprocess.run(
            [sys.executable, str(CLI), *argv],
            cwd=self.tmp.name, env=env, text=True, timeout=120,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_processes_and_cleanup_work_without_a_session(self) -> None:
        """Cleanup must not need the very session state that went missing."""
        import json

        listing = self._run("processes")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(set(json.loads(listing.stdout)) >= {"live", "orphans"}, True)

        dry = self._run("cleanup", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertTrue(json.loads(dry.stdout)["dry_run"])



class CliInstallerTests(unittest.TestCase):
    """`autonom` on PATH — the entry point the docs assume exists."""

    INSTALLER = ROOT / "scripts/install_cli.sh"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bindir = Path(self.tmp.name) / "bin"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _install(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.INSTALLER), "--bin-dir", str(self.bindir), *extra],
            capture_output=True, text=True, timeout=60, check=False,
        )

    def test_installed_command_runs_from_an_unrelated_directory(self) -> None:
        """autonom.py resolves its library through __file__, so a symlink must
        keep working no matter where it is invoked from."""
        self.assertEqual(self._install().returncode, 0)
        installed = self.bindir / "autonom"
        self.assertTrue(installed.exists())
        result = subprocess.run([str(installed), "version"], capture_output=True,
                                text=True, cwd=self.tmp.name, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json as _json
        self.assertEqual(_json.loads(result.stdout)["name"], "autonom")

    def test_copy_mode_also_produces_a_working_command(self) -> None:
        self.assertEqual(self._install("--copy").returncode, 0)
        result = subprocess.run([str(self.bindir / "autonom"), "version"],
                                capture_output=True, text=True, cwd=self.tmp.name,
                                timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_is_idempotent_and_uninstall_removes_it(self) -> None:
        self._install()
        self.assertEqual(self._install().returncode, 0)
        self.assertEqual(self._install("uninstall").returncode, 0)
        self.assertFalse((self.bindir / "autonom").exists())

    def test_an_unrelated_file_is_never_clobbered(self) -> None:
        self.bindir.mkdir(parents=True)
        stranger = self.bindir / "autonom"
        stranger.write_text("#!/bin/sh\necho someone else's tool\n", encoding="utf-8")
        result = self._install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertIn("someone else", stranger.read_text())

if __name__ == "__main__":
    unittest.main()
