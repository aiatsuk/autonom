from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
FAKE_AXE = ROOT / "tests/fakes/fake_axe.py"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import doctor  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

try:
    from process_isolation import own_entries  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.process_isolation import own_entries  # noqa: E402

COMPANION_2022 = '{"build_time":"08:41:50","build_date":"Aug 12 2022"}'
COMPANION_FIXED = '{"version":"1.6.2","build_date":"Jun 20 2026"}'


# Inherited variables that would point doctor at a real tool (or a real
# companion / idb state file) on the machine running the suite.
TOOL_ENV = ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_MITMDUMP",
            "AUTONOM_AXE", "AUTONOM_IOS_HID", "AUTONOM_IDB_COMPANION",
            "AUTONOM_IDB_STATE_FILE", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG",
            "DEVELOPER_DIR")


def hermetic_env(home: str, path: str) -> dict:
    """os.environ without any tool override, with its own home and PATH."""
    env = {key: value for key, value in os.environ.items() if key not in TOOL_ENV}
    env.update({"AUTONOM_HOME": home, "PATH": path})
    return env


def fakes_path(tmp: str) -> str:
    """A PATH holding only python3 (the fakes' interpreter): every tool that
    is not handed in by flag resolves to missing, never to a real idb, axe,
    idb_companion, xcrun or adb on the developer's machine."""
    directory = Path(tmp) / "fake-bin"
    directory.mkdir(exist_ok=True)
    python = directory / "python3"
    if not python.exists():
        python.symlink_to(sys.executable)
    return str(directory)


class DoctorTests(unittest.TestCase):
    """CAP-DOC-001..003 — one answer to 'what can this machine do?'."""

    def _run(self, *args: str, bare: bool = False, cwd: str | None = None,
             home: str | None = None):
        env = {key: value for key, value in os.environ.items() if key not in TOOL_ENV}
        # The process registry and session store are machine-wide; without
        # isolation these tests read the developer's real orphans (a booted
        # emulator, a live proxy) and the "clean host" oracle becomes a function
        # of the host's mood. A caller can pass its own home to seed orphans.
        owned = None
        if home is None:
            owned = tempfile.TemporaryDirectory()
            home = owned.name
        env["AUTONOM_HOME"] = home
        # Bare: nothing at all on PATH. Otherwise only the fakes' python3, so
        # a tool not passed by flag is missing rather than the host's own.
        empty = tempfile.TemporaryDirectory()
        env["PATH"] = empty.name if bare else fakes_path(empty.name)
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
            {"android": False, "ios_session": False, "ios_ui": False, "network": False,
             "ios_tree": False, "ios_hid": False},
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
            # Isolate the machine store like _run does — without this the
            # probe mkdirs the operator's real ~/.autonom/sessions — and the
            # PATH, so no real idb_companion or axe answers next to it.
            env = hermetic_env(tmp, fakes_path(tmp))
            env["AUTONOM_IDB"] = str(broken)
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
            # discovery is machine-wide: another test's proxy is not this one's
            orphans = own_entries(report["orphans"], home)
            self.assertEqual(len(orphans), 1)
            self.assertEqual(orphans[0]["port"], 8080)
            self.assertIn("network stop", orphans[0]["hint"])

    def test_clean_host_reports_no_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            report = json.loads(self._run(bare=True, home=home).stdout)
            # a proxy another test (or the developer) runs is not this host's
            # clean-store answer: only what this home owns or holds counts
            self.assertEqual(own_entries(report["orphans"], home), [])
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
            env = hermetic_env(home, fakes_path(home))
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



class XcodeSimulatorKitTests(unittest.TestCase):
    """IOS-001 — doctor detects the Xcode 27 SimulatorKit move.

    A synthetic Xcode.app and a PATH holding only fakes: nothing here reads
    the real Xcode, companion, or AXe on the developer's machine.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        (self.bin / "idb").symlink_to(FAKE_IDB)
        self.home = self.root / "home"

    def xcode(self, version: str, *, legacy: bool, shared: bool) -> Path:
        contents = self.root / f"Xcode-{version}.app" / "Contents"
        developer = contents / "Developer"
        developer.mkdir(parents=True)
        with (contents / "version.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": version,
                           "ProductBuildVersion": "27A266a"}, handle)
        if legacy:
            (developer / "Library/PrivateFrameworks/SimulatorKit.framework").mkdir(parents=True)
        if shared:
            (contents / "SharedFrameworks/SimulatorKit.framework").mkdir(parents=True)
        return developer

    def companion(self, stdout: str) -> None:
        path = self.bin / "idb_companion"
        # Shell builtins only: PATH holds nothing but the fakes.
        path.write_text("#!/bin/sh\necho 'SECRET_TOKEN=do-not-print' >&2\n"
                        f"printf '%s\\n' '{stdout}'\n", encoding="utf-8")
        path.chmod(0o755)

    def doctor(self, developer: Path, **extra: str) -> dict:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("AUTONOM_")}
        env.update({"PATH": str(self.bin), "DEVELOPER_DIR": str(developer),
                    "AUTONOM_HOME": str(self.home), **extra})
        result = subprocess.run(
            [sys.executable, str(CLI), "doctor"], cwd=self.root, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("do-not-print", result.stdout)
        return json.loads(result.stdout)

    def test_xcode27_with_a_2022_companion_marks_hid_not_ready(self) -> None:
        developer = self.xcode("27.0", legacy=False, shared=True)
        self.companion(COMPANION_2022)
        report = self.doctor(developer)
        caps = report["capabilities"]
        self.assertTrue(caps["ios_tree"]["ready"], "describe-all still works")
        self.assertFalse(caps["ios_hid"]["ready"])
        self.assertFalse(caps["ios_ui"]["ready"], "ios_ui reflects HID honestly")
        self.assertIn("tap", caps["ios_ui"]["degraded"])
        warning = next(w for w in report["warnings"]
                       if w["code"] == "idb_companion_predates_xcode27")
        self.assertIn("brew update && brew upgrade idb-companion", warning["hint"])
        self.assertIn("pipx upgrade fb-idb", warning["fix"])
        ios = report["ios"]
        self.assertEqual(ios["xcode_version"], "27.0")
        self.assertTrue(ios["simulatorkit_path"].endswith(
            "Contents/SharedFrameworks/SimulatorKit.framework"))
        self.assertFalse(ios["simulatorkit_legacy_present"])
        self.assertEqual(ios["idb_companion"]["build_date"], "Aug 12 2022")
        self.assertTrue(ios["idb_companion"]["predates_xcode27_fix"])
        self.assertEqual(report["tools"]["idb_companion"]["build_date"], "Aug 12 2022")
        self.assertEqual(ios["axe"]["state"], "missing")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(set(checks), {"xcode_developer_dir", "simulatorkit",
                                       "idb_companion_hid", "axe"})
        for check in checks.values():
            self.assertEqual(set(check), {"name", "ok", "required", "detail", "fix"})
        self.assertFalse(checks["idb_companion_hid"]["ok"])
        self.assertTrue(checks["idb_companion_hid"]["required"])
        self.assertIn("brew upgrade idb-companion", checks["idb_companion_hid"]["fix"])

    def test_axe_takes_hid_over_and_doctor_says_so(self) -> None:
        developer = self.xcode("27.0", legacy=False, shared=True)
        self.companion(COMPANION_2022)
        (self.bin / "axe").symlink_to(FAKE_AXE)
        # AXe needs the simulator toolchain too; `xcrun simctl help` just exits 0.
        xcrun = self.bin / "xcrun"
        xcrun.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        xcrun.chmod(0o755)
        report = self.doctor(developer)
        caps = report["capabilities"]
        self.assertFalse(caps["ios_hid"]["ready"])
        self.assertEqual(caps["ios_hid"]["backend"], "axe")
        self.assertTrue(caps["ios_ui"]["ready"])
        self.assertIn("AXe", caps["ios_ui"]["degraded"])
        warning = next(w for w in report["warnings"]
                       if w["code"] == "idb_companion_predates_xcode27")
        self.assertIn("route through AXe", warning["hint"])
        self.assertEqual(report["ios"]["axe"]["path"], str(self.bin / "axe"))
        self.assertEqual(report["ios"]["axe"]["version"], "1.8.0")
        self.assertFalse({c["name"]: c for c in report["checks"]}["idb_companion_hid"]["required"])

    def test_fixed_companion_is_ready(self) -> None:
        developer = self.xcode("27.0", legacy=False, shared=True)
        self.companion(COMPANION_FIXED)
        report = self.doctor(developer)
        self.assertTrue(report["capabilities"]["ios_hid"]["ready"])
        self.assertTrue(report["capabilities"]["ios_ui"]["ready"])
        self.assertIsNone(report["capabilities"]["ios_ui"]["degraded"])
        self.assertNotIn("idb_companion_predates_xcode27",
                         {w["code"] for w in report["warnings"]})

    def test_older_xcode_keeps_the_legacy_path_and_works(self) -> None:
        developer = self.xcode("26.1", legacy=True, shared=False)
        self.companion(COMPANION_2022)
        report = self.doctor(developer)
        self.assertTrue(report["capabilities"]["ios_hid"]["ready"])
        self.assertTrue(report["ios"]["simulatorkit_path"].endswith(
            "Developer/Library/PrivateFrameworks/SimulatorKit.framework"))

    def test_invalid_hid_mode_is_warned(self) -> None:
        developer = self.xcode("27.0", legacy=False, shared=True)
        self.companion(COMPANION_FIXED)
        report = self.doctor(developer, AUTONOM_IOS_HID="bogus")
        self.assertIn("invalid_ios_hid_mode", {w["code"] for w in report["warnings"]})
        self.assertEqual(report["ios"]["hid_mode"], "auto")


class SlowDeviceTests(EnvSandboxMixin, unittest.TestCase):
    """IOS-006 — a timed-out probe is a warning, never an abort."""

    def setUp(self) -> None:
        self.sandbox_home()
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        # Nothing real is reachable: every tool resolves to missing.
        self.set_env(PATH=empty.name, DEVELOPER_DIR=None, AUTONOM_ADB=None,
                     AUTONOM_SIMCTL=None, AUTONOM_IDB=None, AUTONOM_AXE=None,
                     AUTONOM_MITMDUMP=None, AUTONOM_IOS_HID=None)

    def patched(self):
        live = [{"pid": os.getpid(), "port": 8080, "session_id": "s_other"}]
        timeout = subprocess.TimeoutExpired(["adb", "shell"], 10)
        return (
            mock.patch.object(doctor.adb_mod, "find_adb", return_value="adb"),
            mock.patch.object(doctor.adb_mod, "list_devices", return_value=[
                SimpleNamespace(serial="emulator-5554", state="device")]),
            mock.patch.object(doctor.adb_mod, "run_adb", side_effect=timeout),
            live,
        )

    def test_timed_out_probe_is_a_warning_naming_the_target(self) -> None:
        find, devices, run, live = self.patched()
        with find, devices, run:
            warnings = doctor._foreign_attachments(None, live)  # noqa: SLF001
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "probe_timed_out")
        self.assertEqual(warnings[0]["target_id"], "emulator-5554")
        self.assertIn("emulator-5554", warnings[0]["error"])

    def test_doctor_stays_ok_when_a_device_hangs(self) -> None:
        find, devices, run, live = self.patched()
        scan = {"live": live, "orphans": [], "stale_entries": []}
        with find, devices, run, mock.patch("autonom_lib.processes.scan", return_value=scan):
            report = doctor.collect()
        self.assertTrue(report["ok"])
        timed_out = [w for w in report["warnings"] if w["code"] == "probe_timed_out"]
        self.assertEqual([w["target_id"] for w in timed_out], ["emulator-5554"])

    def test_timed_out_device_listing_is_a_warning_too(self) -> None:
        with mock.patch.object(doctor.adb_mod, "find_adb", return_value="adb"), \
                mock.patch.object(doctor.adb_mod, "list_devices",
                                  side_effect=subprocess.TimeoutExpired(["adb"], 10)):
            warnings = doctor._foreign_attachments(None, [{"pid": 1, "port": 8080}])  # noqa: SLF001
        self.assertEqual(warnings[0]["code"], "probe_timed_out")


if __name__ == "__main__":
    unittest.main()
