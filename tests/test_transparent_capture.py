"""Transparent Android MITM capture without modifying the app.

Proven live on WoolBox (a Flutter/Dio app): the emulator is LAUNCHED routed
through the proxy (`-http-proxy`, a launch-time flag), and the MITM CA is added
to the SYSTEM trust store via root — reversibly (tmpfs + a bind into each zygote
mount namespace), API-aware (APEX conscrypt on API>=34). This exercises those
mechanics against fakes and stubs; nothing here touches a real device.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_EMULATOR = ROOT / "tests/fakes/fake_emulator.py"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import consent, emulator as emulator_mod, errors, processes  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib.network import (  # noqa: E402
    attachment as attachment_mod, device_proxy_android, mitm_addon, mocks as mocks_mod,
    proxy as proxy_mod, store as store_mod,
)
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


_REAL_STDIN = None


def setUpModule() -> None:
    """Keep the consent gate away from the terminal, as `test_network` does.

    `attach`/`attach_transparent` pass through `consent.require`, which prompts
    on the confirmation phrase whenever stdin is a TTY. `tty_guard.py` runs the
    suite with a stdin that claims to be a TTY and raises on read, so a
    non-TTY stdin here keeps the interactive branch out of these tests (the
    branch itself is covered explicitly in `test_network`)."""
    global _REAL_STDIN
    _REAL_STDIN = sys.stdin
    sys.stdin = io.StringIO()


def tearDownModule() -> None:
    if _REAL_STDIN is not None:
        sys.stdin = _REAL_STDIN


def _write_self_signed(path: Path) -> None:
    """A real self-signed PEM so `openssl -subject_hash_old` has something to
    read; the private key is written beside it and never used."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = path.with_suffix(".key")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(path), "-days", "1",
         "-subj", "/CN=mitmproxy/O=mitmproxy"],
        capture_output=True, check=True, timeout=30,
    )


class _AdbStub:
    """A stand-in for ``adb.run_adb`` that records argv and answers the few
    reads the CA path makes, so no fake_adb edits and no real device are
    needed. `uid`, `api`, `root_out` and `ls_rc` shape the device's answers."""

    def __init__(self, *, uid: str = "0", api: str = "37",
                 root_out: str = "restarting adbd as root", ls_rc: int = 0,
                 qemu: str = "1") -> None:
        self.calls: list[list[str]] = []
        self.settings: dict[str, str] = {}
        self.uid = uid
        self.api = api
        self.root_out = root_out
        self.ls_rc = ls_rc
        self.qemu = qemu
        self.install_script: str | None = None
        self.verify_script: str | None = None

    def __call__(self, adb, args, *, serial=None, timeout=None,
                 check=False, binary=False):
        args = list(args)
        self.calls.append(args)
        rc, out = 0, ""
        if args[:1] == ["root"]:
            out = self.root_out
        elif args[0] == "shell":
            rest = args[1:]
            if rest == ["id", "-u"]:
                out = self.uid
            elif rest[:1] == ["getprop"] and rest[1:2] == ["ro.build.version.sdk"]:
                out = self.api
            elif rest[:1] == ["getprop"] and rest[1:2] == ["ro.kernel.qemu"]:
                out = self.qemu
            elif rest[:1] == ["settings"] and len(rest) >= 4:
                action, key = rest[1], rest[3]
                if action == "get":
                    out = self.settings.get(key, "null")
                elif action == "put":
                    self.settings[key] = rest[4]
                elif action == "delete":
                    self.settings.pop(key, None)
            else:
                script = rest[0] if rest else ""
                if "mount -t tmpfs" in script:
                    self.install_script = script
                elif "nsenter" in script or script.startswith("ls "):
                    self.verify_script = script
                    rc = self.ls_rc
        return subprocess.CompletedProcess(args=["adb", *args], returncode=rc, stdout=out)

    def flat(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


class TransparentBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.set_env(AUTONOM_HOME=str(self.home))
        # A CA the machine store exposes, so ca_certificate() finds one.
        _write_self_signed(self.home / "ca" / "mitmproxy-ca-cert.pem")
        self.target = Target("android", "emulator-5556", str(FAKE_ADB),
                             {"serial": "emulator-5556"})

    def _record(self) -> dict:
        artifacts = self.home / "sessions" / "s_test"
        (artifacts / "network").mkdir(parents=True, exist_ok=True)
        return {"session_id": "s_test", "platform": "android",
                "target_id": "emulator-5556", "artifacts_dir": str(artifacts),
                "network": {}}

    def _stub_adb(self, **kwargs) -> _AdbStub:
        stub = _AdbStub(**kwargs)
        original = device_proxy_android.adb_mod.run_adb
        device_proxy_android.adb_mod.run_adb = stub  # type: ignore[assignment]
        self.addCleanup(setattr, device_proxy_android.adb_mod, "run_adb", original)
        return stub


class BootHttpProxyTests(EnvSandboxMixin, unittest.TestCase):
    """`devices boot --http-proxy` is a launch-time route, recorded by serial."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.state.write_text(json.dumps(
            {"avds": ["Pixel_9"], "devices": [["emulator-5554", "device", ""]]}),
            encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_HOME=str(root / "home"))

    def _emulator_argv(self) -> list[list[str]]:
        rows = [json.loads(line)
                for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [row["argv"] for row in rows
                if row["tool"] == "emulator" and row["argv"][:1] == ["-avd"]]

    def test_boot_passes_http_proxy_url_at_launch(self) -> None:
        detail = emulator_mod.boot_avd(
            str(FAKE_EMULATOR), str(FAKE_ADB), "Pixel_9", timeout=15,
            http_proxy="127.0.0.1:8080",
        )
        self.assertTrue(detail["booted"])
        argv = self._emulator_argv()[0]
        self.assertEqual(argv[:2], ["-avd", "Pixel_9"])
        self.assertIn("-http-proxy", argv)
        self.assertEqual(argv[argv.index("-http-proxy") + 1], "http://127.0.0.1:8080")
        self.assertEqual(detail["http_proxy"], "127.0.0.1:8080")

    def test_boot_records_routing_by_serial(self) -> None:
        detail = emulator_mod.boot_avd(
            str(FAKE_EMULATOR), str(FAKE_ADB), "Pixel_9", timeout=15,
            http_proxy="127.0.0.1:8080",
        )
        self.assertEqual(emulator_mod.proxy_routing(detail["serial"]), "127.0.0.1:8080")

    def test_full_url_is_accepted_and_reduced(self) -> None:
        detail = emulator_mod.boot_avd(
            str(FAKE_EMULATOR), str(FAKE_ADB), "Pixel_9", timeout=15,
            http_proxy="http://127.0.0.1:9090",
        )
        argv = self._emulator_argv()[0]
        self.assertEqual(argv[argv.index("-http-proxy") + 1], "http://127.0.0.1:9090")
        self.assertEqual(emulator_mod.proxy_routing(detail["serial"]), "127.0.0.1:9090")

    def test_boot_without_proxy_records_no_routing(self) -> None:
        detail = emulator_mod.boot_avd(
            str(FAKE_EMULATOR), str(FAKE_ADB), "Pixel_9", timeout=15)
        argv = self._emulator_argv()[0]
        self.assertNotIn("-http-proxy", argv)
        self.assertNotIn("http_proxy", detail)
        self.assertIsNone(emulator_mod.proxy_routing(detail["serial"]))


class InstallSystemCaTests(TransparentBase):
    def test_api37_uses_the_apex_store_and_the_zygote_bind(self) -> None:
        stub = self._stub_adb(api="37")
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        detail = device_proxy_android.install_system_ca(self.target, cert)
        self.assertEqual(detail["method"], "apex_conscrypt")
        self.assertEqual(detail["api"], 37)
        self.assertTrue(detail["reversible"])
        self.assertRegex(detail["installed"],
                         r"^/system/etc/security/cacerts/[0-9a-f]+\.0$")
        script = stub.install_script or ""
        self.assertIn("cp /apex/com.android.conscrypt/cacerts/*", script)
        self.assertIn("mount -t tmpfs tmpfs /system/etc/security/cacerts", script)
        self.assertIn("chcon u:object_r:system_file:s0", script)
        self.assertIn("nsenter --mount=/proc/$Z/ns/mnt -- mount --bind", script)
        self.assertIn("/apex/com.android.conscrypt/cacerts", script)

    def test_root_precedes_push(self) -> None:
        stub = self._stub_adb(api="37")
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        device_proxy_android.install_system_ca(self.target, cert)
        flat = stub.flat()
        self.assertLess(next(i for i, a in enumerate(flat) if a == "root"),
                        next(i for i, a in enumerate(flat) if a.startswith("push ")),
                        "adb root must precede the cert push")

    def test_api30_uses_the_system_store_without_the_apex_bind(self) -> None:
        stub = self._stub_adb(api="30")
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        detail = device_proxy_android.install_system_ca(self.target, cert)
        self.assertEqual(detail["method"], "system_cacerts")
        self.assertEqual(detail["api"], 30)
        script = stub.install_script or ""
        self.assertIn("cp /system/etc/security/cacerts/*", script)
        self.assertNotIn("nsenter", script)

    def test_non_rootable_image_is_refused_with_a_capability(self) -> None:
        self._stub_adb(uid="2000", root_out="adbd cannot run as root in production builds")
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.install_system_ca(self.target, cert)
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(caught.exception.extra["capability"], "network.system_ca")
        self.assertEqual(caught.exception.extra["reason"], "adb_root_refused")
        self.assertIn("google_apis", caught.exception.hint)

    def test_physical_device_is_refused(self) -> None:
        self._stub_adb(qemu="0")
        physical = Target("android", "R58M123", str(FAKE_ADB), {"serial": "R58M123"})
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.install_system_ca(physical, cert)
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(caught.exception.extra["capability"], "network.system_ca")


class SystemCaVerifyTests(TransparentBase):
    def test_verify_uses_the_zygote_namespace_on_api37(self) -> None:
        stub = self._stub_adb(api="37", ls_rc=0)
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        detail = device_proxy_android.system_ca_installed(self.target, cert)
        self.assertTrue(detail["present"])
        self.assertEqual(detail["checked_via"], "zygote_namespace")
        self.assertIn("nsenter --mount=/proc/$Z/ns/mnt -- ls", stub.verify_script or "")

    def test_verify_reports_absent_when_the_zygote_namespace_lacks_it(self) -> None:
        self._stub_adb(api="37", ls_rc=1)
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        detail = device_proxy_android.system_ca_installed(self.target, cert)
        self.assertFalse(detail["present"])

    def test_verify_reads_the_system_store_below_api34(self) -> None:
        stub = self._stub_adb(api="30", ls_rc=0)
        cert = proxy_mod.ca_store() / "mitmproxy-ca-cert.pem"
        detail = device_proxy_android.system_ca_installed(self.target, cert)
        self.assertEqual(detail["checked_via"], "system_store")
        self.assertNotIn("nsenter", stub.verify_script or "")


class AttachTransparentTests(TransparentBase):
    def _route(self, hostport: str = "127.0.0.1:8080") -> None:
        processes.register("emulator", 424242, owner=processes.HARNESS_OWNER,
                           serial="emulator-5556", http_proxy=hostport)

    def test_refuses_when_the_emulator_was_not_booted_routed(self) -> None:
        stub = self._stub_adb()
        record = self._record()
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.attach_transparent(
                self.target, record, port=8080, acknowledged=True)
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(caught.exception.extra["capability"], "network.transparent_capture")
        self.assertIn("devices boot", caught.exception.hint)
        self.assertIn("--http-proxy", caught.exception.hint)
        self.assertEqual(stub.calls, [], "nothing may touch the device before it is routed")

    def _app_proxy_attached(self, *, labelled: bool = True) -> dict:
        """A session where the app-proxy attach wrote 10.0.2.2:<port> and saved
        the operator's corporate proxy for detach to put back."""
        record = self._record()
        record["network"] = {"attached": True, "device_proxy": "10.0.2.2:8080",
                             "previous_http_proxy": "proxy.corp:3128"}
        if labelled:
            record["network"]["capture_mode"] = "app_proxy"
        return record

    def test_refuses_on_top_of_an_active_app_proxy_attach(self) -> None:
        stub = self._stub_adb()
        self._route()
        record = self._app_proxy_attached()
        before = json.loads(json.dumps(record))
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.attach_transparent(
                self.target, record, port=8080, acknowledged=True)
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(caught.exception.extra["capability"], "network.transparent_capture")
        self.assertEqual(caught.exception.extra["reason"], "app_proxy_attached")
        self.assertIn("network detach", caught.exception.hint)
        # nothing ran, and the value detach must restore is still on record
        self.assertEqual(stub.calls, [])
        self.assertEqual(record, before)
        self.assertEqual(record["network"]["previous_http_proxy"], "proxy.corp:3128")

    def test_refuses_a_legacy_attach_recorded_before_capture_mode(self) -> None:
        self._stub_adb()
        self._route()
        record = self._app_proxy_attached(labelled=False)
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.attach_transparent(
                self.target, record, port=8080, acknowledged=True)
        self.assertEqual(caught.exception.extra["reason"], "app_proxy_attached")

    def test_detach_then_system_ca_restores_the_proxy_and_proceeds(self) -> None:
        stub = self._stub_adb(api="37")
        self._route()
        record = self._app_proxy_attached()
        stub.settings["http_proxy"] = "10.0.2.2:8080"
        with mock.patch.object(device_proxy_android.time, "sleep"):
            device_proxy_android.detach(self.target, record)
        self.assertEqual(stub.settings["http_proxy"], "proxy.corp:3128")
        self.assertEqual(stub.settings["global_http_proxy_host"], "proxy.corp")
        # detach wrote back exactly the value the app-proxy attach had saved
        self.assertIn("shell settings put global http_proxy proxy.corp:3128", stub.flat())
        detail = device_proxy_android.attach_transparent(
            self.target, record, port=8080, acknowledged=True)
        self.assertEqual(detail["capture_mode"], "transparent")

    def test_refuses_when_routed_to_a_different_port(self) -> None:
        self._stub_adb()
        self._route("127.0.0.1:9999")
        record = self._record()
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.attach_transparent(
                self.target, record, port=8080, acknowledged=True)
        self.assertEqual(caught.exception.extra["observed"], "127.0.0.1:9999")
        self.assertEqual(caught.exception.extra["expected"], "127.0.0.1:8080")

    def test_without_consent_nothing_is_installed(self) -> None:
        stub = self._stub_adb()
        self._route()
        record = self._record()
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.attach_transparent(
                self.target, record, port=8080, acknowledged=False)
        self.assertEqual(caught.exception.code, errors.CONSENT_REQUIRED)
        self.assertEqual(stub.calls, [], "consent is refused before any device command")

    def test_happy_path_installs_verifies_and_marks_transparent(self) -> None:
        stub = self._stub_adb(api="37", ls_rc=0)
        self._route()
        record = self._record()
        detail = device_proxy_android.attach_transparent(
            self.target, record, port=8080, acknowledged=True)
        self.assertEqual(detail["capture_mode"], "transparent")
        self.assertEqual(detail["http_proxy_routed"], "127.0.0.1:8080")
        self.assertEqual(detail["system_ca"]["method"], "apex_conscrypt")
        # the install is verified from the zygote namespace and reported
        self.assertTrue(detail["system_ca"]["verified"])
        self.assertEqual(detail["system_ca"]["checked_via"], "zygote_namespace")
        self.assertNotIn("warnings", detail)
        self.assertEqual(record["network"]["capture_mode"], "transparent")
        self.assertTrue(record["network"]["attached"])
        self.assertEqual(record["consent_log"][-1]["operation"], "ca_install")
        # the proven install ran on the device, then the verify
        self.assertTrue(any("mount -t tmpfs" in a for a in stub.flat()))
        self.assertIn("nsenter --mount=/proc/$Z/ns/mnt -- ls", stub.verify_script or "")

    def test_failed_verify_is_a_warning_not_a_false_success(self) -> None:
        self._stub_adb(api="37", ls_rc=1)  # the zygote namespace does not see it
        self._route()
        record = self._record()
        detail = device_proxy_android.attach_transparent(
            self.target, record, port=8080, acknowledged=True)
        # honest, not fatal: attach stands, but the CA is flagged unverified
        self.assertTrue(detail["attached"])
        self.assertFalse(detail["system_ca"]["verified"])
        self.assertEqual([w["code"] for w in detail["warnings"]], ["system_ca_unverified"])
        self.assertFalse(record["network"]["system_ca"]["verified"])

    def test_detach_transparent_restores_nothing(self) -> None:
        self._stub_adb(api="37")
        self._route()
        record = self._record()
        device_proxy_android.attach_transparent(
            self.target, record, port=8080, acknowledged=True)
        detail = device_proxy_android.detach(self.target, record)
        self.assertTrue(detail["was_attached"])
        self.assertEqual(detail["capture_mode"], "transparent")
        self.assertTrue(detail["system_ca_persists"])
        self.assertFalse(record["network"]["attached"])
        # no proxy setting was ever written, so none was restored
        self.assertNotIn("wrote", detail)


class AppProxyStillLabelledTests(EnvSandboxMixin, unittest.TestCase):
    """The fallback path is unchanged but now names itself `app_proxy`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"
        self.state.write_text(json.dumps({"settings": {}}), encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state))
        self.target = Target("android", "emulator-5554", str(FAKE_ADB),
                             {"serial": "emulator-5554"})

    def test_app_proxy_attach_labels_the_capture_mode(self) -> None:
        artifacts = Path(self.tmp.name) / ".autonom" / "s_test"
        (artifacts / "network").mkdir(parents=True, exist_ok=True)
        record = {"session_id": "s_test", "platform": "android",
                  "target_id": "emulator-5554", "artifacts_dir": str(artifacts),
                  "network": {}}
        device_proxy_android.attach(self.target, record, port=8080,
                                    acknowledged=True, network_cycle=False)
        self.assertEqual(record["network"]["capture_mode"], "app_proxy")


class NetworkStatusCaptureModeTests(EnvSandboxMixin, unittest.TestCase):
    """`network status` surfaces `capture_mode` and the installed system CA."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.set_env(AUTONOM_HOME=str(Path(self.tmp.name) / "home"),
                     AUTONOM_FAKE_STATE=str(Path(self.tmp.name) / "adb-state.json"))
        Path(self.tmp.name, "adb-state.json").write_text("{}", encoding="utf-8")
        self.env = dict(os.environ)

    def _run(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, env=self.env, timeout=60,
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        return completed.returncode, json.loads(stream)

    def _transparent_session(self) -> None:
        record = session_mod.start_session(str(FAKE_ADB), serial="emulator-5554",
                                           app_id="com.example.app")
        record["network"] = {
            "attached": True, "capture_mode": "transparent",
            "http_proxy_routed": "127.0.0.1:8080",
            "system_ca": {"installed": "/system/etc/security/cacerts/abc.0",
                          "hash": "abc", "api": 37, "method": "apex_conscrypt",
                          "reversible": True},
        }
        session_mod.save(record)

    def test_status_reports_transparent_capture_mode(self) -> None:
        self._transparent_session()
        # what `devices boot --http-proxy` recorded; the CLI reads the same registry
        processes.register("emulator", 424244, owner=processes.HARNESS_OWNER,
                           serial="emulator-5554", http_proxy="127.0.0.1:8080")
        code, payload = self._run("network", "status", "--serial", "emulator-5554",
                                  "--adb", str(FAKE_ADB))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["capture_mode"], "transparent")
        self.assertEqual(payload["system_ca"]["method"], "apex_conscrypt")
        self.assertEqual(payload["http_proxy_routed"], "127.0.0.1:8080")
        # attachment is proven by the launch-time route, even with no traffic yet
        self.assertIs(payload["attached"], True)
        self.assertEqual(payload["evidence"], "transparent_proxy")

    def test_status_after_the_route_is_gone_is_not_attached(self) -> None:
        # the session was kept, but the emulator was shut down: no registry entry
        self._transparent_session()
        code, payload = self._run("network", "status", "--serial", "emulator-5554",
                                  "--adb", str(FAKE_ADB))
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["attached"], False)
        self.assertEqual(payload["evidence"], "transparent_route_gone")
        self.assertIn("no longer registered", payload["reason"])


class TransparentAttributionTests(EnvSandboxMixin, unittest.TestCase):
    """While the session's emulator is still registered as routed through the
    proxy, every flow on it is counted as the device's (loopback by design) and
    `network requests` lists them. Once the route is gone, nothing is assumed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.set_env(AUTONOM_HOME=str(Path(self.tmp.name) / "home"))
        self.artifacts = Path(self.tmp.name) / "s_test"
        (self.artifacts / "network").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _route(hostport: str = "127.0.0.1:18091") -> None:
        """What `devices boot --http-proxy` leaves in the process registry."""
        processes.register("emulator", 424243, owner=processes.HARNESS_OWNER,
                           serial="emulator-5554", http_proxy=hostport)

    def _record(self, capture_mode: str) -> dict:
        network = {"attached": True, "capture_mode": capture_mode,
                   "proxy_host": "127.0.0.1", "proxy_port": 18091}
        if capture_mode == "transparent":
            network["http_proxy_routed"] = "127.0.0.1:18091"
        else:
            network["device_proxy"] = "10.0.2.2:18091"
        return {"session_id": "s_test", "platform": "android",
                "target_id": "emulator-5554", "artifacts_dir": str(self.artifacts),
                "network": network}

    def _write_loopback_flows(self, count: int = 3) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines = []
        for i in range(count):
            lines.append(json.dumps({
                "id": f"f_{i:04d}", "host": "backend.woolbox.app",
                "client_ip": "127.0.0.1", "started_at": now, "method": "GET",
                "url": f"https://backend.woolbox.app/api/v1/item/{i}",
                "path": f"/api/v1/item/{i}", "status": 200,
            }))
        (self.artifacts / "network" / "flows.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def test_transparent_counts_loopback_flows_as_the_devices(self) -> None:
        self._route()
        self._write_loopback_flows(3)
        evidence = attachment_mod.attachment_evidence(
            self._record("transparent"), platform="android")
        self.assertIs(evidence["attached"], True)
        self.assertEqual(evidence["evidence"], "transparent_proxy")
        self.assertEqual(evidence["recent_flow_count"], 3)
        self.assertEqual(evidence["target_flow_count"], 3)
        self.assertEqual(evidence["unattributed_flow_count"], 0)

    def test_transparent_attached_true_even_with_no_traffic(self) -> None:
        self._route()
        evidence = attachment_mod.attachment_evidence(
            self._record("transparent"), platform="android")
        self.assertIs(evidence["attached"], True)
        self.assertEqual(evidence["evidence"], "transparent_proxy")
        self.assertEqual(evidence["recent_flow_count"], 0)

    def test_route_gone_is_not_attached_and_says_why(self) -> None:
        # `devices shutdown` dropped the registry entry, the session was kept,
        # and a host `curl` goes through the port: it must not count.
        self._write_loopback_flows(3)
        evidence = attachment_mod.attachment_evidence(
            self._record("transparent"), platform="android")
        self.assertIs(evidence["attached"], False)
        self.assertEqual(evidence["evidence"], "transparent_route_gone")
        self.assertEqual(evidence["reason"], attachment_mod.TRANSPARENT_ROUTE_GONE)
        self.assertEqual(evidence["target_flow_count"], 0)
        self.assertEqual(evidence["unattributed_flow_count"], 3)

    def test_a_reboot_routed_elsewhere_is_a_gone_route(self) -> None:
        self._route("127.0.0.1:9999")  # rebooted through another proxy
        evidence = attachment_mod.attachment_evidence(
            self._record("transparent"), platform="android")
        self.assertIs(evidence["attached"], False)
        self.assertEqual(evidence["evidence"], "transparent_route_gone")

    def test_a_gone_route_never_reads_the_device_setting(self) -> None:
        # Transparent mode wrote no device proxy, so reading one back would
        # misreport its absence as "cleared externally"; nor is it a device call.
        def observe() -> str | None:
            raise AssertionError("the device setting must not be read")

        evidence = attachment_mod.attachment_evidence(
            self._record("transparent"), platform="android", observe_setting=observe)
        self.assertEqual(evidence["evidence"], "transparent_route_gone")

    def test_requests_lists_loopback_flows_in_transparent_mode(self) -> None:
        self._write_loopback_flows(3)
        listing = store_mod.listing(self._record("transparent"))
        self.assertEqual(listing["count"], 3)
        self.assertEqual(listing["total_matched"], 3)

    def test_app_proxy_still_treats_loopback_as_unattributed(self) -> None:
        # The heuristic is only skipped for transparent mode; the fallback path
        # must keep treating loopback flows as not provably the device's.
        self._write_loopback_flows(3)
        evidence = attachment_mod.attachment_evidence(
            self._record("app_proxy"), platform="android")
        self.assertEqual(evidence["target_flow_count"], 0)
        self.assertEqual(evidence["unattributed_flow_count"], 3)
        self.assertNotEqual(evidence["evidence"], "transparent_proxy")


class ConnectIpHostTests(EnvSandboxMixin, unittest.TestCase):
    """Under `-http-proxy` every CONNECT names an IP, so `request.host` is the
    IP while the app asked for a name. Measured live: host `130.193.59.68`,
    url `https://backend.woolbox.app/api/v1/pattern`, Host header
    `backend.woolbox.app`. The record and host-based mocks must use the name."""

    NAME = "backend.woolbox.app"
    IP = "130.193.59.68"
    URL = "https://backend.woolbox.app/api/v1/pattern"

    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.artifacts = self.home / "s_test"
        self.network_dir = self.artifacts / "network"
        self.network_dir.mkdir(parents=True, exist_ok=True)

    def _flow(self, *, host: str | None = None, pretty_host: str | None = NAME):
        now = time.time()
        request = types.SimpleNamespace(
            method="GET", pretty_url=self.URL, host=host or self.IP,
            path="/api/v1/pattern", content=b"", timestamp_start=now,
            headers=types.SimpleNamespace(items=lambda: [("Host", self.NAME)]),
        )
        if pretty_host is not None:
            request.pretty_host = pretty_host
        response = types.SimpleNamespace(
            status_code=200, content=b"{}", timestamp_end=now,
            headers=types.SimpleNamespace(items=lambda: []),
        )
        return types.SimpleNamespace(
            request=request, response=response, metadata={},
            client_conn=types.SimpleNamespace(peername=("127.0.0.1", 50123)),
        )

    def _recorded(self, flow) -> dict:
        recorder = mitm_addon.AutonomRecorder()
        recorder.directory = str(self.network_dir)
        recorder.response(flow)
        lines = (self.network_dir / "flows.jsonl").read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])

    def test_recorded_host_is_the_requested_name_not_the_connect_ip(self) -> None:
        record = self._recorded(self._flow())
        self.assertEqual(record["host"], self.NAME)
        # the connection's real target is kept, additively
        self.assertEqual(record["server_ip"], self.IP)

    def test_requests_list_host_filter_matches_the_name(self) -> None:
        self._recorded(self._flow())
        session = {"artifacts_dir": str(self.artifacts)}
        self.assertEqual(store_mod.listing(session, host=self.NAME)["count"], 1)
        self.assertEqual(store_mod.listing(session, host=self.IP)["count"], 0)

    def test_host_mock_matches_a_connect_ip_flow(self) -> None:
        registry = self.home / "mocks"
        rule = mocks_mod.add(url_glob="*", host=self.NAME, status=299,
                             body_text='{"mocked":true}', registry=registry)
        recorder = mitm_addon.AutonomRecorder()
        recorder.mocks_path = str(mocks_mod.registry_file(registry))
        flow = self._flow()
        flow.response = None
        # the hook builds its response with mitmproxy, which is not installed here
        fake_http = types.SimpleNamespace(Response=types.SimpleNamespace(
            make=lambda status, body, headers: types.SimpleNamespace(
                status_code=status, content=body, headers=headers)))
        fake_mitmproxy = types.ModuleType("mitmproxy")
        fake_mitmproxy.http = fake_http  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"mitmproxy": fake_mitmproxy}):
            recorder.request(flow)
        self.assertIsNotNone(flow.response, "a --host mock must match the requested name")
        self.assertEqual(flow.response.status_code, 299)
        self.assertEqual(flow.metadata["autonom_mock_id"], rule["id"])

    def test_a_flow_whose_host_is_already_the_name_gets_no_server_ip(self) -> None:
        record = self._recorded(self._flow(host=self.NAME))
        self.assertEqual(record["host"], self.NAME)
        self.assertNotIn("server_ip", record)

    def test_a_flow_without_pretty_host_falls_back_to_request_host(self) -> None:
        flow = self._flow(host="api.example.net", pretty_host=None)
        self.assertEqual(mitm_addon.flow_host(flow), "api.example.net")


class CliWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("autonom_cli_tc", CLI)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.parser = module.build_parser()

    def test_devices_boot_accepts_http_proxy(self) -> None:
        args = self.parser.parse_args(
            ["devices", "boot", "--avd", "Pixel_9", "--http-proxy", "127.0.0.1:8080"])
        self.assertEqual(args.http_proxy, "127.0.0.1:8080")

    def test_network_attach_accepts_system_ca(self) -> None:
        args = self.parser.parse_args(["network", "attach", "--system-ca"])
        self.assertTrue(args.system_ca)


if __name__ == "__main__":
    unittest.main()
