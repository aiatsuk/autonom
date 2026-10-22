"""System language reads, explicit targets and verified changes."""
import plistlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from autonom_lib import device_language as lang, errors
from autonom_lib.platform import Target

ANDROID = Target("android", "emulator-test", "/sdk/adb")
IOS = Target("ios", "simulator-test", "/usr/bin/xcrun")

class DeviceLanguageTests(unittest.TestCase):
    def test_android_reads_primary_and_uses_explicit_target(self):
        with patch.object(lang.adb, "run_adb", return_value=SimpleNamespace(stdout="de-DE,en-US")) as call:
            self.assertEqual(lang.current(ANDROID)["language"], "de-DE")
            self.assertEqual(call.call_args.kwargs["serial"], ANDROID.target_id)

    def test_android_falls_back_for_unset_language(self):
        with patch.object(lang, "android_call", side_effect=["null", "", "en_US"]):
            self.assertEqual(lang.current(ANDROID)["language"], "en-US")

    def test_ios_reads_runtime_preferences(self):
        data = plistlib.dumps({"AppleLanguages": ["ja-JP", "en"]}).decode()
        with patch.object(lang.ios_simctl, "run_simctl", return_value=SimpleNamespace(stdout=data)) as call:
            self.assertEqual(lang.current(IOS), {"language": "ja-JP", "restart_required": True})
            self.assertEqual(call.call_args.args[1], ["spawn", IOS.target_id, "defaults", "export", "NSGlobalDomain", "-"])

    def test_invalid_input_does_not_touch_device(self):
        with patch.object(lang, "current") as read:
            for value in [None, "", "de; reboot", "en_US", "../../foo"]:
                with self.assertRaises(errors.AutonomError):
                    lang.set_language(ANDROID, value)
            read.assert_not_called()

    def test_same_language_does_not_restart_or_compile(self):
        with patch.object(lang, "current", return_value={"language": "en"}), patch.object(lang.ios_simctl, "shutdown") as shutdown:
            self.assertFalse(lang.set_language(IOS, "en")["restarted"])
            shutdown.assert_not_called()

    def test_android_verifies_and_cleans_helper(self):
        with patch.object(lang, "current", side_effect=[{"language":"en-US"}, {"language":"de-DE"}]), patch.object(lang, "android_helper", return_value=("local.dex", "/data/local/tmp/helper.dex")), patch.object(lang, "android_call") as call:
            self.assertTrue(lang.set_language(ANDROID, "de-DE")["verified"])
            self.assertEqual(call.call_args_list[-1].args[1], ["shell", "rm", "-f", "/data/local/tmp/helper.dex"])
            self.assertIn("de-DE", call.call_args_list[1].args[1])

    def test_android_mismatch_is_failure(self):
        with patch.object(lang, "current", return_value={"language":"en-US"}), patch.object(lang, "android_helper", return_value=("local.dex", "/data/local/tmp/helper.dex")), patch.object(lang, "android_call"), patch.object(lang.time, "sleep"):
            with self.assertRaisesRegex(errors.AutonomError, "did not confirm"):
                lang.set_language(ANDROID, "de-DE")

    def test_ios_requires_explicit_restart(self):
        with patch.object(lang, "current", return_value={"language":"en"}), patch.object(lang.ios_simctl, "shutdown") as shutdown:
            with self.assertRaisesRegex(errors.AutonomError, "requires a restart"):
                lang.set_language(IOS, "de-DE")
            shutdown.assert_not_called()

    def test_ios_preserves_region_and_other_preferences_and_boots(self):
        prefs = {"AppleLanguages":["en", "ru-RU"], "AppleLocale":"en_US@rg=nlzzzz", "KeyboardAutocorrection":False}
        with patch.object(lang, "current", side_effect=[{"language":"en"}, {"language":"de-DE"}]), patch.object(lang.ios_prefs, "require_preferences_dir"), patch.object(lang.ios_simctl, "find_simulator", side_effect=[SimpleNamespace(state="Booted"), SimpleNamespace(state="Shutdown")]), patch.object(lang.ios_simctl, "shutdown") as shutdown, patch.object(lang.ios_simctl, "boot") as boot, patch.object(lang.ios_prefs, "read_domain", return_value=prefs.copy()), patch.object(lang.ios_prefs, "write_domain") as write:
            self.assertTrue(lang.set_language(IOS, "de-DE", restart=True)["restarted"])
            self.assertEqual(write.call_args.args, (IOS.target_id, lang.ios_prefs.GLOBAL_DOMAIN, {**prefs, "AppleLanguages":["de-DE", "en", "ru-RU"]}))
            shutdown.assert_called_once_with(IOS.tool, IOS.target_id)
            boot.assert_called_once_with(IOS.tool, IOS.target_id)

    def test_ios_does_not_write_if_shutdown_fails(self):
        with patch.object(lang, "current", return_value={"language":"en"}), patch.object(lang.ios_prefs, "require_preferences_dir"), patch.object(lang.ios_simctl, "find_simulator", return_value=SimpleNamespace(state="Booted")), patch.object(lang.ios_simctl, "shutdown"), patch.object(lang.ios_prefs, "write_domain") as write:
            with self.assertRaisesRegex(errors.AutonomError, "did not shut down"):
                lang.set_language(IOS, "de-DE", restart=True)
            write.assert_not_called()

    def test_ios_boots_even_if_preferences_write_fails(self):
        with patch.object(lang, "current", return_value={"language":"en"}), patch.object(lang.ios_prefs, "require_preferences_dir"), patch.object(lang.ios_simctl, "find_simulator", side_effect=[SimpleNamespace(state="Booted"), SimpleNamespace(state="Shutdown")]), patch.object(lang.ios_simctl, "shutdown"), patch.object(lang.ios_simctl, "boot") as boot, patch.object(lang.ios_prefs, "read_domain", return_value={}), patch.object(lang.ios_prefs, "write_domain", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                lang.set_language(IOS, "de-DE", restart=True)
            boot.assert_called_once_with(IOS.tool, IOS.target_id)

    def test_tools_rejects_restart_while_recording(self):
        from autonom_canvas_tools import Tools, LEASED_TOOLS_OPS
        self.assertIn("device.language.set", LEASED_TOOLS_OPS)
        tools = Tools(IOS)
        with patch.object(tools, "_recording_active", return_value=True), patch.object(lang, "set_language") as setter:
            with self.assertRaisesRegex(errors.AutonomError, "Stop recording"):
                tools.device_language_set({"language":"de-DE", "restart":True}, None)
            setter.assert_not_called()
