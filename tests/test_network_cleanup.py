"""Proxy cleanup regressions. Fake Android state only; no real device or listener."""
from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import autonom as cli
from autonom_lib import errors, session
from autonom_lib.network import device_proxy_android as device
from autonom_lib.platform import Target
from env_isolation import EnvSandboxMixin


class ProxyCleanupTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self):
        self.home = self.sandbox_home()
        self.state = self.home / "fake.json"
        self.log = self.home / "calls.jsonl"
        self.state.write_text("{}")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log))
        self.target = Target("android", "emulator-5580", str(ROOT / "tests/fakes/fake_adb.py"), {})
        self.record = session.start_session(self.target.tool, serial=self.target.target_id,
                                            app_id="com.example.app")
        for patcher in (mock.patch.object(device.time, "sleep"),
                        mock.patch.object(device.consent, "require", return_value={}),
                        mock.patch.object(device.consent, "record")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self):
        return json.loads(self.state.read_text()).get("settings", {})

    def set_settings(self, **settings):
        self.state.write_text(json.dumps({"settings": settings}))

    def attach(self, previous=None):
        self.set_settings(**({"http_proxy": previous} if previous else {}))
        device.attach(self.target, self.record, port=8899, acknowledged=True)
        session.save(self.record)

    def stop(self):
        return cli.network_stop_payload(self.record, lambda: self.target)

    def test_stop_restores_and_verifies_before_terminating_proxy(self):
        self.attach("proxy.corp:3128")
        def terminate(record):
            self.assertEqual(self.settings()["http_proxy"], "proxy.corp:3128")
            self.assertFalse(session.load_current()["network"]["attached"])
            calls = [json.loads(x)["argv"] for x in self.log.read_text().splitlines()]
            self.assertTrue(any(x[-3:] == ["svc", "wifi", "enable"] for x in calls))
            return {"was_running": True}
        with mock.patch.object(cli.proxy_mod, "stop", side_effect=terminate):
            result = self.stop()
        self.assertTrue(result["detach"]["setting_applied"]["applied"])
        self.assertFalse(session.load_current()["network"]["enabled"])

    def test_cli_stop_uses_the_same_cleanup(self):
        self.attach()
        with mock.patch.object(cli, "_target", return_value=self.target), \
                mock.patch.object(cli.proxy_mod, "stop", return_value={"was_running": True}), \
                mock.patch.object(cli, "emit", return_value=0):
            cli.cmd_network_stop(argparse.Namespace())
        self.assertEqual(self.settings()["http_proxy"], ":0")
        self.assertFalse(session.load_current()["network"]["attached"])

    def test_canvas_stop_uses_its_bound_target(self):
        import autonom_canvas_tools as tools
        self.attach()
        with mock.patch.object(cli, "_target", side_effect=AssertionError("global target")), \
                mock.patch.object(cli.proxy_mod, "stop", return_value={"was_running": True}):
            result, _, _ = tools.Tools(self.target).network_stop({}, self.record)
        self.assertTrue(result["detach"]["was_attached"])
        self.assertEqual(self.settings()["http_proxy"], ":0")

    def test_failed_restore_keeps_proxy_and_snapshot(self):
        self.attach("proxy.corp:3128")
        with mock.patch.object(device, "_put_setting", side_effect=errors.AutonomError(
                errors.BACKEND_FAILED, "write failed")), \
                mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertTrue(session.load_current()["network"]["attached"])
        self.assertEqual(self.record["network"]["previous_http_proxy"], "proxy.corp:3128")

    def test_failed_refresh_can_retry_after_setting_was_written(self):
        self.attach()
        with mock.patch.object(device, "apply_proxy_setting", return_value={"applied": False}), \
                mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertEqual(self.settings()["http_proxy"], ":0")
        self.assertTrue(session.load_current()["network"]["attached"])
        with mock.patch.object(cli.proxy_mod, "stop", return_value={"was_running": True}):
            self.stop()
        self.assertFalse(session.load_current()["network"]["attached"])

    def test_failed_readback_keeps_proxy(self):
        self.attach()
        with mock.patch.object(device, "_get_setting", return_value="10.0.2.2:8899"), \
                mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertTrue(self.record["network"]["attached"])

    def test_stop_failure_does_not_reactivate_detached_proxy(self):
        self.attach()
        with mock.patch.object(cli.proxy_mod, "stop", side_effect=errors.AutonomError(
                errors.BACKEND_FAILED, "stop failed")):
            with self.assertRaises(errors.AutonomError):
                self.stop()
        self.assertFalse(session.load_current()["network"]["attached"])
        self.assertTrue(session.load_current()["network"]["enabled"])

    def test_repeated_attach_preserves_first_snapshot_even_when_port_changes(self):
        self.attach("proxy.corp:3128")
        device.attach(self.target, self.record, port=9900, acknowledged=True)
        self.assertEqual(self.record["network"]["previous_http_proxy"], "proxy.corp:3128")
        device.detach(self.target, self.record)
        self.assertEqual(self.settings()["http_proxy"], "proxy.corp:3128")

    def test_modern_settings_are_restored_with_legacy_setting(self):
        original = {"http_proxy": "proxy.corp:3128", "global_http_proxy_host": "proxy.corp",
                    "global_http_proxy_port": "3128", "global_http_proxy_exclusion_list": "localhost",
                    "global_proxy_pac_url": "https://proxy.corp/settings.pac"}
        self.set_settings(**original)
        device.attach(self.target, self.record, port=8899, acknowledged=True)
        self.set_settings(http_proxy="10.0.2.2:8899", global_http_proxy_host="10.0.2.2",
                          global_http_proxy_port="8899")
        device.detach(self.target, self.record)
        self.assertEqual(self.settings(), original)

    def test_legacy_session_clears_stale_canonical_proxy_on_direct_restore(self):
        self.attach()
        self.record["network"].pop("previous_proxy_settings")
        self.set_settings(http_proxy="10.0.2.2:8899", global_http_proxy_host="10.0.2.2",
                          global_http_proxy_port="8899")
        device.detach(self.target, self.record)
        self.assertEqual(self.settings(), {"http_proxy": ":0"})

    def test_external_proxy_change_is_not_overwritten(self):
        self.attach()
        self.set_settings(http_proxy="other.corp:8080")
        with mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertEqual(self.settings()["http_proxy"], "other.corp:8080")

    def test_external_pac_change_is_not_overwritten(self):
        self.attach()
        self.set_settings(http_proxy="10.0.2.2:8899",
                          global_proxy_pac_url="https://other.corp/settings.pac")
        with mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertEqual(self.settings()["global_proxy_pac_url"], "https://other.corp/settings.pac")

    def test_legacy_session_restores_canonical_foreign_proxy(self):
        self.attach("proxy.corp:3128")
        self.record["network"].pop("previous_proxy_settings")
        self.set_settings(http_proxy="10.0.2.2:8899", global_http_proxy_host="10.0.2.2",
                          global_http_proxy_port="8899")
        device.detach(self.target, self.record)
        self.assertEqual(self.settings(), {"http_proxy": "proxy.corp:3128",
                        "global_http_proxy_host": "proxy.corp", "global_http_proxy_port": "3128"})

    def test_repeated_attach_refuses_external_proxy_change(self):
        self.attach()
        self.set_settings(http_proxy="other.corp:8080")
        with self.assertRaises(errors.AutonomError):
            device.attach(self.target, self.record, port=8899, acknowledged=True)
        self.assertEqual(self.settings()["http_proxy"], "other.corp:8080")
        self.assertIsNone(self.record["network"]["previous_http_proxy"])

    def test_unknown_original_setting_is_not_replaced_with_same_proxy(self):
        self.set_settings(http_proxy="10.0.2.2:8899")
        with self.assertRaises(errors.AutonomError):
            device.attach(self.target, self.record, port=8899, acknowledged=True)
        self.assertFalse(self.record["network"].get("attached"))

    def test_corrupted_self_restore_is_refused(self):
        self.attach()
        self.record["network"]["previous_http_proxy"] = "10.0.2.2:8899"
        with mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertEqual(self.settings()["http_proxy"], "10.0.2.2:8899")

    def test_dead_prior_local_proxy_is_not_restored(self):
        self.attach("10.0.2.2:58514")
        with mock.patch.object(device.socket, "create_connection", side_effect=OSError("dead")), \
                mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertEqual(self.settings()["http_proxy"], "10.0.2.2:8899")

    def test_dead_canonical_prior_proxy_is_not_restored(self):
        self.set_settings(global_http_proxy_host="10.0.2.2", global_http_proxy_port="58514")
        device.attach(self.target, self.record, port=8899, acknowledged=True)
        with mock.patch.object(device.socket, "create_connection", side_effect=OSError("dead")), \
                mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError):
                self.stop()
        stop.assert_not_called()
        self.assertEqual(self.settings()["http_proxy"], "10.0.2.2:8899")

    def test_live_prior_local_proxy_is_preserved(self):
        self.attach("10.0.2.2:58514")
        with mock.patch.object(device.socket, "create_connection") as probe:
            device.detach(self.target, self.record)
        probe.assert_called_once_with(("127.0.0.1", 58514), timeout=1)
        self.assertEqual(self.settings()["http_proxy"], "10.0.2.2:58514")

    def test_stop_without_attachment_never_resolves_device(self):
        with mock.patch.object(cli.proxy_mod, "stop", return_value={"was_running": False}), \
                mock.patch.object(cli, "_target", side_effect=AssertionError("device")):
            result = cli.network_stop_payload(self.record)
        self.assertFalse(result["detach"]["was_attached"])

    def test_ios_stop_clears_launch_environment_before_terminating(self):
        target = Target("ios", "simulator-test", "fake-simctl", {})
        self.record.update(platform="ios", target_id=target.target_id)
        self.record["network"] = {"enabled": True, "attached": True,
                                  "launch_env": {"http_proxy": "http://127.0.0.1:8899"}}
        def terminate(record):
            self.assertIsNone(record["network"]["launch_env"])
            self.assertFalse(session.load_current()["network"]["attached"])
            return {"was_running": True}
        with mock.patch.object(cli.proxy_mod, "stop", side_effect=terminate):
            cli.network_stop_payload(self.record, lambda: target)

    def test_wrong_target_cannot_detach_session(self):
        self.attach()
        other = Target("android", "emulator-5582", self.target.tool, {})
        with mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError) as caught:
                cli.network_stop_payload(self.record, lambda: other)
        self.assertEqual(caught.exception.code, errors.SESSION_TARGET_MISMATCH)
        stop.assert_not_called()

    def test_transparent_route_blocks_stop_before_device_actions(self):
        with mock.patch("autonom_lib.network.attachment.transparent_route_live", return_value=True), \
                mock.patch.object(cli.proxy_mod, "stop") as stop:
            with self.assertRaises(errors.AutonomError) as caught:
                self.stop()
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        stop.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_session_stop_failure_keeps_current_session_and_proxy(self):
        self.attach()
        with mock.patch.object(cli, "_target", return_value=self.target), \
                mock.patch.object(cli, "_session_target_gone", return_value=False), \
                mock.patch.object(device, "_put_setting", side_effect=errors.AutonomError(
                    errors.BACKEND_FAILED, "write failed")), \
                mock.patch.object(cli.proxy_mod, "stop") as stop, \
                mock.patch.object(session, "reap_owned_processes") as reap:
            with self.assertRaises(errors.AutonomError):
                cli.cmd_session_stop(argparse.Namespace())
        stop.assert_not_called()
        reap.assert_not_called()
        saved = session.require_current()
        self.assertTrue(saved["network"]["attached"])
        self.assertFalse(saved.get("stopping_at"))

    def test_attach_refresh_exception_keeps_written_proxy_recoverable(self):
        with mock.patch.object(cli.proxy_mod, "status", return_value={"running": True,"port":8899}), \
                mock.patch.object(device, "apply_proxy_setting", side_effect=RuntimeError("refresh")):
            with self.assertRaises(RuntimeError):
                cli.network_attach_payload(self.record, lambda: self.target, acknowledged=True)
        saved = session.require_current()
        self.assertTrue(saved["network"]["attached"])
        self.assertIsNone(saved["network"]["previous_http_proxy"])

    def test_wifi_is_reenabled_when_disabling_raises(self):
        with mock.patch.object(device.adb_mod, "run_adb", side_effect=[RuntimeError("disable"),
                mock.Mock(returncode=0)]) as adb:
            with self.assertRaises(RuntimeError):
                device.apply_proxy_setting(self.target)
        self.assertEqual(adb.call_args.args[1][-1], "enable")


if __name__ == "__main__":
    unittest.main()
