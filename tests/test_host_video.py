"""Explicit Mac capture configuration; no real emulator/network changes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from autonom_lib import host_video, errors, emulator

class HostVideoConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / ".config/autonom/host-video.json"
        self.config.parent.mkdir(parents=True)
        self.token = self.home / ".emulator_console_auth_token"
        self.token.write_text("synthetic-test-token-only")
        for p in (patch.object(host_video.Path, "home", return_value=self.home),
                  patch.object(host_video.sys, "platform", "darwin")):
            p.start(); self.addCleanup(p.stop)

    def write(self, **extra):
        d = {"avd":"Example", "serial":"emulator-5556", "grpcPort":8556, **extra}
        self.config.write_text(json.dumps({"version":1,"devices":[d]}))

    def test_unconfigured_does_not_enable_listener(self):
        self.assertIsNone(host_video.configured_device("Example", None))
        self.write()
        self.assertIsNone(host_video.configured_device("Other", 5554))

    def test_explicit_avd_and_serial(self):
        self.write()
        self.assertEqual(host_video.configured_device("Example", None), {"port":5556,"grpcPort":8556})
        with self.assertRaises(errors.AutonomError):
            host_video.configured_device("Example", 5554)

    def test_invalid_port_and_no_console_token_dependency(self):
        self.write(grpcPort=5557)
        with self.assertRaises(errors.AutonomError):
            host_video.configured_device("Example", None)
        self.write()
        self.token.unlink()
        self.assertEqual(host_video.configured_device("Example", None), {"port":5556,"grpcPort":8556})

    def test_linux_unchanged(self):
        self.write()
        with patch.object(host_video.sys, "platform", "linux"):
            self.assertIsNone(host_video.configured_device("Example", None))

class HostVideoBootTest(unittest.TestCase):
    def test_ordinary_boot_passes_authenticated_api_flags_and_keeps_serial(self):
        child = Mock(pid=12345, service=None)
        with patch.object(emulator, "running_avds", return_value={}), \
             patch.object(emulator.host_video, "configured_device", return_value={"port":5556,"grpcPort":8556}), \
             patch.object(emulator.adb_mod, "list_devices", return_value=[]), \
             patch.object(emulator, "_launching_on", return_value=None), \
             patch.object(emulator, "_loopback_port_free", return_value=True), \
             patch.object(emulator.emulator_launch, "start", return_value=child) as start, \
             patch.object(emulator.processes, "register") as register:
            result = emulator._boot_locked("/sdk/emulator", "/sdk/adb", "Example",
                wait=False, timeout=30, http_proxy=None, port=None, release=lambda:None,
                started=0, deadline=30)
        self.assertEqual(start.call_args.args[0], ["/sdk/emulator","-avd","Example","-port","5556",
            "-grpc","8556","-grpc-use-token"])
        self.assertEqual(result["port"],5556)
        self.assertEqual(register.call_args.kwargs["serial"],"emulator-5556")
