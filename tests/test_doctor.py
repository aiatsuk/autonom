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
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"


class DoctorTests(unittest.TestCase):
    """CAP-DOC-001..003 — one answer to 'what can this machine do?'."""

    def _run(self, *args: str, bare: bool = False, cwd: str | None = None,
             home: str | None = None):
        env = dict(os.environ)
        for key in ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_MITMDUMP",
                    "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG"):
            env.pop(key, None)
        # The process registry and session store are machine-wide; without
        # isolation these tests read the developer's real orphans (a booted
        # emulator, a live proxy) and the "clean host" oracle becomes a function
        # of the host's mood. A caller can pass its own home to seed orphans.
        owned = None
        if home is None:
            owned = tempfile.TemporaryDirectory()
            home = owned.name
        env["AUTONOM_HOME"] = home
        empty = None
        if bare:
            empty = tempfile.TemporaryDirectory()
            env["PATH"] = empty.name
        try:
            return subprocess.run(
                [sys.executable, str(CLI), "doctor", *args],
                cwd=cwd or ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
            )
        finally:
            if owned:
                owned.cleanup()
            if empty:
                empty.cleanup()

    def test_report_shape_on_a_bare_host(self) -> None:
        result = self._run(bare=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(
            set(report["tools"]), {"adb", "simctl", "idb", "idb_companion", "mitmdump"}
        )
        for name, entry in report["tools"].items():
            with self.subTest(tool=name):
                self.assertEqual(entry["state"], "missing")
                self.assertTrue(entry.get("install_hint"), f"{name} has no install hint")

    def test_capabilities_are_all_false_when_nothing_is_installed(self) -> None:
        report = json.loads(self._run(bare=True).stdout)
        self.assertEqual(
            {key: value["ready"] for key, value in report["capabilities"].items()},
            {"android": False, "ios_session": False, "ios_ui": False, "network": False},
        )
        for entry in report["capabilities"].values():
            self.assertTrue(entry["needs"])

    def test_strict_turns_missing_tools_into_a_failure(self) -> None:
        plain = self._run(bare=True)
        strict = self._run("--strict", bare=True)
        self.assertEqual(plain.returncode, 0)
        self.assertEqual(strict.returncode, 1)
        # Same payload either way; only the exit code differs.
        self.assertEqual(json.loads(plain.stdout)["tools"], json.loads(strict.stdout)["tools"])

    def test_no_traceback_and_valid_json_with_nothing_installed(self) -> None:
        result = self._run(bare=True)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        json.loads(result.stdout)

    def test_idb_python314_failure_is_diagnosed_specifically(self) -> None:
        """The traceback fb-idb emits under Python 3.14 does not name its own cause."""
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "idb"
            broken.write_text(
                "#!/bin/sh\n"
                "echo 'RuntimeError: There is no current event loop in thread MainThread.' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            broken.chmod(0o755)
            env = dict(os.environ)
            env["AUTONOM_IDB"] = str(broken)
            # Isolate the machine store like _run does — without this the
            # probe mkdirs the operator's real ~/.autonom/sessions.
            env["AUTONOM_HOME"] = tmp
            result = subprocess.run(
                [sys.executable, str(CLI), "doctor"],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
            )
            entry = json.loads(result.stdout)["tools"]["idb"]
            self.assertEqual(entry["state"], "error")
            self.assertIn("python@3.12", entry["install_hint"])

    def test_orphaned_proxy_is_reported_with_a_cleanup_command(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            network = Path(home) / "sessions" / "s_orphan" / "network"
            network.mkdir(parents=True)
            # This process is alive by definition, so it stands in for a live proxy.
            (network / "proxy.json").write_text(
                json.dumps({"pid": os.getpid(), "port": 8080}), encoding="utf-8"
            )
            report = json.loads(self._run(bare=True, home=home).stdout)
            self.assertEqual(len(report["orphans"]), 1)
            self.assertEqual(report["orphans"][0]["port"], 8080)
            self.assertIn("network stop", report["orphans"][0]["hint"])

    def test_clean_host_reports_no_orphans(self) -> None:
        report = json.loads(self._run(bare=True).stdout)
        self.assertEqual(report["orphans"], [])
        self.assertIsNone(report["session"])

    def test_dangling_attachment_is_surfaced(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            session = Path(home) / "sessions" / "s_dangling"
            session.mkdir(parents=True)
            (session / "session.json").write_text(json.dumps({
                "session_id": "s_dangling", "platform": "android", "target_id": "emulator-5554",
                "artifacts_dir": str(session),
                "network": {"attached": True, "proxy_port": 8080},
            }), encoding="utf-8")
            report = json.loads(self._run(bare=True, home=home).stdout)
            codes = {warning["code"] for warning in report["warnings"]}
            self.assertIn("device_may_be_left_attached", codes)

    def test_active_overrides_are_named(self) -> None:
        """A `--adb` flag is promoted to AUTONOM_ADB; doctor must say so."""
        report = json.loads(self._run("--adb", str(FAKE_ADB)).stdout)
        self.assertEqual(report["overrides"]["AUTONOM_ADB"],
                         {"value": str(FAKE_ADB), "exists": True})
        # The test harness's own AUTONOM_HOME redirect is an override too.
        self.assertIn("AUTONOM_HOME", report["overrides"])
        self.assertNotIn("AUTONOM_SIMCTL", report["overrides"])

    def test_override_pointing_at_nothing_is_warned_by_name(self) -> None:
        """The stale-env trap: adb reads as missing while `which adb` finds it."""
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ)
            env["AUTONOM_HOME"] = home
            env["AUTONOM_ADB"] = str(Path(home) / "nonexistent-adb")
            result = subprocess.run(
                [sys.executable, str(CLI), "doctor"],
                cwd=ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
            )
            report = json.loads(result.stdout)
            self.assertFalse(report["overrides"]["AUTONOM_ADB"]["exists"])
            warning = next(w for w in report["warnings"] if w["code"] == "override_path_missing")
            self.assertEqual(warning["variable"], "AUTONOM_ADB")
            self.assertIn("unset AUTONOM_ADB", warning["hint"])
            self.assertNotEqual(report["tools"]["adb"]["state"], "ok")

    def test_installed_tools_are_reported_ok(self) -> None:
        env_result = self._run("--adb", str(FAKE_ADB), "--simctl", str(FAKE_SIMCTL))
        report = json.loads(env_result.stdout)
        self.assertEqual(report["tools"]["adb"]["state"], "ok")
        self.assertEqual(report["capabilities"]["android"]["ready"], True)


if __name__ == "__main__":
    unittest.main()
