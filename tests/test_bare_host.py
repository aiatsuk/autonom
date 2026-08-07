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
UI_FIXTURE = ROOT / "tests/fixtures/ui_dump.xml"
IOS_FIXTURE = ROOT / "tests/fixtures/idb_describe_all_sample.json"

UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

# Every device-touching verb, plus the offline ones that must keep working.
SWEEP: list[tuple[str, list[str], bool]] = [
    ("version", ["version"], True),
    ("devices", ["devices"], True),
    ("doctor", ["doctor"], True),
    ("devices_list", ["devices", "list"], True),
    ("devices_android", ["devices", "--platform", "android"], False),
    ("devices_ios", ["devices", "--platform", "ios"], False),
    ("devices_boot_avd", ["devices", "boot", "--avd", "Pixel_9"], False),
    ("devices_boot_serial", ["devices", "boot", "--serial", "emulator-5554"], False),
    ("devices_boot_udid", ["devices", "boot", "--udid", UDID], False),
    ("devices_shutdown", ["devices", "shutdown", "--serial", "emulator-5554"], False),
    ("session_show", ["session", "show"], False),
    ("session_stop", ["session", "stop"], False),
    ("session_start", ["session", "start"], False),
    ("session_launch", ["session", "launch", "com.example.app"], False),
    ("session_force_stop", ["session", "force-stop", "com.example.app"], False),
    ("session_clear", ["session", "clear", "com.example.app"], False),
    ("session_uninstall", ["session", "uninstall", "com.example.app"], False),
    ("ui_tree_live", ["ui", "tree"], False),
    ("ui_tree_dump_android", ["ui", "tree", "--dump", str(UI_FIXTURE)], True),
    ("ui_tree_dump_ios", ["ui", "tree", "--dump", str(IOS_FIXTURE)], True),
    ("ui_find_live", ["ui", "find", "--text", "x"], False),
    ("ui_find_dump", ["ui", "find", "--dump", str(UI_FIXTURE), "--text", "Settings", "--all"], True),
    ("ui_tap", ["ui", "tap", "--text", "x"], False),
    ("ui_swipe", ["ui", "swipe", "--from", "1,2", "--to", "3,4"], False),
    ("ui_shake", ["ui", "shake"], False),
    ("ui_pinch", ["ui", "pinch", "--at", "10,10"], False),
    ("ui_rotate", ["ui", "rotate"], False),
    ("ui_type", ["ui", "type", "hello"], False),
    ("ui_key", ["ui", "key", "KEYCODE_BACK"], False),
    ("screenshot", ["screenshot", "--out", "/dev/null"], False),
    ("logs_tail", ["logs", "tail"], False),
    ("network_start", ["network", "start", "--i-understand-mitm"], False),
    ("network_stop", ["network", "stop"], False),
    ("network_status", ["network", "status"], False),
    ("network_attach", ["network", "attach", "--i-understand-mitm"], False),
    ("network_detach", ["network", "detach"], False),
    ("network_requests_list", ["network", "requests", "list"], False),
    ("network_requests_show", ["network", "requests", "show", "f_0001"], False),
    ("network_export", ["network", "export"], False),
    # Mock CRUD is pure local state in the persistent registry: no device, no
    # session, no mitmdump. It therefore SUCCEEDS on a bare host by design —
    # you can prepare rules long before anything is plugged in.
    ("network_mock_add", ["network", "mock", "add", "--match", "*"], True),
    ("network_mock_list", ["network", "mock", "list"], True),
    ("network_mock_clear", ["network", "mock", "clear"], True),
    ("crash_list", ["crash", "list"], False),
    ("open_url", ["open", "https://example.com"], False),
    ("permissions", ["permissions", "grant", "photos", "com.example.app"], False),
    ("location_set", ["location", "set", "1,2"], False),
    ("location_get", ["location", "get"], False),
    ("media_add", ["media", "add", "/dev/null"], False),
    ("file_ls", ["file", "ls"], False),
    ("record_start", ["record", "start"], False),
    ("record_stop", ["record", "stop"], False),
    # Cleanup must work when everything else is broken — that is its job.
    ("processes", ["processes"], True),
    ("cleanup_dry_run", ["cleanup", "--dry-run"], True),
    # The journal is read-only and session-scoped: with no session it is a soft
    # empty, never an error. A note needs a session, so it fails cleanly.
    ("journal", ["journal"], True),
    ("note_add", ["note", "add", "a thought"], False),
]


class BareHostTests(unittest.TestCase):
    """VER-011 / INV-08 — the oracle that replaces CI (DEC-012).

    Every verb must fail with one machine-readable error code when its backend
    is absent. A traceback on stdout, or a non-JSON stderr, would leave a host
    agent with nothing to branch on.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.empty = tempfile.TemporaryDirectory()
        cls.workdir = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.empty.cleanup()
        cls.workdir.cleanup()

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = self.empty.name  # no adb, no xcrun, no idb, no mitmdump
        # The mock registry is machine-level, so an un-redirected sweep would
        # write into — and `mock clear` would wipe — the developer's real one.
        env["AUTONOM_HOME"] = self.workdir.name
        for key in ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB",
                    "AUTONOM_IDB_COMPANION", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG"):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, str(CLI), *argv],
            cwd=self.workdir.name, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
        )

    def test_every_verb_degrades_to_one_machine_readable_error(self) -> None:
        for name, argv, expect_success in SWEEP:
            with self.subTest(verb=name):
                result = self._run(argv)
                self.assertNotIn("Traceback", result.stdout, f"{name} leaked a traceback to stdout")
                self.assertNotIn("Traceback", result.stderr, f"{name} leaked a traceback")
                if expect_success:
                    self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
                    json.loads(result.stdout)
                    continue
                self.assertEqual(result.returncode, 2, f"{name} should have failed cleanly")
                payload = json.loads(result.stderr)
                self.assertFalse(payload["ok"])
                self.assertTrue(payload.get("error_code"), f"{name} has no error_code")
                self.assertTrue(payload.get("error"))

    def test_missing_tools_name_their_install_hint(self) -> None:
        cases = {
            ("devices", "--platform", "android"): "adb_not_found",
            ("devices", "--platform", "ios"): "simctl_not_found",
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                payload = json.loads(self._run(list(argv)).stderr)
                self.assertEqual(payload["error_code"], expected)
                self.assertIn("doctor", payload["hint"])

    def test_ios_ui_without_idb_reports_idb_required(self) -> None:
        result = self._run(["--platform", "ios", "--target", UDID, "ui", "tree"])
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        # xcrun is absent too on a bare host, so the target resolves no further
        # than the toolchain; either code is a correct, actionable answer.
        self.assertIn(payload["error_code"], {"idb_required", "simctl_not_found"})

    def test_offline_parsing_still_works_for_both_platforms(self) -> None:
        android = json.loads(self._run(["ui", "tree", "--dump", str(UI_FIXTURE)]).stdout)
        ios = json.loads(self._run(["ui", "tree", "--dump", str(IOS_FIXTURE)]).stdout)
        self.assertEqual(android["platform"], "android")
        self.assertEqual(ios["platform"], "ios")
        self.assertGreaterEqual(android["count"], 4)
        self.assertGreaterEqual(ios["count"], 3)
        self.assertEqual(set(android["nodes"][0]), set(ios["nodes"][0]))


if __name__ == "__main__":
    unittest.main()
