"""Cross-module wiring found in the hardening reviews, proven against fakes.

- SEC-005: a run manifest names the bytes it executed (``flow_sha256`` and
  every runFlow child's ``subflow_sha256``), and Teach approval counts by
  them — an mtime-preserving edit no longer passes as a replay;
- TOUR-003 / IOS-003: the tour's iOS preflight counts AXe as HID-ready and
  refuses only when nothing can tap; its repair brief sees the real timeline;
- IOS-003: long press and double tap go through the HID backend selection,
  and every actuation verb names the backend it used;
- IOS-007: the session log stream is bounded, and a generic bundle-id leaf
  (``com.example.app``) no longer matches every process;
- FLOW-006: directory discovery reports the yaml files it skipped;
- doctor tests: nothing on the host's PATH or in its environment answers.

Nothing here reaches a real adb, simctl, idb, idb_companion or AXe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_AXE = ROOT / "tests/fakes/fake_axe.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import app_skills, errors, ios_simctl, logs, teach, tour, ui, ui_ios  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib.flow import validator  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

try:
    import test_doctor  # noqa: E402  (discover -s tests)
except ImportError:
    from tests import test_doctor  # noqa: E402

_PARENT = ("schema: autonom.dev/flow/v1\nid: flow_parent\nappId: com.example.app\n"
           "name: parent\n---\n- runFlow: sub/child.yaml\n")
_CHILD = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: child\n"
          "---\n- back\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Sandbox(EnvSandboxMixin, unittest.TestCase):
    """Own home, fake state/log, and a PATH that holds nothing real."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.state.write_text(json.dumps({"ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), PATH=str(self.bin),
                     AUTONOM_ADB=str(self.root / "absent-adb"),
                     AUTONOM_SIMCTL=str(self.root / "absent-xcrun"),
                     AUTONOM_IDB=str(self.root / "absent-idb"),
                     AUTONOM_AXE=None, AUTONOM_IOS_HID=None, AUTONOM_IDB_COMPANION=None,
                     AUTONOM_IOS_LOG_MAX_MB=None, DEVELOPER_DIR=None)
        ui_ios.reset_hid_probe()
        self.addCleanup(ui_ios.reset_hid_probe)

    def calls(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        return [entry["argv"] for entry in entries if entry.get("tool") == tool]


# --- SEC-005 -------------------------------------------------------------------


class ManifestHashBindingTests(_Sandbox):
    """The executor records what it ran; Teach approval counts by it."""

    def setUp(self) -> None:
        super().setUp()
        (self.root / ".autonom").mkdir()
        (self.root / "sub").mkdir()
        self.flow = self.root / "parent.yaml"
        self.child = self.root / "sub/child.yaml"
        self.flow.write_text(_PARENT, encoding="utf-8")
        self.child.write_text(_CHILD, encoding="utf-8")
        started = self._cli("session", "start", "--app-id", "com.example.app")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.record = session_mod.load_current()

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "android", "--serial", "emulator-5554",
             "--adb", str(FAKE_ADB), *args],
            cwd=self.root, env=dict(os.environ), text=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)

    def _run_flow(self) -> dict:
        result = self._cli("flow", "run", str(self.flow))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        return json.loads((Path(summary["events"]).parent / "manifest.json")
                          .read_text(encoding="utf-8"))

    def test_manifest_names_root_and_child_bytes(self) -> None:
        manifest = self._run_flow()
        self.assertEqual(manifest["flow_sha256"], _sha(self.flow))
        self.assertEqual(manifest["subflow_sha256"],
                         {str(self.child.resolve()): _sha(self.child)})

    def test_mtime_preserving_edit_is_not_a_replay(self) -> None:
        """`cp -p` / `touch -r` keep the mtime: before manifests carried a
        hash, the mtime fallback counted these replays for the edited file."""
        for _ in range(3):
            self._run_flow()
        stat = self.flow.stat()
        self.flow.write_text(_PARENT.replace("name: parent", "name: parenT"), encoding="utf-8")
        os.utime(self.flow, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(errors.AutonomError) as caught:
            teach.approve(self.record, self.flow)
        self.assertEqual(caught.exception.code, errors.TEACH_APPROVAL_BLOCKED)
        self.assertEqual(caught.exception.extra["clean_replays"], 0)

    def test_changed_child_refuses_approval_and_promotion(self) -> None:
        for _ in range(3):
            self._run_flow()
        receipt = teach.approve(self.record, self.flow)
        self.assertEqual(receipt["replay_binding"]["sha256"], 3)
        self.assertEqual(receipt["legacy_unhashed"], [])
        self.assertEqual(receipt["subflow_sha256"],
                         {str(self.child.resolve()): _sha(self.child)})
        teach.verify_subflows(receipt)  # unchanged: passes
        self.child.write_text(_CHILD.replace("- back", "- back\n- back"), encoding="utf-8")
        with self.assertRaises(errors.AutonomError) as caught:
            teach.approve(self.record, self.flow)
        self.assertEqual(caught.exception.code, errors.FLOW_SOURCE_CHANGED)
        self.assertEqual([item["path"] for item in caught.exception.extra["changed_subflows"]],
                         [str(self.child.resolve())])
        with self.assertRaises(errors.AutonomError) as caught:
            teach.verify_subflows(receipt)
        self.assertEqual(caught.exception.code, errors.FLOW_SOURCE_CHANGED)

    def test_promote_refuses_a_changed_child(self) -> None:
        """The root bytes still match the receipt; only the runFlow child
        moved. Promotion must refuse instead of packaging unapproved steps."""
        for _ in range(3):
            self._run_flow()
        teach.approve(self.record, self.flow)
        self.child.write_text(_CHILD.replace("- back", "- back\n- back"), encoding="utf-8")
        with self.assertRaises(errors.AutonomError) as caught:
            app_skills.promote(self.root, "com.example.app", self.flow)
        self.assertEqual(caught.exception.code, errors.FLOW_SOURCE_CHANGED)
        self.assertEqual([item["path"] for item in caught.exception.extra["changed_subflows"]],
                         [str(self.child.resolve())])
        self.assertEqual(caught.exception.extra["flow"], str(self.flow))
        self.assertFalse((self.root / ".autonom/apps/com.example.app/catalog.json").exists())

    def test_legacy_manifests_are_marked_in_the_receipt(self) -> None:
        for index in range(3):
            run = Path(self.record["artifacts_dir"]) / "flows" / f"old-{index}"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({
                "run_id": f"old-{index}", "flow_id": "flow_parent", "status": "passed",
                "started_at_ms": 4_000_000_000_000}), encoding="utf-8")
        os.utime(self.flow, (900, 900))
        receipt = teach.approve(self.record, self.flow)
        self.assertEqual(sorted(receipt["legacy_unhashed"]), ["old-0", "old-1", "old-2"])
        self.assertEqual(receipt["warnings"][0]["code"], "legacy_unhashed_replays")

    def test_hashed_manifest_never_falls_back_to_mtime(self) -> None:
        for index in range(3):
            run = Path(self.record["artifacts_dir"]) / "flows" / f"new-{index}"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({
                "run_id": f"new-{index}", "flow_id": "flow_parent", "status": "passed",
                "flow_sha256": None, "started_at_ms": 4_000_000_000_000}),
                encoding="utf-8")
        os.utime(self.flow, (900, 900))  # the mtime would have allowed it
        with self.assertRaises(errors.AutonomError) as caught:
            teach.approve(self.record, self.flow)
        self.assertEqual(caught.exception.code, errors.TEACH_APPROVAL_BLOCKED)


class LoaderHashTests(unittest.TestCase):
    def test_source_hash_is_of_the_parsed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crlf.yaml"
            path.write_bytes(_CHILD.replace("\n", "\r\n").encode("utf-8"))
            flow = validator.load_flow(path)
            self.assertEqual(validator.source_sha256(flow), _sha(path))
            self.assertEqual([step.command for step in flow.steps], ["back"])


# --- TOUR-003 / IOS-003 ----------------------------------------------------------


READY_IDB = {"state": "ready", "version": "1.1.7", "hid": {"ready": True}}
BROKEN_HID = {"state": "ready", "version": "1.1.7",
              "hid": {"ready": False, "reason": "SimulatorKit moved",
                      "fix": "brew update && brew upgrade idb-companion"}}


class _BootReached(Exception):
    """Raised in place of booting: the preflight let the walk through."""


class TourHidPreflightTests(_Sandbox):
    CHOICE = {"platform": IOS, "target_id": UDID, "name": "iPhone", "boot_needed": False}

    def _preflight(self, probe: dict):
        """Run the tour up to the boot; a sentinel stands in for booting."""
        with mock.patch.object(tour, "_idb_probe", return_value=probe), \
                mock.patch.object(tour, "_boot", side_effect=_BootReached):
            try:
                tour.run(dict(self.CHOICE), argparse.Namespace())
            except _BootReached:
                return "booted"
        return "returned"

    def test_axe_makes_a_broken_idb_hid_ready(self) -> None:
        self.set_env(AUTONOM_AXE=str(FAKE_AXE))
        hid = tour.ios_hid_readiness(BROKEN_HID)
        self.assertEqual((hid["ready"], hid["backend"]), (True, "axe"))
        self.assertEqual(self._preflight(BROKEN_HID), "booted")

    def test_selected_axe_is_ready_even_when_idb_hid_works(self) -> None:
        self.set_env(AUTONOM_AXE=str(FAKE_AXE), AUTONOM_IOS_HID="axe")
        self.assertEqual(tour.ios_hid_readiness(READY_IDB)["backend"], "axe")

    def test_broken_hid_without_axe_is_refused_before_booting(self) -> None:
        """Before the fix a ready idb state passed the preflight whatever its
        HID said, so the tour booted a simulator it could not tap."""
        with self.assertRaises(errors.AutonomError) as caught:
            self._preflight(BROKEN_HID)
        self.assertEqual(caught.exception.code, errors.IOS_HID_FRAMEWORK_MISSING)
        self.assertIn("AXe", caught.exception.hint)
        self.assertIn("brew upgrade idb-companion", caught.exception.extra["fix"])

    def test_missing_idb_is_still_required_for_the_tree(self) -> None:
        self.set_env(AUTONOM_AXE=str(FAKE_AXE))
        with self.assertRaises(errors.AutonomError) as caught:
            self._preflight({"state": "missing", "error": "idb not found"})
        self.assertEqual(caught.exception.code, errors.IDB_REQUIRED)

    def test_working_idb_needs_no_axe(self) -> None:
        self.assertEqual(tour.ios_hid_readiness(READY_IDB)["backend"], "idb")
        self.assertEqual(self._preflight(READY_IDB), "booted")


class TourRepairTimelineTests(_Sandbox):
    def test_repair_brief_sees_status_of_executed_steps(self) -> None:
        """Step 2 is a failed retry attempt that was superseded; the prefix to
        replay ends at step 1. Index-only rows made it step 2."""
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        record = {"session_id": "s_tour", "artifacts_dir": str(artifacts)}
        run_dir = artifacts / "flows" / "fr_x"
        run_dir.mkdir(parents=True)
        events = run_dir / "events.ndjson"
        events.write_text("", encoding="utf-8")
        flow = self.root / "walk.yaml"
        flow.write_text(_CHILD, encoding="utf-8")
        rows = [
            {"index": 1, "command": "launchApp", "status": "passed", "hook": None},
            {"index": 2, "command": "tapOn", "status": "failed", "error_code": "x"},
            {"index": 3, "command": "tapOn", "status": "failed",
             "error_code": errors.FLOW_ASSERTION_TIMEOUT},
        ]
        failure = {"step_index": 3, "command": "tapOn", "line": 5, "flow": str(flow),
                   "error_code": errors.FLOW_ASSERTION_TIMEOUT,
                   "failure_class": "test_failure", "error": "not found"}
        summary = tour._summarize(  # noqa: SLF001
            choice={"platform": ANDROID, "target_id": "emulator-5554"},
            target=Target(ANDROID, "emulator-5554", str(FAKE_ADB)), record=record,
            title="t", app_id="com.example.app", run_id="fr_x", status="failed",
            step_rows=rows, failure=failure, flow_path=flow, events_path=str(events),
            boot_detail={}, started=0.0)
        commands = " ".join(summary["repair"]["commands"])
        self.assertIn("--until-step 1", commands)
        self.assertNotIn("--until-step 2", commands)


# --- IOS-003: actuation through the HID selection ------------------------------------


class ActuationBackendTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.set_env(AUTONOM_AXE=str(FAKE_AXE), AUTONOM_IOS_HID="axe")
        self.ios = Target(IOS, UDID, str(FAKE_SIMCTL))

    def test_long_press_goes_through_axe(self) -> None:
        """Before: ui.long_press called idb directly — AXe was never used."""
        self.assertEqual(ui.long_press(self.ios, 10, 20, 800, screen=(400, 800)), "axe")
        self.assertEqual(self.calls("axe"),
                         [["touch", "-x", "10", "-y", "20", "--down", "--up",
                           "--delay", "0.8", "--udid", UDID]])
        self.assertEqual(ui_ios.last_backend(), "axe")

    def test_double_tap_goes_through_axe_twice(self) -> None:
        self.assertEqual(ui.double_tap(self.ios, 10, 20, screen=(400, 800)), "axe")
        self.assertEqual(self.calls("axe"),
                         [["tap", "-x", "10", "-y", "20", "--udid", UDID]] * 2)

    def test_every_verb_names_its_backend(self) -> None:
        screen = (400, 800)
        self.assertEqual(ui.tap(self.ios, 1, 2, screen=screen), "axe")
        self.assertEqual(ui.swipe(self.ios, 1, 2, 3, 4, 0.2, screen=screen), "axe")
        self.assertEqual(ui.type_text(self.ios, "hi"), "axe")
        self.assertEqual(ui.press_key(self.ios, "HOME"), "axe")
        android = Target(ANDROID, "emulator-5554", str(FAKE_ADB), {"serial": "emulator-5554"})
        self.assertEqual(ui.tap(android, 1, 2, screen=(1080, 1920)), "adb")


# --- IOS-007 -----------------------------------------------------------------------------


class LogStreamTests(_Sandbox):
    def test_session_stream_is_bounded_and_filtered_exactly(self) -> None:
        line = json.dumps({"eventMessage": "x" * 90})
        self.state.write_text(json.dumps({"ios_log": [line] * 400}), encoding="utf-8")
        self.set_env(AUTONOM_IOS_LOG_MAX_MB="0.004")  # ~4 KB
        destination = self.root / "logs" / "stream.ndjson"
        pid = logs.start_log_stream(Target(IOS, UDID, str(FAKE_SIMCTL)), destination,
                                    bundle_id="com.example.app")
        self.assertIsNotNone(pid)
        os.waitpid(pid, 0)
        # Before: the stream was written unbounded straight to the file.
        self.assertLessEqual(destination.stat().st_size, ios_simctl.log_max_bytes())
        self.assertTrue(destination.with_name("stream.ndjson.1").exists())
        stream = next(argv for argv in self.calls("simctl") if "stream" in argv)
        predicate = stream[stream.index("--predicate") + 1]
        self.assertEqual(predicate, 'subsystem == "com.example.app"')

    def test_predicate_uses_a_distinctive_name_only(self) -> None:
        self.assertNotIn("CONTAINS", ios_simctl.log_predicate("com.example.app"))
        self.assertNotIn("CONTAINS", ios_simctl.log_predicate("com.example.io"))
        self.assertIn('processImagePath CONTAINS "shop"',
                      ios_simctl.log_predicate("com.example.shop"))
        # An executable name alone matches it as the image's last component
        # (every Flutter app is a `Runner`, so the installed bundle path is
        # preferred when known: tests/test_fix_ios_dev.py).
        self.assertIn('processImagePath ENDSWITH "/Runner"',
                      ios_simctl.log_predicate("com.example.app", executable="Runner"))
        self.assertEqual(logs._ios_predicate("com.example.app"),  # noqa: SLF001
                         ios_simctl.log_predicate("com.example.app"))

    def test_stream_file_filter_ignores_a_generic_leaf(self) -> None:
        stream = self.root / "stream.ndjson"
        stream.write_text(
            json.dumps({"processImagePath": "/x/Other.app/Other", "eventMessage": "noise"})
            + "\n" + json.dumps({"subsystem": "com.example.app", "eventMessage": "mine"})
            + "\n", encoding="utf-8")
        entries, _ = logs.tail_ios(Target(IOS, UDID, str(FAKE_SIMCTL)), stream_path=stream,
                                   package="com.example.app")
        self.assertEqual(len(entries), 1)
        self.assertIn("mine", entries[0]["line"])


# --- FLOW-006 --------------------------------------------------------------------------


class DiscoverySkippedTests(unittest.TestCase):
    def test_skipped_files_are_reported_with_the_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "good.yaml").write_text(_CHILD, encoding="utf-8")
            (directory / "typo.yaml").write_text(
                _CHILD.replace("autonom.dev/flow/v1", "autonom.dev/flows/v1"), encoding="utf-8")
            (directory / "selectors.yaml").write_text(
                "schema: autonom.selectors/v1\nselectors: []\n", encoding="utf-8")
            overlay = directory / ".autonom/apps/com.example.app/subflows"
            overlay.mkdir(parents=True)
            (overlay / "copy.yaml").write_text(_CHILD, encoding="utf-8")
            flows, skipped = validator.discover_with_skipped(directory)
            self.assertEqual(flows, [directory / "good.yaml"])
            self.assertEqual(flows, validator.discover(directory))
            reasons = {Path(item["file"]).name: item["reason"] for item in skipped}
            self.assertEqual(reasons, {"typo.yaml": "non_flow_schema",
                                       "selectors.yaml": "non_flow_schema",
                                       "copy.yaml": "app_skill_overlay"})
            warning = validator.skipped_warning(skipped)
            self.assertEqual(warning["code"], "flow_files_skipped")
            self.assertIn("skipped 2 file(s) (non-Flow schema: autonom.dev/flows/v1, "
                          "autonom.selectors/v1)", warning["message"])
            self.assertIsNone(validator.skipped_warning(
                [item for item in skipped if item["reason"] == "app_skill_overlay"]))


# --- doctor tests are hermetic ------------------------------------------------------------


class DoctorHermeticTests(EnvSandboxMixin, unittest.TestCase):
    def test_doctor_runs_reach_no_host_tool(self) -> None:
        """Sentinels on the inherited PATH and in AUTONOM_AXE/_IDB_COMPANION:
        before the fix `_run` passed both through, so doctor ran them."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shims = Path(tmp.name) / "host"
        shims.mkdir()
        trace = Path(tmp.name) / "trace.log"
        for name in ("adb", "xcrun", "idb", "idb_companion", "axe", "xcode-select"):
            shim = shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{trace}"\nexit 1\n',
                            encoding="utf-8")
            shim.chmod(0o755)
        self.set_env(PATH=f"{shims}{os.pathsep}{os.environ.get('PATH', '')}",
                     AUTONOM_AXE=str(shims / "axe"),
                     AUTONOM_IDB_COMPANION="127.0.0.1:10882",
                     AUTONOM_IOS_HID="axe",
                     AUTONOM_IDB_STATE_FILE=str(Path(tmp.name) / "state"))
        case = test_doctor.DoctorTests("test_clean_host_reports_no_orphans")
        result = case._run("--adb", str(FAKE_ADB))  # noqa: SLF001
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["tools"]["adb"]["state"], "ok")
        self.assertNotIn("AUTONOM_AXE", report["overrides"])
        self.assertFalse(trace.exists() and trace.read_text(encoding="utf-8"),
                         "a doctor test reached a host tool")


if __name__ == "__main__":
    unittest.main()
