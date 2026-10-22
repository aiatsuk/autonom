"""macOS launch policy and lifecycle without touching the host launchd."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import emulator, emulator_launch as launch, errors

class InteractiveLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patcher in (
            mock.patch.object(launch.sys, "platform", "darwin"),
            mock.patch.dict(os.environ, {"AUTONOM_EMULATOR_LAUNCH_MODE": "auto",
                                         "ANDROID_AVD_HOME": "/tmp/alternate-avds"}),
            mock.patch.object(launch.processes, "registry_dir", return_value=self.root),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.job = None

    def run_launchctl(self, argv, **kwargs):
        if argv[1] == "bootstrap":
            path = Path(argv[-1])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.job = plistlib.loads(path.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "4321\n", "")

    def test_mac_is_interactive_single_run_and_preserves_launch_identity(self):
        argv = ["/sdk/emulator", "-avd", "Example_AVD", "-port", "5560",
                "-http-proxy", "http://localhost:8080"]
        with mock.patch.object(launch.subprocess, "run", side_effect=self.run_launchctl), \
             mock.patch.object(launch.subprocess, "Popen") as popen:
            child = launch.start(argv)
        popen.assert_not_called()
        self.assertEqual(child.pid, 4321)
        self.assertEqual(self.job["ProcessType"], "Interactive")
        self.assertFalse(self.job["RunAtLoad"])
        self.assertFalse(self.job["KeepAlive"])
        self.assertTrue(self.job["LaunchOnlyOnce"])
        self.assertEqual(self.job["ProgramArguments"], argv + [
            "-no-window", "-no-snapshot", "-gpu", "host"])
        self.assertEqual(self.job["EnvironmentVariables"]["ANDROID_AVD_HOME"],
                         "/tmp/alternate-avds")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failure_cleans_private_plist_and_job_without_direct_fallback(self):
        for stage in ("bootstrap", "kickstart"):
            with self.subTest(stage=stage):
                def run(argv, **kwargs):
                    if argv[1] == stage:
                        return subprocess.CompletedProcess(argv, 5, "", "secret-value")
                    return self.run_launchctl(argv, **kwargs)
                with mock.patch.object(launch.subprocess, "run", side_effect=run) as call, \
                     mock.patch.object(launch.subprocess, "Popen") as popen:
                    with self.assertRaises(errors.AutonomError) as ctx:
                        launch.start(["/sdk/emulator", "-avd", "Example"])
                self.assertEqual(ctx.exception.code, errors.BACKEND_FAILED)
                self.assertNotIn("secret-value", str(ctx.exception))
                self.assertEqual(call.call_args.args[0][1], "bootout")
                popen.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_kickstart_timeout_removes_job(self):
        def run(argv, **kwargs):
            if argv[1] == "kickstart":
                raise subprocess.TimeoutExpired(argv, 10)
            return self.run_launchctl(argv, **kwargs)
        with mock.patch.object(launch.subprocess, "run", side_effect=run) as call:
            with self.assertRaises(errors.AutonomError):
                launch.start(["/sdk/emulator"])
        self.assertEqual(call.call_args.args[0][1], "bootout")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_poll_reaps_exited_job_and_keeps_exit_code(self):
        service = f"gui/{os.getuid()}/com.autonom.emulator." + "a" * 32
        child = launch.LaunchdChild(4321, service)
        with mock.patch.object(launch.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, " pid = 4321\n", ""),
            subprocess.CompletedProcess([], 0, " last exit code = 7\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]) as run:
            self.assertIsNone(child.poll())
            self.assertEqual(child.wait(timeout=1), 7)
            self.assertEqual(child.poll(), 7)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[0], [launch.LAUNCHCTL, "bootout", service])

    def test_only_our_current_user_service_can_be_removed(self):
        with mock.patch.object(launch.subprocess, "run") as run:
            launch.remove_service("gui/123/com.apple.finder")
            launch.remove_service(f"gui/{os.getuid()+1}/com.autonom.emulator." + "a" * 32)
        run.assert_not_called()

    def test_linux_and_explicit_direct_mode_keep_popen_arguments(self):
        for platform, mode in (("linux", "auto"), ("darwin", "direct")):
            with self.subTest(platform=platform, mode=mode), \
                 mock.patch.object(launch.sys, "platform", platform), \
                 mock.patch.dict(os.environ, {"AUTONOM_EMULATOR_LAUNCH_MODE": mode}), \
                 mock.patch.object(launch.subprocess, "Popen") as popen, \
                 mock.patch.object(launch.subprocess, "run") as run:
                argv = ["/sdk/emulator", "-avd", "Example"]
                self.assertIs(launch.start(argv), popen.return_value)
                self.assertEqual(popen.call_args.args[0], argv)
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                run.assert_not_called()

    def test_shutdown_removes_only_the_matching_emulator_job(self):
        own = f"gui/{os.getuid()}/com.autonom.emulator." + "a" * 32
        other = f"gui/{os.getuid()}/com.autonom.emulator." + "b" * 32
        with mock.patch.object(emulator.processes, "entries", return_value=[
            {"kind": "emulator", "pid": 123, "serial": "emulator-5556", "launchd_service": own},
            {"kind": "emulator", "pid": 456, "serial": "emulator-5554", "launchd_service": other},
        ]), mock.patch.object(emulator.processes, "deregister") as deregister, \
             mock.patch.object(launch, "remove_service") as remove:
            self.assertEqual(emulator.forget_emulator("emulator-5556"), [123])
        remove.assert_called_once_with(own)
        deregister.assert_called_once_with(123)

    def test_conflicting_device_discards_launch_job_not_unrelated_pid_group(self):
        child = launch.LaunchdChild(4321, "job")
        with mock.patch.object(child, "close") as close, \
             mock.patch.object(child, "wait"), \
             mock.patch.object(emulator.processes, "terminate_group") as terminate, \
             mock.patch.object(emulator.processes, "deregister") as deregister:
            emulator._discard_child(child)
        close.assert_called_once()
        terminate.assert_not_called()
        deregister.assert_called_once_with(4321)

if __name__ == "__main__":
    unittest.main()
