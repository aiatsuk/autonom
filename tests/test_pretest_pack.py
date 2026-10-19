"""Ticket pre-test pack, summaries, review and pack verification.

Module-level tests (tp-012..tp-016, tp-018) under a temporary ``AUTONOM_HOME``
with a fake Android target: device reads (crash list, logcat, metrics) are
patched, while the journal, screenshots, trees, action details, captured
traffic and flow runs are real files in the temporary session.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import actions as actions_mod  # noqa: E402
from autonom_lib import contracts, device_state, errors, logs  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib.metrics import snapshot as snapshot_mod  # noqa: E402
from autonom_lib.pretest import checks as checks_mod  # noqa: E402
from autonom_lib.pretest import findings as findings_mod  # noqa: E402
from autonom_lib.pretest import kit as kit_mod  # noqa: E402
from autonom_lib.pretest import pack as pack_mod  # noqa: E402
from autonom_lib.pretest import review as review_mod  # noqa: E402
from autonom_lib.pretest import store  # noqa: E402
from autonom_lib.pretest import summary as summary_mod  # noqa: E402
from autonom_lib.pretest import view as view_mod  # noqa: E402

APP = "com.example.app"
TOKEN = "sk-planted-TOKEN-0123456789abcdef"
PASSWORD = "hunter2-typed-secret"
CANVAS_PASSWORD = "CanvasTypedHunter9"
CANVAS_RECORDED = "canvas-recorded-hunter7"
INPUT_VALUE = "value-field-hunter5"
SCHEMAS = ROOT / "schemas"


def iso_now() -> str:
    return contracts.utc_now()


def tree_state(root: Path) -> dict[str, tuple[str, int]]:
    """Digest and mtime of every file below ``root``."""
    return {path.relative_to(root).as_posix(): (contracts.sha256_file(path),
                                                path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()}


class PackBase(unittest.TestCase):
    """A temporary AUTONOM_HOME, a session on a fake Android target and an
    active pre-test whose ticket carries a planted bearer token."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.artifacts = self.root / "home/sessions/s_test"
        for name in ("shots", "trees", "logs", "network", "crashes", "flows", "actions",
                     "recordings"):
            (self.artifacts / name).mkdir(parents=True)
        self.session = {"session_id": "s_test", "artifacts_dir": str(self.artifacts),
                        "platform": "android", "target_id": "emulator-5554",
                        "app_id": APP, "network": {"enabled": True}}
        self.target = SimpleNamespace(platform="android", target_id="emulator-5554",
                                      tool="fake-adb", aliases={"serial": "emulator-5554"})
        self.crashes: list = []
        for target, name, fake in (
                (device_state, "crash_list", lambda target, app_id=None: list(self.crashes)),
                (logs, "tail_logcat_detailed", lambda *a, **k: {
                    "entries": [], "warnings": [], "filter": "uid", "uid": 1, "pid": None}),
                (snapshot_mod, "take", lambda target, app_id: ({
                    "ok": True, "platform": "android", "pid": 42,
                    "memory": {"total_pss_kb": 100_000}, "cpu": {"process_percent": 5.0},
                    "captured_at": iso_now()}, "raw"))):
            patcher = mock.patch.object(target, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record = self.create()

    # --- fixtures -------------------------------------------------------------

    def create(self, **overrides) -> dict:
        args = dict(target=self.target,
                    ticket_text=f"# WB-7 Cart total\nAuthorization: Bearer {TOKEN}\n"
                                "Removing an item updates the total.",
                    ticket_ref="WB-7", title=None, repo=None, base="main", head=None,
                    flows_dir=None, app_id=APP, budget=None,
                    context={"changed_files": [{"path": "lib/cart.dart", "status": "M",
                                                "ranges": [[10, 12]]}],
                             "diff_truncated": False})
        args.update(overrides)
        return store.create(self.session, **args)

    def add_check(self, title: str, *, kind: str = "ticket", case: str | None = None,
                  begin: bool = True, verdict: str | None = "pass",
                  run_id: str | None = None, during=None) -> str:
        check_id = checks_mod.add(self.session, self.record, title=title, kind=kind,
                                  case=case)["check_id"]
        if begin:
            checks_mod.begin(self.session, self.record, check_id)
            if during:
                during(check_id)
            if verdict:
                checks_mod.end(self.session, self.record, check_id, verdict=verdict,
                               run_id=run_id)
        return check_id

    def shot(self, label: str) -> str:
        """A screenshot filed like ``capture_evidence`` does (attribution included)."""
        attribution = store.attribution(self.session)
        task = attribution.get("check_id") or "misc"
        folder = self.artifacts / "shots" / task
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{len(list(folder.iterdir())) + 1:04d}_{label}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + label.encode() + os.urandom(16))
        row = {"captured_at": iso_now(), "label": label, "task": task,
               "session_id": "s_test", "platform": "android", "mocks_active": [],
               "file": str(path.relative_to(self.artifacts)), "width": 1080,
               "height": 2400, **attribution}
        index = self.artifacts / "shots" / "index.jsonl"
        with index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        return str(len(index.read_text(encoding="utf-8").splitlines()))

    def typed_password(self) -> None:
        journal_mod.append(self.session, {"kind": "action", "verb": "ui type",
                                          "argv": ["ui", "type", PASSWORD], "ok": True})
        actions_mod.record_detail(self.session, "type", {
            "kind": "type", "sensitive": False, "text": PASSWORD,
            "text_len": len(PASSWORD)})
        # Canvas typing: the dispatched op and the recorded kind are both "text"
        actions_mod.record_detail(self.session, "canvas-text", {
            "kind": "text", "origin": "human", "canvas": True, "sensitive": False,
            "text": CANVAS_PASSWORD, "text_len": len(CANVAS_PASSWORD)})
        actions_mod.record_detail(self.session, "canvas-text", {
            "kind": "text", "origin": "human", "canvas": True, "transport": "scrcpy",
            "sensitive": False, "text_len": len(CANVAS_RECORDED), "text": CANVAS_RECORDED})
        # any other input action that carries the typed value as "value"
        actions_mod.record_detail(self.session, "set-value", {
            "kind": "set_text", "value": INPUT_VALUE, "sensitive": False})
        journal_mod.append(self.session, {"kind": "action", "verb": "ui tap",
                                          "argv": ["ui", "tap", "--text", "Remove"],
                                          "ok": True})

    def tree(self) -> str:
        path = self.artifacts / "trees" / f"{len(list((self.artifacts / 'trees').iterdir())) + 1:04d}_120000.json"
        path.write_text(json.dumps({"nodes": [{"text": "Cart", "password": PASSWORD}]}),
                        encoding="utf-8")
        return path.name

    def failed_request(self, flow_id: str = "fl-1") -> str:
        now = time.time()
        row = {"id": flow_id, "started_at": iso_now(), "started_at_ms": int(now * 1000),
               "method": "GET", "url": "https://api.example.com/api/cart",
               "host": "api.example.com", "path": "/api/cart", "status": 500,
               "mocked": False, "mock_id": None,
               "request": {"headers": {"Authorization": f"Bearer {TOKEN}"}},
               "response": {"status_code": 500, "body": f"token={TOKEN}"}}
        with (self.artifacts / "network" / "flows.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return flow_id

    def flow_run(self, run_id: str = "fr_1") -> str:
        flows = self.root / "workspace"
        flows.mkdir(exist_ok=True)
        (flows / "cart.yaml").write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: Cart\n"
            "id: cart-v1\n---\n- launchApp\n- tapOn: Remove\n", encoding="utf-8")
        run_dir = self.artifacts / "flows" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "events.ndjson").write_bytes(b'{"event":"run.start","seq":1}\n')
        (run_dir / "step-1-shot.png").write_bytes(b"\x89PNG-run-" + run_id.encode())
        manifest = {
            "schema_version": 3, "session_id": "s_test", "run_id": run_id,
            "attempt_id": f"attempt_{run_id}", "flow_id": "cart-v1", "flow_name": "Cart",
            "flow_path": str(flows / "cart.yaml"), "app_id": APP, "platform": "android",
            "target_id": "emulator-5554", "status": "passed", "execution_status": "passed",
            "proof_verdict": "pass", "started_at_ms": 1000, "finished_at_ms": 1300,
            "steps": [{"index": 1, "step_id": "src_a:1", "source_id": "src_a",
                       "command": "launchApp", "status": "passed", "started_at_ms": 1000,
                       "finished_at_ms": 1300, "duration_ms": 300}],
            "artifacts": [f"flows/{run_id}/events.ndjson", f"flows/{run_id}/step-1-shot.png"],
            "artifact_steps": [{"path": f"flows/{run_id}/step-1-shot.png",
                                "kind": "screenshot", "step_index": 1, "step_id": "src_a:1"}],
            "environment": {"platform": "android"},
            "reproduction": f"autonom flow run {flows / 'cart.yaml'}",
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return run_id

    def crash(self, name: str = "app-crash-1") -> None:
        (self.artifacts / "crashes" / f"{name}.txt").write_text(
            f"Fatal in CartScreen\napi token: {TOKEN}\n", encoding="utf-8")
        self.crashes.append({"name": name, "date": iso_now()})

    def build(self, **kwargs) -> dict:
        return pack_mod.build(self.session, self.record, target=self.target, **kwargs)

    def full_pretest(self) -> dict:
        """Two checks (one with a finding and a screenshot, one with a flow
        run), a typed password, a tree, a 500 with an Authorization header
        and a new crash. Returns the ids."""
        ids: dict = {}

        def first(check_id: str) -> None:
            ids["shot"] = self.shot("cart-total")
            self.typed_password()
            ids["tree"] = self.tree()
            ids["request"] = self.failed_request()
            self.crash()

        ids["chk1"] = self.add_check("Total updates after removing", during=first,
                                     verdict="fail")
        ids["finding"] = findings_mod.add(
            self.session, self.record, check_id=ids["chk1"], severity="major",
            title="Total is stale", actual="The total still shows 30",
            expected="The total shows 20",
            evidence=[f"shot:{ids['shot']}", f"tree:{ids['tree']}",
                      f"request:{ids['request']}"])["finding_id"]
        ids["run"] = self.flow_run()

        def second(check_id: str) -> None:
            ids["log_from"] = iso_now()
            recording = self.artifacts / "recordings" / "cart.mp4"
            recording.write_bytes(b"\x00\x00\x00\x18ftypmp42-cart")
            # the file clock can trail the wall clock by a tick: place the
            # recording well inside the check window
            time.sleep(0.02)
            os.utime(recording, (time.time(), time.time()))
            time.sleep(0.02)
            ids["log_to"] = iso_now()

        ids["chk2"] = self.add_check("Regression: cart flow", kind="regression",
                                     run_id=ids["run"], during=second)
        ids["finding2"] = findings_mod.add(
            self.session, self.record, check_id=ids["chk2"], severity="minor",
            title="Crash log during flow", actual="The flow logged a crash",
            evidence=[f"log:{ids['log_from']}..{ids['log_to']}", f"run:{ids['run']}",
                      "crash:app-crash-1", "action:0001_type"])["finding_id"]
        return ids

    def assert_code(self, code: str, fn, *args, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        payload = caught.exception.as_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], code)
        self.assertTrue(payload.get("hint"), f"{code} has no hint")
        json.dumps(payload)
        return caught.exception

    def read(self, pack: Path, name: str):
        return json.loads((pack / name).read_text(encoding="utf-8"))


# --- tp-013 --------------------------------------------------------------------------


class PackTests(PackBase):
    def test_pack_layout_verify_tamper_and_idempotent_repack(self) -> None:
        """tp-013-S01."""
        ids = self.full_pretest()
        result = self.build()
        pack = Path(result["pack"])
        self.assertEqual(pack, store.pretest_dir(self.session, self.record["pretest_id"])
                         / "pack")
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["verdict"], "fail")
        for name in ("manifest.json", "pretest.json", "ticket.md", "context.json",
                     "baseline.json", "budget.json", "health.json", "checks.ndjson",
                     "findings.ndjson", f"checks/{ids['chk1']}.json",
                     f"findings/{ids['finding']}.json", f"runs/{ids['run']}/manifest.json",
                     "summary.json", "summary.md", "index.html", "integrity.json",
                     "finalized.json"):
            self.assertTrue((pack / name).is_file(), name)
        self.assertTrue(list((pack / "blobs" / "sha256").rglob("*")))
        manifest = self.read(pack, "manifest.json")
        self.assertEqual(manifest["kind"], "autonom.pretest-pack/v1")
        self.assertEqual(manifest["runs"], [ids["run"]])
        self.assertEqual(pack_mod.manifest_failures(manifest), [])
        listed = {item["path"] for item in self.read(pack, "integrity.json")["files"]}
        self.assertIn("manifest.json", listed)
        self.assertIn(f"runs/{ids['run']}/integrity.json", listed)
        self.assertTrue(pack_mod.is_pack(pack))

        # the record is packed, the pointer cleared, the inner record says packed
        session_record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(session_record["status"], "packed")
        self.assertEqual(session_record["pack"],
                         {"path": str(pack), "integrity_sha256": result["integrity_sha256"]})
        self.assertIsNone(store.active_ids(self.session))
        inner = store.find_pack_record(pack)
        self.assertEqual(inner["status"], "packed")
        self.assertEqual(self.read(pack, "finalized.json")["integrity_sha256"],
                         result["integrity_sha256"])

        verified = pack_mod.verify(pack)
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["nested"], [{"run_id": ids["run"], "ok": True}])

        # a second pack: same digest, nothing rewritten
        before = tree_state(pack)
        again = pack_mod.build(self.session, self.record, target=self.target)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["integrity_sha256"], result["integrity_sha256"])
        self.assertEqual(again["pack"], str(pack))
        self.assertEqual(again["verdict"], "fail")
        self.assertEqual(tree_state(pack), before)
        # the same explicit --out is idempotent too
        self.assertTrue(pack_mod.build(self.session, self.record, out=pack)["idempotent"])

        # one changed blob byte
        blob = sorted(p for p in (pack / "blobs").rglob("*") if p.is_file())[0]
        original = blob.read_bytes()
        blob.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": blob.relative_to(pack).as_posix(), "reason": "digest-mismatch"},
                      failed.extra["failures"])
        blob.write_bytes(original)
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_nested_bundle_tamper_is_reported_with_its_run(self) -> None:
        """tp-013-S01 (nested): a changed byte inside runs/<id>/ fails both the
        pack catalogue and the nested bundle's own verification."""
        run_id = self.flow_run("fr_nested")
        self.add_check("Flow", kind="regression", run_id=run_id)
        pack = Path(self.build()["pack"])
        model = pack / "runs" / run_id / "model" / "report.json"
        model.write_bytes(model.read_bytes().replace(b"cart-v1", b"cart-v2", 1))
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        paths = {item["path"] for item in failed.extra["failures"]}
        self.assertIn(f"runs/{run_id}/model/report.json", paths)
        self.assertEqual(failed.extra["nested"], [{"run_id": run_id, "ok": False}])

    def test_removed_nested_run_and_unlisted_core_file_fail_verify(self) -> None:
        run_id = self.flow_run("fr_gone")
        self.add_check("Flow", kind="regression", run_id=run_id)
        pack = Path(self.build()["pack"])
        import shutil
        shutil.rmtree(pack / "runs" / run_id)
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": f"runs/{run_id}/", "reason": "missing"},
                      failed.extra["failures"])

    def test_open_check_is_ended_inconclusive_at_pack_time(self) -> None:
        check_id = self.add_check("Still open", verdict=None)
        result = self.build()
        state = checks_mod.fold(self.session, self.record)[0]
        self.assertEqual((state["check_id"], state["state"], state["verdict"], state["note"]),
                         (check_id, "ended", "inconclusive", "open at pack time"))
        self.assertEqual(result["verdict"], "inconclusive")
        summary = self.read(Path(result["pack"]), "summary.json")
        self.assertEqual(summary["checks"][0]["note"], "open at pack time")
        self.assertIsNotNone(summary["ended_at"])

    def test_stopped_pretest_can_be_packed(self) -> None:
        self.add_check("Done")
        store.stop(self.session)
        record = store.load(self.session, self.record["pretest_id"])
        result = pack_mod.build(self.session, record, target=None)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(store.load(self.session, record["pretest_id"])["status"], "packed")
        health = self.read(Path(result["pack"]), "summary.json")["health"]
        self.assertEqual(health["crashes"]["state"], "unavailable")

    def test_failed_pack_attempt_does_not_fix_the_end_time(self) -> None:
        """Contract 12.6: ended_at and timing.wall_s come from the successful pack."""
        self.add_check("Done")
        with mock.patch.object(view_mod, "render", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.build()
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(record["status"], "active")
        self.assertIsNone(record["ended_at"])
        failed_at = iso_now()
        time.sleep(1.1)
        result = self.build()
        summary = self.read(Path(result["pack"]), "summary.json")
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(record["status"], "packed")
        self.assertEqual(record["ended_at"], summary["ended_at"])
        self.assertEqual(store.find_pack_record(result["pack"])["ended_at"],
                         summary["ended_at"])
        self.assertGreater(checks_mod.parse_ts(summary["ended_at"]),
                           checks_mod.parse_ts(failed_at))
        began = checks_mod.parse_ts(record["created_at"])
        self.assertAlmostEqual(summary["timing"]["wall_s"],
                               (checks_mod.parse_ts(summary["ended_at"]) - began)
                               .total_seconds(), places=2)
        self.assertGreaterEqual(summary["timing"]["wall_s"], 1.0)

    def test_stopped_pretest_keeps_its_stop_time(self) -> None:
        self.add_check("Done")
        stopped = store.stop(self.session)
        time.sleep(1.1)
        result = pack_mod.build(self.session, stopped, target=None)
        summary = self.read(Path(result["pack"]), "summary.json")
        self.assertEqual(summary["ended_at"], stopped["ended_at"])
        self.assertEqual(store.load(self.session, stopped["pretest_id"])["ended_at"],
                         stopped["ended_at"])

    def test_packed_pretest_refuses_changes(self) -> None:
        check_id = self.add_check("Done")
        self.build()
        self.assert_code(errors.PRETEST_FINALIZED, checks_mod.add, self.session,
                         self.record, title="Late", kind="ticket")
        self.assert_code(errors.PRETEST_FINALIZED, findings_mod.add, self.session,
                         self.record, check_id=check_id, severity="minor", title="Late",
                         actual="x")

    def test_pack_files_are_owner_only(self) -> None:
        self.full_pretest()
        pack = Path(self.build()["pack"])
        self.assertEqual(stat.S_IMODE(pack.stat().st_mode), 0o700)
        for path in pack.rglob("*"):
            want = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), want, path)
        leftovers = [p.name for p in pack.parent.iterdir() if ".staging-" in p.name
                     or p.name.startswith(".pack-runs-")]
        self.assertEqual(leftovers, [])

    def test_unknown_run_is_reported_not_fatal(self) -> None:
        self.add_check("Flow", kind="regression", run_id="fr_missing")
        result = self.build()
        manifest = self.read(Path(result["pack"]), "manifest.json")
        self.assertEqual(manifest["runs"], [])
        self.assertEqual(manifest["runs_unavailable"][0]["run_id"], "fr_missing")
        self.assertIn("run:fr_missing", [item["evidence"] for item in result["warnings"]])
        self.assertTrue(pack_mod.verify(result["pack"])["ok"])

    def test_explicit_out_directory(self) -> None:
        self.add_check("Done")
        out = self.root / "exports" / "wb-7"
        result = self.build(out=out)
        self.assertEqual(result["pack"], str(out.resolve()))
        self.assertTrue(pack_mod.verify(out)["ok"])

    def test_empty_out_directory_is_used(self) -> None:
        self.add_check("Done")
        out = self.root / "empty"
        out.mkdir()
        self.assertTrue(pack_mod.verify(self.build(out=out)["pack"])["ok"])


class PackRefusalTests(PackBase):
    def test_without_session(self) -> None:
        self.assert_code(errors.NO_ACTIVE_SESSION, pack_mod.build, None, self.record)
        self.assert_code(errors.NO_ACTIVE_SESSION, pack_mod.build, {}, self.record)

    def test_without_record(self) -> None:
        self.assert_code(errors.INVALID_VALUE, pack_mod.build, self.session, None)
        self.assert_code(errors.INVALID_VALUE, pack_mod.build, self.session, {"x": 1})

    def test_unknown_pretest(self) -> None:
        self.assert_code(errors.NO_ACTIVE_PRETEST, pack_mod.build, self.session,
                         {"pretest_id": "pt_20260101T000000_abcdef"})

    def test_packed_with_another_out_is_refused(self) -> None:
        self.add_check("Done")
        first = self.build()
        error = self.assert_code(errors.PRETEST_FINALIZED, self.build,
                                 out=self.root / "elsewhere")
        self.assertEqual(error.extra["pack"], first["pack"])
        self.assertFalse((self.root / "elsewhere").exists())

    def test_non_empty_out_is_refused_before_anything_changes(self) -> None:
        check_id = self.add_check("Open one", verdict=None)
        out = self.root / "busy"
        out.mkdir()
        (out / "keep.txt").write_text("mine", encoding="utf-8")
        self.assert_code(errors.REPORT_BUNDLE_FINALIZED, self.build, out=out)
        self.assertEqual((out / "keep.txt").read_text(encoding="utf-8"), "mine")
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(record["status"], "active")
        self.assertEqual(checks_mod.fold(self.session, record)[0]["state"], "open")
        self.assertFalse((store.pretest_dir(self.session, record["pretest_id"])
                          / "health.json").exists())
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], check_id))

    def test_unresolvable_out_is_refused_before_anything_changes(self) -> None:
        check_id = self.add_check("Open one", verdict=None)
        for out in ("~no-such-user-x9q/pack", "bad\0out"):
            self.assert_code(errors.REPORT_BUNDLE_FINALIZED, self.build, out=out)
        self.assertEqual(store.active_ids(self.session), (self.record["pretest_id"], check_id))
        self.assertEqual(store.load(self.session, self.record["pretest_id"])["status"],
                         "active")

    def test_out_that_is_a_file_is_refused(self) -> None:
        out = self.root / "file.txt"
        out.write_text("x", encoding="utf-8")
        self.assert_code(errors.REPORT_BUNDLE_FINALIZED, self.build, out=out)

    def test_out_holding_another_pretests_pack_is_refused(self) -> None:
        self.add_check("Done")
        first = Path(self.build()["pack"])
        self.record = self.create()
        self.add_check("Second")
        error = self.assert_code(errors.REPORT_BUNDLE_FINALIZED, self.build, out=first)
        self.assertIn("pt_", error.message)
        self.assertEqual(store.load(self.session, self.record["pretest_id"])["status"],
                         "active")

    def test_failure_while_staging_leaves_no_pack_and_record_unpacked(self) -> None:
        self.add_check("Done")
        with mock.patch.object(view_mod, "render", side_effect=errors.AutonomError(
                errors.REPORT_MODEL_INVALID, "boom", hint="x")):
            self.assert_code(errors.REPORT_MODEL_INVALID, self.build)
        directory = store.pretest_dir(self.session, self.record["pretest_id"])
        self.assertFalse((directory / "pack").exists())
        self.assertEqual([p.name for p in directory.iterdir() if p.name.startswith(".pack")],
                         [])
        self.assertNotEqual(store.load(self.session, self.record["pretest_id"])["status"],
                            "packed")
        # a retry succeeds
        self.assertTrue(pack_mod.verify(self.build()["pack"])["ok"])

    def test_crash_between_finalize_and_mark_is_adopted(self) -> None:
        self.add_check("Done")
        with mock.patch.object(store, "mark_packed", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                self.build()
        pack = store.pretest_dir(self.session, self.record["pretest_id"]) / "pack"
        self.assertTrue((pack / "finalized.json").is_file())
        self.assertNotEqual(store.load(self.session, self.record["pretest_id"])["status"],
                            "packed")
        before = tree_state(pack)
        adopted = self.build()
        self.assertTrue(adopted["idempotent"])
        self.assertEqual(tree_state(pack), before)
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(record["status"], "packed")
        self.assertEqual(record["pack"]["integrity_sha256"], adopted["integrity_sha256"])

    def crashed_pack(self) -> tuple[Path, dict]:
        """A pack finalized while marking the record failed (a crash between
        the two): the record stays active and mutable."""
        self.add_check("Done")
        with mock.patch.object(store, "mark_packed", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                self.build()
        pack = store.pretest_dir(self.session, self.record["pretest_id"]) / "pack"
        self.assertTrue((pack / "finalized.json").is_file())
        self.record.update(store.load(self.session, self.record["pretest_id"]))
        self.assertEqual(self.record["status"], "active")
        return pack, tree_state(pack)

    def assert_stale_refusal(self, pack: Path, before: dict, differs: str) -> None:
        error = self.assert_code(errors.REPORT_BUNDLE_FINALIZED, self.build)
        self.assertTrue(error.extra["stale"])
        self.assertIn(differs, error.extra["differs"])
        # fu-013: the same hint as every refusal about an existing pack
        self.assertEqual(error.hint, pack_mod.FINALIZED_HINT)
        self.assertNotIn("again into", error.hint)
        self.assertEqual(tree_state(pack), before)
        self.assertEqual(store.load(self.session, self.record["pretest_id"])["status"],
                         "active")

    def test_crash_then_symlink_in_pack_is_refused_before_reading(self) -> None:
        """Contract 16: re-pack scans the crashed pack with lstat only before
        its manifest or record is opened."""
        pack, _before = self.crashed_pack()
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        (pack / "annotations").mkdir()
        (pack / "annotations" / "link").symlink_to(outside)
        with mock.patch.object(store, "find_pack_record") as record, \
                mock.patch.object(pack_mod, "is_pack") as identify:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, self.build)
        record.assert_not_called()
        identify.assert_not_called()
        self.assertIn({"path": "annotations/link", "reason": "symlink"},
                      failed.extra["failures"])
        self.assertEqual(store.load(self.session, self.record["pretest_id"])["status"],
                         "active")
        (pack / "annotations" / "link").unlink()
        self.assertTrue(self.build()["idempotent"])

    def test_crash_before_the_end_time_was_saved_is_adopted(self) -> None:
        self.add_check("Done")
        with mock.patch.object(pack_mod, "_seal", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                self.build()
        pack = store.pretest_dir(self.session, self.record["pretest_id"]) / "pack"
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual((record["status"], record["ended_at"]), ("active", None))
        before = tree_state(pack)
        adopted = self.build()
        self.assertTrue(adopted["idempotent"])
        self.assertEqual(tree_state(pack), before)
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(record["status"], "packed")
        self.assertEqual(record["ended_at"], store.find_pack_record(pack)["ended_at"])

    def test_crash_then_new_check_refuses_the_stale_pack(self) -> None:
        pack, before = self.crashed_pack()
        failed = self.add_check("Checked after the crash", verdict="fail")
        self.assert_stale_refusal(pack, before, "checks.ndjson")
        # packing into a new directory keeps the later work and its verdict
        result = self.build(out=self.root / "fresh-pack")
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["verdict"], "fail")
        summary = self.read(Path(result["pack"]), "summary.json")
        self.assertIn(failed, [check["check_id"] for check in summary["checks"]])
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual((record["status"], record["pack"]["path"]),
                         ("packed", result["pack"]))
        self.assertEqual(tree_state(pack), before)

    def test_crash_then_new_finding_refuses_the_stale_pack(self) -> None:
        pack, before = self.crashed_pack()
        findings_mod.add(self.session, self.record, check_id=None, severity="blocker",
                         title="Found after the crash", actual="Checkout crashes")
        self.assert_stale_refusal(pack, before, "findings.ndjson")

    def test_crash_then_record_change_refuses_the_stale_pack(self) -> None:
        pack, before = self.crashed_pack()
        self.record["title"] = "Renamed after the crash"
        store.save(self.session, self.record)
        self.assert_stale_refusal(pack, before, "pretest.json")

    def test_crash_then_removed_record_file_refuses_the_stale_pack(self) -> None:
        pack, before = self.crashed_pack()
        (store.pretest_dir(self.session, self.record["pretest_id"]) / "health.json").unlink()
        self.assert_stale_refusal(pack, before, "health.json")

    def test_crash_then_stop_still_adopts_the_matching_pack(self) -> None:
        """Contract 13.4: a stop after the crash changes status and ended_at
        only, so the sealed pack still matches and is adopted."""
        self.add_check("Done")
        # the crash comes before the pack's end time reached the record
        with mock.patch.object(pack_mod, "_seal", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                self.build()
        pack = store.pretest_dir(self.session, self.record["pretest_id"]) / "pack"
        before = tree_state(pack)
        sealed_end = store.find_pack_record(pack)["ended_at"]
        self.assertIsNone(store.load(self.session, self.record["pretest_id"])["ended_at"])
        time.sleep(1.1)
        stopped = store.stop(self.session)
        self.assertEqual(stopped["status"], "stopped")
        self.assertNotEqual(stopped["ended_at"], sealed_end)
        adopted = pack_mod.build(self.session, stopped, target=None)
        self.assertTrue(adopted["idempotent"])
        self.assertEqual(tree_state(pack), before)
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual((record["status"], record["ended_at"]), ("packed", sealed_end))
        self.assertEqual(record["pack"]["integrity_sha256"], adopted["integrity_sha256"])

    def test_crash_then_recomputed_health_with_same_content_is_adopted(self) -> None:
        pack, before = self.crashed_pack()
        health_file = store.pretest_dir(self.session, self.record["pretest_id"]) / "health.json"
        doc = json.loads(health_file.read_text("utf-8"))
        doc["computed_at"] = "2099-01-01T00:00:00.000Z"
        health_file.write_text(json.dumps(doc), encoding="utf-8")
        self.assertTrue(self.build()["idempotent"])
        self.assertEqual(tree_state(pack), before)

    def test_crash_then_changed_health_refuses_the_stale_pack(self) -> None:
        pack, before = self.crashed_pack()
        health_file = store.pretest_dir(self.session, self.record["pretest_id"]) / "health.json"
        doc = json.loads(health_file.read_text("utf-8"))
        doc["crashes"] = {"state": "available", "new": [{"name": "late", "time": None,
                                                          "check_id": None}]}
        health_file.write_text(json.dumps(doc), encoding="utf-8")
        self.assert_stale_refusal(pack, before, "health.json")

    def test_record_digest_is_sealed_in_the_manifest(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        sealed = self.read(pack, "manifest.json")["record_digest"]
        self.assertEqual(set(sealed), {"sha256", "record", "checks", "findings", "health"})
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(pack_mod.record_digest(self.session, record), sealed)
        # status, pack and ended_at are not part of it
        changed = dict(record, status="stopped", ended_at="2099-01-01T00:00:00.000Z",
                       pack=None)
        self.assertEqual(pack_mod.record_digest(self.session, changed), sealed)
        self.assertNotEqual(pack_mod.record_digest(self.session, dict(record, title="x")),
                            sealed)

    def test_pack_without_a_sealed_digest_is_stale(self) -> None:
        """A manifest that lacks the digest cannot prove it matches."""
        pack, _before = self.crashed_pack()
        self.assertEqual(pack_mod._pack_differences(self.session, self.record, pack), [])
        copy = self.root / "manifest-only"
        copy.mkdir()
        manifest = self.read(pack, "manifest.json")
        for digest in (None, "x", {"sha256": "0" * 64}):
            if digest is None:
                manifest.pop("record_digest", None)
            else:
                manifest["record_digest"] = digest
            (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(pack_mod._pack_differences(self.session, self.record, copy),
                             ["pretest.json", "checks.ndjson", "findings.ndjson",
                              "health.json"])
        (copy / "manifest.json").write_text("[1]", encoding="utf-8")
        self.assertEqual(len(pack_mod._pack_differences(self.session, self.record, copy)), 4)

    def test_packed_record_whose_pack_was_deleted(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        import shutil
        shutil.rmtree(pack)
        self.assert_code(errors.NOT_A_PRETEST_PACK, self.build)


class VerifyTests(PackBase):
    def test_not_a_pack(self) -> None:
        self.assert_code(errors.NOT_A_PRETEST_PACK, pack_mod.verify, self.root / "nothing")
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "manifest.json").write_text('{"kind": "other"}', encoding="utf-8")
        self.assert_code(errors.NOT_A_PRETEST_PACK, pack_mod.verify, plain)
        (plain / "manifest.json").write_text("{broken", encoding="utf-8")
        self.assertFalse(pack_mod.is_pack(plain))
        self.assertFalse(pack_mod.is_pack(self.root / "missing"))
        self.assertFalse(pack_mod.is_pack("\0bad"))
        for odd in ("\0bad", "~no-such-user-x9q/pack"):
            self.assert_code(errors.NOT_A_PRETEST_PACK, pack_mod.verify, odd)
        self.assertFalse(pack_mod.is_pack("~no-such-user-x9q/pack"))

    def test_missing_integrity_and_finalized(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        (pack / "finalized.json").unlink()
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": "finalized.json", "reason": "missing"},
                      failed.extra["failures"])
        (pack / "integrity.json").unlink()
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": "integrity.json", "reason": "missing"},
                      failed.extra["failures"])

    def test_changed_summary_and_escaping_entry(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        summary = pack / "summary.json"
        summary.write_bytes(summary.read_bytes().replace(b'"pass"', b'"fail"', 1))
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": "summary.json", "reason": "digest-mismatch"},
                      failed.extra["failures"])
        integrity = self.read(pack, "integrity.json")
        integrity["files"].append({"path": "../outside", "sha256": "0" * 64})
        (pack / "integrity.json").write_text(json.dumps(integrity), encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn("path-forbidden", {item["reason"] for item in failed.extra["failures"]})

    def test_core_file_dropped_from_catalogue(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        integrity = self.read(pack, "integrity.json")
        integrity["files"] = [item for item in integrity["files"]
                              if item["path"] != "summary.md"]
        (pack / "integrity.json").write_text(json.dumps(integrity), encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": "summary.md", "reason": "not-listed"},
                      failed.extra["failures"])

    def test_annotations_do_not_affect_verify(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        (pack / "annotations").mkdir()
        (pack / "annotations" / "free.json").write_text("{}", encoding="utf-8")
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_unlisted_files_and_symlinks_fail_verify(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        (pack / "findings").mkdir(exist_ok=True)
        (pack / "findings" / "f-99.json").write_text("{}", encoding="utf-8")
        (pack / "extra.txt").write_text("x", encoding="utf-8")
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "hidden.json").write_text("{}", encoding="utf-8")
        (pack / "linked-dir").symlink_to(outside, target_is_directory=True)
        (pack / "linked-file").symlink_to(pack / "summary.md")
        # contract 16: the symlinks stop verification before anything is read
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"],
                         [{"path": "linked-dir", "reason": "symlink"},
                          {"path": "linked-file", "reason": "symlink"},
                          {"path": "integrity.json", "reason": "digests-not-checked"}])
        for name in ("linked-dir", "linked-file"):
            (pack / name).unlink()
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        found = failed.extra["failures"]
        # this pack has no findings, so findings/ is a directory the catalogue
        # does not cover: it is reported itself (contract 14.2)
        for item in ({"path": "findings", "reason": "unlisted"},
                     {"path": "extra.txt", "reason": "unlisted"}):
            self.assertIn(item, found)
        self.assertFalse(any("hidden.json" in item["path"] for item in found))
        for name in ("extra.txt", "findings/f-99.json"):
            (pack / name).unlink()
        (pack / "findings").rmdir()
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # an unlisted file inside a listed directory is reported by its path
        (pack / "checks" / "chk-99.json").write_text("{}", encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"],
                         [{"path": "checks/chk-99.json", "reason": "unlisted"}])

    def test_os_metadata_files_are_ignored(self) -> None:
        """Contract 12.3."""
        self.full_pretest()
        pack = Path(self.build()["pack"])
        run = next(child for child in (pack / "runs").iterdir() if child.is_dir())
        blob_dir = next(p for p in (pack / "blobs" / "sha256").iterdir() if p.is_dir())
        planted = [pack / ".DS_Store", pack / "._summary.md", blob_dir / ".DS_Store",
                   run / "Thumbs.db", run / "desktop.ini", pack / "runs" / "._x"]
        for path in planted:
            path.write_bytes(b"\x00metadata")
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # any other unlisted file is still reported, also a hidden one and a
        # metadata name in another letter case (contract 17: exact names)
        (pack / ".hidden").write_text("x", encoding="utf-8")
        (run / "DS_Store").write_text("x", encoding="utf-8")
        (pack / "checks" / "THUMBS.DB").write_text("x", encoding="utf-8")
        (pack / "checks" / ".ds_store").write_text("x", encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        paths = {item["path"] for item in failed.extra["failures"]}
        self.assertEqual(paths, {".hidden", f"runs/{run.name}/DS_Store",
                                 "checks/THUMBS.DB", "checks/.ds_store"})
        for path in (pack / ".hidden", run / "DS_Store", pack / "checks" / "THUMBS.DB",
                     pack / "checks" / ".ds_store"):
            path.unlink()
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # a symlink with a metadata name is still a symlink
        (pack / ".DS_Store").unlink()
        (pack / ".DS_Store").symlink_to(pack / "summary.md")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"],
                         [{"path": ".DS_Store", "reason": "symlink"},
                          {"path": "integrity.json", "reason": "digests-not-checked"}])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this host")
    def test_os_metadata_names_count_only_for_regular_files(self) -> None:
        """Contract 13.3: a FIFO or directory named like OS metadata is reported."""
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        os.mkfifo(pack / ".DS_Store")
        # contract 16: a FIFO anywhere stops verification before anything is read
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"],
                         [{"path": ".DS_Store", "reason": "not-a-regular-file"},
                          {"path": "integrity.json", "reason": "digests-not-checked"}])
        (pack / ".DS_Store").unlink()
        (pack / "Thumbs.db").mkdir()
        (pack / "Thumbs.db" / "inside.txt").write_text("x", encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"],
                         [{"path": "Thumbs.db", "reason": "unlisted"}])

    def test_malformed_nested_and_catalogue_data_is_an_autonom_error(self) -> None:
        """Contract 13.2: non-object JSON in a nested bundle, a catalogue that
        is not a list and other malformed data are refusals with a hint."""
        run_id = self.flow_run("fr_bad")
        self.add_check("Flow", kind="regression", run_id=run_id)
        pack = Path(self.build()["pack"])
        nested = pack / "runs" / run_id
        originals = {name: (nested / name).read_bytes()
                     for name in ("manifest.json", "finalized.json", "model/report.json")}
        for name, bad in (("manifest.json", b"[1, 2]"), ("manifest.json", b"\xff\xfe"),
                          ("manifest.json", b"{broken"), ("finalized.json", b'"text"'),
                          ("model/report.json", b"[]")):
            (nested / name).write_bytes(bad)
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
            self.assertIn({"run_id": run_id, "ok": False}, failed.extra["nested"])
            self.assertTrue(any(item["path"].startswith(f"runs/{run_id}/")
                                for item in failed.extra["failures"]), name)
            (nested / name).write_bytes(originals[name])
            self.assertTrue(pack_mod.verify(pack)["ok"])
        # the pack's own catalogue: files that are not a list, unhashable paths
        integrity = pack / "integrity.json"
        original = integrity.read_bytes()
        for bad in ({"files": 5}, {"files": [{"path": ["x"], "sha256": "0" * 64}]},
                    {"files": "abc"}, ["not", "an", "object"]):
            integrity.write_text(json.dumps(bad), encoding="utf-8")
            self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        integrity.write_bytes(original)
        # a manifest whose runs are not a list
        manifest = pack / "manifest.json"
        doc = json.loads(manifest.read_text("utf-8"))
        doc["runs"] = "fr_bad"
        manifest.write_text(json.dumps(doc), encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": "manifest.json", "reason": "digest-mismatch"},
                      failed.extra["failures"])
        # any unexpected error inside verification is still a refusal
        manifest.write_text(json.dumps({**doc, "runs": [run_id]}), encoding="utf-8")
        with mock.patch.object(pack_mod, "_verify_pack", side_effect=TypeError("odd")):
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"], [{"path": "", "reason": "TypeError"}])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads anything")
    def test_unreadable_directory_in_pack_is_reported(self) -> None:
        self.full_pretest()
        pack = Path(self.build()["pack"])
        blob_dir = next(p for p in (pack / "blobs" / "sha256").iterdir() if p.is_dir())
        os.chmod(blob_dir, 0)
        self.addCleanup(os.chmod, blob_dir, 0o700)
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": blob_dir.relative_to(pack).as_posix(), "reason": "unreadable"},
                      failed.extra["failures"])

    def test_symlinked_integrity_or_finalized_is_reported(self) -> None:
        """Contract 12.4: both must be regular files, even with the right bytes."""
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        for name in ("integrity.json", "finalized.json"):
            moved = self.root / f"moved-{name}"
            (pack / name).replace(moved)
            (pack / name).symlink_to(moved)
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
            self.assertIn({"path": name, "reason": "symlink"}, failed.extra["failures"])
            (pack / name).unlink()
            moved.replace(pack / name)
            self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_unlisted_file_in_nested_run_fails_verify(self) -> None:
        self.full_pretest()
        pack = Path(self.build()["pack"])
        runs = sorted(child for child in (pack / "runs").iterdir() if child.is_dir())
        self.assertTrue(runs)
        (runs[0] / "planted.json").write_text("{}", encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": f"runs/{runs[0].name}/planted.json", "reason": "unlisted"},
                      failed.extra["failures"])

    def test_unlisted_directories_are_reported(self) -> None:
        """Contract 14.2: any directory that holds no listed file, empty or
        not, whatever its name, is reported; annotations/ is not."""
        self.full_pretest()
        pack = Path(self.build()["pack"])
        # a fresh pack holds no empty directory (nested bundles are pruned)
        empty = [current for current, dirs, names in os.walk(pack) if not dirs and not names]
        self.assertEqual(empty, [])
        self.assertTrue(pack_mod.verify(pack)["ok"])
        run = next(child for child in (pack / "runs").iterdir() if child.is_dir())
        blob_dir = next(p for p in (pack / "blobs" / "sha256").iterdir() if p.is_dir())
        planted = ["empty", ".DS_Store", "._x", "THUMBS.DB", "desktop.ini",
                   "blobs/sha256/zz", blob_dir.relative_to(pack).as_posix() + "/.DS_Store",
                   f"runs/{run.name}/extra", f"runs/{run.name}/Thumbs.db"]
        for relative in planted:
            (pack / relative).mkdir()
        (pack / "nested" / "deeper").mkdir(parents=True)
        (pack / "nested" / "deeper" / ".DS_Store").write_bytes(b"\x00")
        (pack / "annotations" / "drafts").mkdir(parents=True)
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        found = failed.extra["failures"]
        wanted = [{"path": relative, "reason": "unlisted"} for relative in planted]
        wanted.append({"path": "nested", "reason": "unlisted"})
        for item in wanted:
            self.assertIn(item, found)
        self.assertEqual(len(found), len(wanted), found)
        self.assertFalse(any(item["path"].startswith("annotations") for item in found))
        for relative in planted:
            (pack / relative).rmdir()
        (pack / "nested" / "deeper" / ".DS_Store").unlink()
        (pack / "nested" / "deeper").rmdir()
        (pack / "nested").rmdir()
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_prune_empty_dirs_keeps_files(self) -> None:
        root = self.root / "bundle"
        (root / "a" / "b" / "c").mkdir(parents=True)
        (root / "d").mkdir()
        (root / "d" / "keep.txt").write_text("x", encoding="utf-8")
        (root / "e" / "f").mkdir(parents=True)
        pack_mod._prune_empty_dirs(root)
        self.assertTrue(root.is_dir())
        self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob("*")),
                         ["d", "d/keep.txt"])

    def no_hang(self, fn, *args, **kwargs):
        """Run ``fn`` in a daemon thread; fail when it blocks (a FIFO opened
        for reading blocks until a writer appears). Returns the result or the
        exception it raised."""
        box: dict = {}

        def run() -> None:
            try:
                box["value"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - handed back to the test
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            # release the reader so the thread ends, then fail
            for fifo in self.fifos:
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    os.close(fd)
                except OSError:
                    pass
            self.fail(f"{getattr(fn, '__name__', fn)} blocked on a non-regular file")
        return box.get("error", box.get("value"))

    def fifo_in_place_of(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            path.unlink()
        os.mkfifo(path)
        self.fifos.append(path)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this host")
    def test_special_files_in_the_pack_are_never_opened(self) -> None:
        """Contract 14.3: every file the pack reads is opened only after lstat
        shows a regular file; a FIFO never blocks verify or review."""
        self.fifos: list[Path] = []
        self.full_pretest()
        pack = Path(self.build()["pack"])
        run = next(child for child in (pack / "runs").iterdir() if child.is_dir())
        saved = {path: path.read_bytes() for path in (
            pack / "manifest.json", pack / "integrity.json", pack / "finalized.json",
            pack / "summary.json", run / "manifest.json")}
        blob = next(p for p in (pack / "blobs").rglob("*") if p.is_file())
        saved[blob] = blob.read_bytes()

        def restore() -> None:
            for path, data in saved.items():
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.write_bytes(data)
            self.assertTrue(pack_mod.verify(pack)["ok"])

        # manifest.json present but not a regular file: a damaged pack
        # (report_integrity_failed, contract 15.1), never opened
        self.fifo_in_place_of(pack / "manifest.json")
        self.assertFalse(self.no_hang(pack_mod.is_pack, pack))
        error = self.no_hang(pack_mod.verify, pack)
        self.assertIsInstance(error, errors.AutonomError)
        self.assertEqual(error.code, errors.REPORT_INTEGRITY_FAILED)
        self.assertTrue(error.hint)
        self.assertEqual(error.extra["failures"][0],
                         {"path": "manifest.json", "reason": "not-a-regular-file"})
        for call in (lambda: review_mod.write(pack, "approve"),
                     lambda: review_mod.status(pack)):
            error = self.no_hang(call)
            self.assertIsInstance(error, errors.AutonomError)
            self.assertEqual(error.code, errors.REPORT_INTEGRITY_FAILED)
        restore()
        # integrity.json and finalized.json: reported, digests not read
        for name in ("integrity.json", "finalized.json"):
            self.fifo_in_place_of(pack / name)
            error = self.no_hang(pack_mod.verify, pack)
            self.assertIsInstance(error, errors.AutonomError)
            self.assertEqual(error.code, errors.REPORT_INTEGRITY_FAILED)
            self.assertIn({"path": name, "reason": "not-a-regular-file"},
                          error.extra["failures"])
            self.assertIn({"path": "integrity.json", "reason": "digests-not-checked"},
                          error.extra["failures"])
            restore()
        # a listed file (a blob, summary.json)
        for path in (blob, pack / "summary.json"):
            relative = path.relative_to(pack).as_posix()
            self.fifo_in_place_of(path)
            error = self.no_hang(pack_mod.verify, pack)
            self.assertIsInstance(error, errors.AutonomError)
            self.assertEqual(error.code, errors.REPORT_INTEGRITY_FAILED)
            self.assertIn({"path": relative, "reason": "not-a-regular-file"},
                          error.extra["failures"])
            # review refuses the damaged pack without blocking either
            for call in (lambda: review_mod.write(pack, "approve"),
                         lambda: review_mod.status(pack)):
                error = self.no_hang(call)
                self.assertIsInstance(error, errors.AutonomError)
                self.assertEqual(error.code, errors.REPORT_INTEGRITY_FAILED)
            restore()
        # a nested bundle file: the bundle is reported entry by entry, not read
        self.fifo_in_place_of(run / "manifest.json")
        error = self.no_hang(pack_mod.verify, pack)
        self.assertIsInstance(error, errors.AutonomError)
        self.assertIn({"path": f"runs/{run.name}/manifest.json",
                       "reason": "not-a-regular-file"}, error.extra["failures"])
        self.assertIn({"run_id": run.name, "ok": False}, error.extra["nested"])
        restore()
        # a symlinked directory on a listed path: digests are not read through it
        blob_dir = blob.parent
        moved = self.root / "moved-blob-dir"
        blob_dir.replace(moved)
        blob_dir.symlink_to(moved, target_is_directory=True)
        error = self.no_hang(pack_mod.verify, pack)
        self.assertIsInstance(error, errors.AutonomError)
        self.assertIn({"path": blob_dir.relative_to(pack).as_posix(), "reason": "symlink"},
                      error.extra["failures"])
        self.assertIn({"path": "integrity.json", "reason": "digests-not-checked"},
                      error.extra["failures"])
        blob_dir.unlink()
        moved.replace(blob_dir)
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # an unlisted FIFO stops verification too (contract 16): nothing is read
        self.fifo_in_place_of(pack / "stray")
        with mock.patch.object(pack_mod.bundle_writer_mod, "verify_tree") as tree:
            error = self.no_hang(pack_mod.verify, pack)
        tree.assert_not_called()
        self.assertEqual(error.extra["failures"],
                         [{"path": "stray", "reason": "not-a-regular-file"},
                          {"path": "integrity.json", "reason": "digests-not-checked"}])
        (pack / "stray").unlink()
        # a review file that is a FIFO: annotations/ is scanned as well, so
        # status and write refuse the pack without blocking (contract 16)
        review_mod.write(pack, "approve")
        self.fifo_in_place_of(pack / "annotations" / "review-2.json")
        for call in (lambda: review_mod.write(pack, "approve"),
                     lambda: review_mod.status(pack)):
            error = self.no_hang(call)
            self.assertIsInstance(error, errors.AutonomError)
            self.assertEqual(error.code, errors.REPORT_INTEGRITY_FAILED)
            self.assertIn({"path": "annotations/review-2.json",
                           "reason": "not-a-regular-file"}, error.extra["failures"])
        (pack / "annotations" / "review-2.json").unlink()
        self.assertEqual([item["n"] for item in review_mod.status(pack)["decisions"]], [1])

    def test_non_regular_manifest_is_an_integrity_failure(self) -> None:
        """Contract 15.1: a manifest.json that exists but is not a regular file
        is report_integrity_failed; not_a_pretest_pack only when it is absent
        or a regular file that is not a pre-test manifest."""
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        manifest = pack / "manifest.json"
        original = manifest.read_bytes()
        # a symlink to a copy of its own bytes
        copy = self.root / "manifest-copy.json"
        copy.write_bytes(original)
        manifest.unlink()
        manifest.symlink_to(copy)
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"][0],
                         {"path": "manifest.json", "reason": "symlink"})
        self.assertIn({"path": "integrity.json", "reason": "digests-not-checked"},
                      failed.extra["failures"])
        self.assertTrue(failed.hint)
        # review refuses with the same code and writes nothing
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.write, pack, "approve")
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.status, pack)
        self.assertFalse((pack / "annotations").exists())
        # a directory in its place
        manifest.unlink()
        manifest.mkdir()
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"][0],
                         {"path": "manifest.json", "reason": "not-a-regular-file"})
        # absent: not a pack
        manifest.rmdir()
        self.assert_code(errors.NOT_A_PRETEST_PACK, pack_mod.verify, pack)
        # a regular file that is not a pre-test manifest: not a pack
        manifest.write_text('{"kind": "autonom.report-bundle/v2"}', encoding="utf-8")
        self.assert_code(errors.NOT_A_PRETEST_PACK, pack_mod.verify, pack)
        manifest.write_bytes(original)
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_nested_bundles_are_read_only_through_real_directories(self) -> None:
        """Contract 15.2: runs/ and runs/<id>/ must lstat as directories before
        a nested bundle is read; otherwise the run is reported, not read."""
        self.full_pretest()
        pack = Path(self.build()["pack"])
        runs = pack / "runs"
        run_ids = sorted(child.name for child in runs.iterdir() if child.is_dir())
        self.assertTrue(run_ids)
        real_verify = pack_mod.report_bundle.verify
        # runs/ is a symlink to a directory outside the pack
        outside = self.root / "outside-runs"
        runs.replace(outside)
        runs.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(pack_mod.report_bundle, "verify",
                               side_effect=real_verify) as spy, \
                mock.patch.object(pack_mod, "_unsafe_entries",
                                  wraps=pack_mod._unsafe_entries) as walk:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        spy.assert_not_called()
        # only the up-front scan of the pack root ran (contract 16), never
        # one below runs/
        self.assertEqual([call.args[0] for call in walk.call_args_list], [pack])
        self.assertEqual(failed.extra["failures"],
                         [{"path": "runs", "reason": "symlink"},
                          {"path": "integrity.json", "reason": "digests-not-checked"}])
        # runs/ is not a real directory: no run is listed as checked
        self.assertEqual(failed.extra["nested"], [])
        runs.unlink()
        outside.replace(runs)
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # one run directory is a symlink inside a real runs/
        bundle = runs / run_ids[0]
        moved = self.root / "outside-run"
        bundle.replace(moved)
        bundle.symlink_to(moved, target_is_directory=True)
        with mock.patch.object(pack_mod, "_verify_nested",
                               wraps=pack_mod._verify_nested) as nested:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertNotIn(bundle, [call.args[0] for call in nested.call_args_list])
        self.assertIn({"path": f"runs/{run_ids[0]}", "reason": "symlink"},
                      failed.extra["failures"])
        self.assertIn({"run_id": run_ids[0], "ok": False}, failed.extra["nested"])
        bundle.unlink()
        moved.replace(bundle)
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # runs/ replaced by a regular file
        parked = self.root / "parked-runs"
        runs.replace(parked)
        runs.write_text("not a directory", encoding="utf-8")
        with mock.patch.object(pack_mod, "_verify_nested") as nested:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        nested.assert_not_called()
        self.assertIn({"path": "runs", "reason": "not-a-directory"}, failed.extra["failures"])
        for run_id in run_ids:
            self.assertIn({"path": f"runs/{run_id}/", "reason": "not-checked"},
                          failed.extra["failures"])
        runs.unlink()
        parked.replace(runs)
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_catalogue_path_normalization(self) -> None:
        cases = {"x": ("x", None), "./x": ("x", None), "a//b": ("a/b", None),
                 "a/./b/": ("a/b", None), "": (None, "malformed-entry"),
                 ".": (None, "malformed-entry"), "./": (None, "malformed-entry"),
                 "a\0b": (None, "malformed-entry"), 5: (None, "malformed-entry"),
                 "/etc/passwd": (None, "path-forbidden"),
                 "//x": (None, "path-forbidden"), "a/../b": (None, "path-forbidden"),
                 "../x": (None, "path-forbidden")}
        for raw, wanted in cases.items():
            self.assertEqual(pack_mod.catalogue_path(raw), wanted, raw)

    def reseal(self, pack: Path, edit) -> None:
        """Rewrite integrity.json with ``edit`` and update finalized.json so
        the finalization record matches the new catalogue."""
        integrity = self.read(pack, "integrity.json")
        edit(integrity["files"])
        (pack / "integrity.json").write_text(json.dumps(integrity), encoding="utf-8")
        finalized = self.read(pack, "finalized.json")
        finalized["integrity_sha256"] = contracts.sha256_file(pack / "integrity.json")
        (pack / "finalized.json").write_text(json.dumps(finalized), encoding="utf-8")

    def test_catalogue_paths_are_normalized_before_comparison(self) -> None:
        """Contract 15.3: ``./x`` and ``a//b`` match their real entries and get
        the same symlink check, so no digest is read through a symlink."""
        self.full_pretest()
        pack = Path(self.build()["pack"])
        original = {name: (pack / name).read_bytes()
                    for name in ("integrity.json", "finalized.json")}
        blob = next(p for p in (pack / "blobs").rglob("*") if p.is_file())
        other = next(p for p in (pack / "blobs").rglob("*")
                     if p.is_file() and p.parent != blob.parent)
        first_dir = blob.parent.relative_to(pack).as_posix() + "/"
        second_dir = other.parent.relative_to(pack).as_posix() + "/"

        def odd_forms(files: list) -> None:
            # every entry under each directory gets the odd spelling, so only
            # normalization can tie the symlinked directory to a listed file
            for item in files:
                if item["path"].startswith(first_dir):
                    item["path"] = "./" + item["path"]
                elif item["path"].startswith(second_dir):
                    item["path"] = item["path"].replace("/", "//", 1)

        self.reseal(pack, odd_forms)
        # the odd spellings name the real files: the pack still verifies
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # a symlinked directory above the "./" entry: digests are not read
        moved = self.root / "outside-blob-dir"
        blob.parent.replace(moved)
        blob.parent.symlink_to(moved, target_is_directory=True)
        with mock.patch.object(pack_mod.bundle_writer_mod, "verify_tree") as tree:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        tree.assert_not_called()
        self.assertIn({"path": blob.parent.relative_to(pack).as_posix(), "reason": "symlink"},
                      failed.extra["failures"])
        self.assertIn({"path": "integrity.json", "reason": "digests-not-checked"},
                      failed.extra["failures"])
        blob.parent.unlink()
        moved.replace(blob.parent)
        # the same for the "a//b" entry
        moved = self.root / "outside-other-dir"
        other.parent.replace(moved)
        other.parent.symlink_to(moved, target_is_directory=True)
        with mock.patch.object(pack_mod.bundle_writer_mod, "verify_tree") as tree:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        tree.assert_not_called()
        self.assertIn({"path": "integrity.json", "reason": "digests-not-checked"},
                      failed.extra["failures"])
        other.parent.unlink()
        moved.replace(other.parent)
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # rejected spellings are reported and never handed to verify_tree
        for bad, reason in (("", "malformed-entry"), ("/etc/hosts", "path-forbidden"),
                            ("blobs/../../outside", "path-forbidden")):
            self.reseal(pack, lambda files, bad=bad: files.append(
                {"path": bad, "sha256": "0" * 64, "size": 0}))
            with mock.patch.object(pack_mod.bundle_writer_mod, "verify_tree") as tree:
                failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify,
                                          pack)
            tree.assert_not_called()
            self.assertIn({"path": bad, "reason": reason}, failed.extra["failures"])
            self.assertIn({"path": "integrity.json", "reason": "digests-not-checked"},
                          failed.extra["failures"])
            for name, data in original.items():
                (pack / name).write_bytes(data)
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_read_regular_bytes_refuses_everything_but_regular_files(self) -> None:
        good = self.root / "good.json"
        good.write_text("{}", encoding="utf-8")
        self.assertEqual(pack_mod.read_regular_bytes(good), b"{}")
        link = self.root / "link.json"
        link.symlink_to(good)
        for path in (link, self.root, self.root / "missing.json"):
            with self.assertRaises(OSError):
                pack_mod.read_regular_bytes(path)
        self.assertIsNone(pack_mod._read_json(link))
        self.assertEqual(pack_mod._read_json(good), {})
        # a file swapped for a FIFO after lstat is refused by the fstat check
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo.json"
            os.mkfifo(fifo)
            real_lstat = os.lstat
            with mock.patch.object(pack_mod.os, "lstat",
                                   side_effect=lambda p: real_lstat(good)):
                with self.assertRaises(OSError):
                    pack_mod.read_regular_bytes(fifo)

    def test_annotations_must_be_a_real_directory(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        (pack / "annotations").write_text("not a dir", encoding="utf-8")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": "annotations", "reason": "not-a-directory"},
                      failed.extra["failures"])
        (pack / "annotations").unlink()
        outside = self.root / "outside-annotations"
        outside.mkdir()
        (pack / "annotations").symlink_to(outside, target_is_directory=True)
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"],
                         [{"path": "annotations", "reason": "symlink"},
                          {"path": "integrity.json", "reason": "digests-not-checked"}])

    def opens_under(self, pack: Path):
        """A context manager that records every file opened below ``pack``
        through ``open``, ``io.open`` or ``os.open`` (the list it yields)."""
        import contextlib
        import io

        opened: list[str] = []
        prefix = str(pack.resolve())
        real_open, real_io_open, real_os_open = open, io.open, os.open

        def note(path) -> None:
            try:
                text = os.path.realpath(os.fspath(path)) if not isinstance(path, int) else ""
                raw = os.path.abspath(os.fspath(path)) if not isinstance(path, int) else ""
            except TypeError:
                return
            if text.startswith(prefix) or raw.startswith(prefix) \
                    or raw.startswith(str(pack)):
                opened.append(raw)

        def spy_open(file, *args, **kwargs):
            note(file)
            return real_open(file, *args, **kwargs)

        def spy_io_open(file, *args, **kwargs):
            note(file)
            return real_io_open(file, *args, **kwargs)

        def spy_os_open(path, *args, **kwargs):
            note(path)
            return real_os_open(path, *args, **kwargs)

        @contextlib.contextmanager
        def watching():
            with mock.patch("builtins.open", spy_open), \
                    mock.patch("io.open", spy_io_open), \
                    mock.patch.object(os, "open", spy_os_open):
                yield opened
        return watching()

    def test_upfront_scan_reads_nothing_when_anything_is_unsafe(self) -> None:
        """Contract 16: one lstat-only walk of the whole pack, annotations/
        included, before anything is opened or hashed; any symlink or special
        file anywhere is report_integrity_failed and nothing else is read."""
        self.full_pretest()
        pack = Path(self.build()["pack"])
        self.assertTrue(pack_mod.verify(pack)["ok"])
        original = {name: (pack / name).read_bytes()
                    for name in ("integrity.json", "finalized.json")}
        secret = self.root / "host-secret.txt"
        secret.write_text("host file outside the pack", encoding="utf-8")
        digest = contracts.sha256_file(secret)
        (pack / "annotations").mkdir()

        def refused(expected: list) -> None:
            with self.opens_under(pack) as opened, \
                    mock.patch.object(pack_mod.bundle_writer_mod, "verify_tree") as tree, \
                    mock.patch.object(pack_mod.report_bundle, "verify") as nested:
                failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify,
                                          pack)
            self.assertEqual(opened, [])
            tree.assert_not_called()
            nested.assert_not_called()
            self.assertEqual(failed.extra["failures"], expected
                             + [{"path": "integrity.json", "reason": "digests-not-checked"}])
            self.assertTrue(failed.hint)
            for call in (lambda: review_mod.write(pack, "approve"),
                         lambda: review_mod.status(pack)):
                with self.opens_under(pack) as opened:
                    self.assert_code(errors.REPORT_INTEGRITY_FAILED, call)
                self.assertEqual(opened, [])

        # a listed symlink under annotations/ to a host file, with the right
        # digest and a re-sealed catalogue (the review probe)
        (pack / "annotations" / "x").symlink_to(secret)
        self.reseal(pack, lambda files: files.append(
            {"path": "annotations/x", "sha256": digest, "size": secret.stat().st_size}))
        refused([{"path": "annotations/x", "reason": "symlink"}])
        (pack / "annotations" / "x").unlink()
        # annotations/ itself a symlink to an outside directory
        outside = self.root / "outside-annotations"
        outside.mkdir()
        (outside / "id").write_text("outside", encoding="utf-8")
        (pack / "annotations").rmdir()
        (pack / "annotations").symlink_to(outside, target_is_directory=True)
        self.reseal(pack, lambda files: files.append(
            {"path": "annotations/id", "sha256": contracts.sha256_file(outside / "id"),
             "size": 7}))
        refused([{"path": "annotations", "reason": "symlink"}])
        (pack / "annotations").unlink()
        for name, data in original.items():
            (pack / name).write_bytes(data)
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # an unlisted symlink deep inside a nested bundle
        run = next(child for child in (pack / "runs").iterdir() if child.is_dir())
        deep = next(path for path in sorted(run.rglob("*")) if path.is_dir())
        (deep / "link").symlink_to(secret)
        refused([{"path": (deep / "link").relative_to(pack).as_posix(), "reason": "symlink"}])
        (deep / "link").unlink()
        # a FIFO under annotations/
        if hasattr(os, "mkfifo"):
            (pack / "annotations").mkdir()
            os.mkfifo(pack / "annotations" / "pipe")
            refused([{"path": "annotations/pipe", "reason": "not-a-regular-file"}])
            (pack / "annotations" / "pipe").unlink()
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_scan_failure_lists_runs_as_unchecked(self) -> None:
        self.full_pretest()
        pack = Path(self.build()["pack"])
        run_ids = sorted(child.name for child in (pack / "runs").iterdir() if child.is_dir())
        self.assertTrue(run_ids)
        (pack / "stray-link").symlink_to(pack / "summary.json")
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["nested"],
                         [{"run_id": run_id, "ok": False} for run_id in run_ids])
        # a scan that cannot finish is a refusal with a hint, not a crash
        (pack / "stray-link").unlink()
        with mock.patch.object(pack_mod, "_unsafe_entries", side_effect=RuntimeError("x")):
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertEqual(failed.extra["failures"], [{"path": "", "reason": "RuntimeError"}])
        self.assertTrue(failed.hint)
        # a directory with a manifest that is not a pre-test one is still
        # scanned first: a symlink there is a damaged pack, never opened
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "manifest.json").write_text('{"kind": "other"}', encoding="utf-8")
        (plain / "link").symlink_to(pack / "summary.json")
        with self.opens_under(plain) as opened:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, plain)
        self.assertEqual(opened, [])
        self.assertEqual(failed.extra["failures"][0], {"path": "link", "reason": "symlink"})
        (plain / "link").unlink()
        self.assert_code(errors.NOT_A_PRETEST_PACK, pack_mod.verify, plain)

    def test_catalogue_names_are_compared_exactly(self) -> None:
        """Contract 17: catalogue paths are compared with names on disk
        exactly on every volume type; a listed path spelled in another case
        is missing and the real entry is unlisted, also where the volume would
        open the file under the other spelling. A symlinked directory is
        caught by the up-front scan."""
        self.full_pretest()
        pack = Path(self.build()["pack"])
        blob = next(p for p in (pack / "blobs").rglob("*") if p.is_file())
        relative = blob.relative_to(pack).as_posix()
        upper = "BLOBS/" + relative.split("/", 1)[1]

        def respell(files: list) -> None:
            for item in files:
                if item["path"] == relative:
                    item["path"] = upper

        # the real entry is reported by itself, or by its directory when no
        # other listed file lives there (an unlisted directory is not entered)
        siblings = [p for p in blob.parent.iterdir() if p != blob]
        unlisted = {"path": relative if siblings else blob.parent.relative_to(pack).as_posix(),
                    "reason": "unlisted"}
        self.reseal(pack, respell)
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": upper, "reason": "missing"}, failed.extra["failures"])
        self.assertIn(unlisted, failed.extra["failures"])
        # the same even when the digest reader finds no fault (as it does on a
        # case-insensitive volume, where it opens the file under either name)
        with mock.patch.object(pack_mod.bundle_writer_mod, "verify_tree", return_value=[]):
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assertIn({"path": upper, "reason": "missing"}, failed.extra["failures"])
        self.assertIn(unlisted, failed.extra["failures"])
        # a core file in another case: missing, and its real entry unlisted
        self.reseal(pack, lambda files: [item.update(path="Summary.md") for item in files
                                         if item["path"] == "summary.md"])
        failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        for item in ({"path": "Summary.md", "reason": "missing"},
                     {"path": "summary.md", "reason": "unlisted"},
                     {"path": "summary.md", "reason": "not-listed"}):
            self.assertIn(item, failed.extra["failures"])
        # spelled exactly again, the pack verifies
        def restore(files: list) -> None:
            for item in files:
                if item["path"] == upper:
                    item["path"] = relative
                elif item["path"] == "Summary.md":
                    item["path"] = "summary.md"

        self.reseal(pack, restore)
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # the blob directory as a symlink: refused before any digest is read
        moved = self.root / "outside-blob-dir"
        blob.parent.replace(moved)
        blob.parent.symlink_to(moved, target_is_directory=True)
        with mock.patch.object(pack_mod.bundle_writer_mod, "verify_tree") as tree:
            failed = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        tree.assert_not_called()
        self.assertEqual(failed.extra["failures"],
                         [{"path": blob.parent.relative_to(pack).as_posix(),
                           "reason": "symlink"},
                          {"path": "integrity.json", "reason": "digests-not-checked"}])

    def test_manifest_validator(self) -> None:
        self.assertEqual(pack_mod.manifest_failures([]), ["manifest must be an object"])
        failures = pack_mod.manifest_failures({"kind": "x", "runs": "a", "evidence": True})
        for text in ("missing pretest_id", "kind must be autonom.pretest-pack/v1",
                     "runs must be a list of ids", "evidence must be a count",
                     "verdict must be a ticket verdict"):
            self.assertIn(text, failures)
        self.assert_code(errors.INVALID_VALUE, pack_mod.validate_manifest, {})
        self.assertIn("missing record_digest", pack_mod.manifest_failures({}))
        for bad in ("x", {"sha256": "0" * 64}, {name: "X" * 64 for name in
                                                ("sha256", "record", "checks", "findings",
                                                 "health")}):
            self.assertIn("record_digest must map sha256, record, checks, findings and "
                          "health to SHA-256 hex digests",
                          pack_mod.manifest_failures({"record_digest": bad}))


# --- tp-012 / tp-014 / tp-016 -------------------------------------------------------------


def assert_summary_facts_and_no_host_paths(self: PackBase) -> None:
    """tp-014-S01 body, shared by the default and the ``/tmp`` temp root."""
    ids = self.full_pretest()
    temp = tempfile.gettempdir()
    kept = ("other paths /Users/someone/x, /etc/hosts and ~/notes.txt and routes /home "
            "and /media/42 stay")
    home_note = (f"{Path.home()}/work/notes.txt and -L{Path.home()}/lib and "
                 f"{self.artifacts} and {temp}/build/app-debug.apk and "
                 f"file://{temp}/y and {self.root}/repo/x; {kept}")
    self.add_check("Unrun", begin=False)
    check_id = checks_mod.add(self.session, self.record, title="With a note",
                              kind="edge", case="offline")["check_id"]
    checks_mod.begin(self.session, self.record, check_id)
    journal_mod.append(self.session, {
        "kind": "action", "verb": "app install",
        "argv": ["app", "install", f"{temp}/build/app-debug.apk"], "ok": True})
    journal_mod.append(self.session, {
        "kind": "action", "verb": "network mock add",
        "argv": ["network", "mock", "add", "--path", "/api/catalog", "--status", "500"],
        "ok": True})
    checks_mod.end(self.session, self.record, check_id, verdict="pass", note=home_note)
    pack = Path(self.build()["pack"])
    summary = self.read(pack, "summary.json")
    markdown = (pack / "summary.md").read_text(encoding="utf-8")
    self.assertEqual(summary_mod.summary_failures(summary), [])
    # no known machine root anywhere (contract section 13.1), also glued to a
    # flag or inside a file:// URL; everything else stays as written
    for root in summary_mod.host_roots([self.artifacts, self.root]):
        self.assertNotIn(root, markdown)
    self.assertIn("-L<host-path> and", markdown)
    self.assertIn("file://<host-path> and", markdown)
    self.assertIn("app install <host-path>", markdown)
    self.assertIn(kept, markdown)
    self.assertIn("/api/catalog", markdown)
    self.assertIn(f"- Verdict: **{summary['verdict']}**", markdown)
    self.assertIn(f"## Checks ({len(summary['checks'])})", markdown)
    self.assertIn(f"## Findings ({len(summary['findings'])})", markdown)
    self.assertIn("## Not covered: " + ", ".join(summary["not_covered"]), markdown)
    self.assertEqual(summary["timing"]["checks"], len(summary["checks"]))
    self.assertEqual(summary["timing"]["findings"], len(summary["findings"]))
    self.assertNotIn(str(Path.home()), markdown)
    self.assertNotIn(str(self.root), markdown)
    self.assertNotIn(str(self.root.resolve()), markdown)
    self.assertNotRegex(markdown, r"[$€£]\s?\d")
    # evidence is named by pack-relative path, and those paths exist
    for item in summary["evidence"]:
        self.assertTrue((pack / item["path"]).exists(), item)
        if item["kind"] != "run":
            self.assertIn(item["path"], markdown)
    finding = next(f for f in summary["findings"] if f["finding_id"] == ids["finding"])
    kinds = {next(e for e in summary["evidence"] if e["id"] == eid)["kind"]
             for eid in finding["evidence"]}
    self.assertEqual(kinds, {"screenshot", "tree", "request"})
    self.assertTrue(finding["repro"])
    shot = next(e for e in summary["evidence"] if e["kind"] == "screenshot")
    self.assertEqual((shot["check_id"], shot["width"], shot["height"]),
                     (ids["chk1"], 1080, 2400))
    # timing facts
    timing = summary["timing"]
    self.assertGreaterEqual(timing["wall_s"], 0)
    self.assertEqual(set(timing["checks_s"]), {ids["chk1"], ids["chk2"], check_id})
    self.assertEqual(timing["auto_findings"],
                     sum(1 for f in summary["findings"] if f["origin"] == "auto"))
    self.assertGreaterEqual(timing["auto_findings"], 2)  # the 500 and the crash
    # the HTML view is the view module's render of this summary
    html = (pack / "index.html").read_text(encoding="utf-8")
    embedded = html.split('id="pretest-data">', 1)[1].split("</script>", 1)[0]
    self.assertEqual(json.loads(embedded), summary)
    # repro steps redact typed text and keep the tap
    steps = next(c for c in summary["checks"] if c["check_id"] == ids["chk1"])["repro"]
    verbs = [step["verb"] for step in steps]
    self.assertEqual(verbs, ["ui type", "ui tap"])
    self.assertEqual(steps[0]["args"], ["<redacted>"])


class SummaryTests(PackBase):
    def test_unrun_check_prevents_pass(self) -> None:
        """tp-012-S01."""
        self.add_check("Total updates")
        unrun = self.add_check("Empty cart message", begin=False)
        result = self.build()
        pack = Path(result["pack"])
        summary = self.read(pack, "summary.json")
        self.assertEqual(summary["verdict"], "not_covered")
        self.assertEqual(result["verdict"], "not_covered")
        self.assertEqual(summary["not_covered"], [unrun])
        markdown = (pack / "summary.md").read_text(encoding="utf-8")
        self.assertIn(f"## Not covered: {unrun}", markdown)
        self.assertIn("Empty cart message", markdown)

    def test_summary_facts_match_and_no_host_paths(self) -> None:
        """tp-014-S01."""
        assert_summary_facts_and_no_host_paths(self)


    def test_finding_refs_of_every_kind_land_in_the_pack(self) -> None:
        ids = self.full_pretest()
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        by_id = {item["id"]: item for item in summary["evidence"]}
        finding = next(f for f in summary["findings"] if f["finding_id"] == ids["finding2"])
        kinds = [by_id[eid]["kind"] for eid in finding["evidence"]]
        self.assertEqual(kinds, ["log", "run", "crash", "action"])
        run = by_id[finding["evidence"][1]]
        self.assertEqual(run["path"], f"runs/{ids['run']}/")
        log = json.loads((pack / by_id[finding["evidence"][0]]["path"]).read_text("utf-8"))
        self.assertEqual(log, {"from": ids["log_from"], "to": ids["log_to"]})
        recording = next(item for item in summary["evidence"] if item["kind"] == "recording")
        self.assertEqual(recording["check_id"], ids["chk2"])
        self.assertIn(recording["id"], next(c for c in summary["checks"]
                                            if c["check_id"] == ids["chk2"])["evidence"])
        # one source, one evidence entry: the crash of the health report and the ref
        self.assertEqual(sum(1 for item in summary["evidence"] if item["kind"] == "crash"), 1)

    def test_unreadable_evidence_is_a_warning_not_a_failure(self) -> None:
        holder = {}

        def during(check_id: str) -> None:
            holder["shot"] = self.shot("gone")
            broken = self.artifacts / "trees" / "0001_120000.json"
            broken.write_text("{not json", encoding="utf-8")
            holder["tree"] = broken.name

        check_id = self.add_check("Broken evidence", during=during)
        findings_mod.add(self.session, self.record, check_id=check_id, severity="minor",
                         title="Odd", actual="x", evidence=[f"tree:{holder['tree']}"])
        index = self.artifacts / "shots" / "index.jsonl"
        row = json.loads(index.read_text("utf-8").splitlines()[0])
        (self.artifacts / row["file"]).unlink()
        result = self.build()
        reasons = {item["evidence"]: item["reason"] for item in result["warnings"]}
        self.assertEqual(reasons[f"shot:{holder['shot']}"], "the screenshot file is missing")
        self.assertEqual(reasons[f"tree:{holder['tree']}"], "the tree file is unreadable")
        summary = self.read(Path(result["pack"]), "summary.json")
        self.assertEqual(summary["evidence"], [])
        self.assertEqual(summary["findings"][0]["evidence"], [])
        self.assertTrue(pack_mod.verify(result["pack"])["ok"])

    def test_unreadable_shot_index_is_a_warning(self) -> None:
        self.add_check("Done")
        index = self.artifacts / "shots" / "index.jsonl"
        index.write_bytes(b"\xff\xfe not utf-8")
        result = self.build()
        self.assertIn("shots/index.jsonl", [item["evidence"] for item in result["warnings"]])
        self.assertTrue(pack_mod.verify(result["pack"])["ok"])

    def test_markdown_keeps_relative_paths_in_titles(self) -> None:
        self.add_check("Open lib/home/screen.dart from app/Users/list")
        markdown = (Path(self.build()["pack"]) / "summary.md").read_text("utf-8")
        self.assertIn("Open lib/home/screen.dart from app/Users/list", markdown)

    def test_kit_coverage_in_summary(self) -> None:
        """tp-016-S01."""
        self.add_check("Offline banner", kind="edge", case="offline")
        self.add_check("500 shows retry", kind="edge", case="server-error", verdict="fail")
        summary = self.read(Path(self.build()["pack"]), "summary.json")
        kit = {item["id"]: item for item in summary["kit"]}
        self.assertEqual(list(kit), kit_mod.ids())
        self.assertEqual((kit["offline"]["covered"], kit["offline"]["verdicts"]),
                         (True, ["pass"]))
        self.assertEqual((kit["server-error"]["covered"], kit["server-error"]["verdicts"]),
                         (True, ["fail"]))
        skipped = [key for key, item in kit.items() if not item["covered"]]
        self.assertEqual(len(skipped), 16)
        markdown = (Path(self.record["pack"]["path"]) / "summary.md").read_text("utf-8")
        self.assertIn("Skipped (16)", markdown)
        self.assertIn("offline (pass)", markdown)

    def test_no_checks_is_not_covered(self) -> None:
        result = self.build()
        summary = self.read(Path(result["pack"]), "summary.json")
        self.assertEqual(summary["verdict"], "not_covered")
        self.assertIn("No checks were added.",
                      (Path(result["pack"]) / "summary.md").read_text("utf-8"))

    def test_build_summary_refusals(self) -> None:
        verdict = {"verdict": "pass", "reasons": [], "not_covered": [], "blocked": [],
                   "open": []}
        self.assert_code(errors.INVALID_VALUE, summary_mod.build_summary, None, [], [], {},
                         [], verdict, {})
        self.assert_code(errors.INVALID_VALUE, summary_mod.build_summary, self.record, [],
                         [], {}, [], {"verdict": "maybe"}, {})
        # a timing block that is not one fails the schema
        error = self.assert_code(errors.INVALID_VALUE, summary_mod.build_summary,
                                 self.record, [], [], {}, [], verdict, {"wall_s": "x"})
        self.assertIn("timing.wall_s must be a number or null", error.extra["failures"])

    def test_summary_validator_messages(self) -> None:
        self.assertEqual(summary_mod.summary_failures("x"), ["summary must be an object"])
        failures = summary_mod.summary_failures({
            "schema": "x", "verdict": "ok", "evidence": [{"id": "x", "kind": "pic",
                                                          "path": "/abs/file"}],
            "checks": [{"check_id": "chk-1", "state": "weird", "evidence": ["e-9"]}],
            "findings": [{"finding_id": "f-1", "check_id": "chk-9", "origin": "bot"}]})
        for text in ("missing pretest_id", "schema must be autonom.pretest-summary/v1",
                     "evidence[0].id must look like e-N",
                     "evidence[0].path must be blobs/sha256/<ab>/<digest> or runs/<id>/",
                     "checks[0].state must be added, open or ended",
                     "checks[0].evidence names unknown 'e-9'",
                     "findings[0].check_id names an unknown check",
                     "findings[0].origin must be agent or auto"):
            self.assertIn(text, failures)
        self.assert_code(errors.INVALID_VALUE, summary_mod.render_markdown, {"schema": "x"})

    def test_strip_host_paths(self) -> None:
        """Contract 13.1: substring replacement of the known roots only."""
        strip = summary_mod.strip_host_paths
        home, temp = str(Path.home()), tempfile.gettempdir()
        autonom_home = os.environ["AUTONOM_HOME"]
        cases = {
            f"a {home}/x b": "a <host-path> b",
            f"a {home} b": "a <host-path> b",
            f"flag -L{home}/lib/x.a, then": "flag -L<host-path>, then",
            f"open file://{temp}/a.html.": "open file://<host-path>.",
            f"({temp}/x)": "(<host-path>)",
            f"cells | {autonom_home}/sessions/s |": "cells | <host-path> |",
            f"`{home}/n.txt`": "`<host-path>`",
            # only at a whole path component
            f"{home}-other/x": f"{home}-other/x",
            # nothing else is rewritten: other users, other absolute paths,
            # app routes, API paths, URLs, "~" paths, pack-relative paths
            "/Users/bob/y and /home/bob/z": "/Users/bob/y and /home/bob/z",
            "/etc/hosts and /bin/sh, then": "/etc/hosts and /bin/sh, then",
            "open /home then /media/42": "open /home then /media/42",
            "GET /api/catalog → 500": "GET /api/catalog → 500",
            "https://api.example.com/v1/cart": "https://api.example.com/v1/cart",
            "notes in ~/work/a.txt and ~bob/b, approx ~5s":
                "notes in ~/work/a.txt and ~bob/b, approx ~5s",
            "`blobs/sha256/ab/abcd` runs/fr_1/ 1/3": "`blobs/sha256/ab/abcd` runs/fr_1/ 1/3",
        }
        for given, wanted in cases.items():
            self.assertEqual(strip(given), wanted, given)
        # the resolved form of a root counts too (macOS /var -> /private/var)
        resolved = os.path.realpath(temp)
        self.assertEqual(strip(f"at {resolved}/x"), "at <host-path>")

    def test_bare_root_before_any_non_path_character_is_replaced(self) -> None:
        """Contract 14.1: a known root is replaced whatever non-path character
        follows it; only a path-name character continues the name."""
        strip = summary_mod.strip_host_paths
        home, temp = str(Path.home()), tempfile.gettempdir()
        for root in (home, temp, "/srv/packs-x"):
            for after in (".", ":", "!", "?", "=", "#", "'", '"', ")", "]", "}", ">", ",",
                          ";", " ", "\t", "\n", "|", "`", "&", "*", "\u00a0", ""):
                given = f"x {root}{after}tail" if after else f"x {root}"
                wanted = f"x <host-path>{after}tail" if after else "x <host-path>"
                self.assertEqual(strip(given, ["/srv/packs-x"]), wanted, repr(given))
            for after in ("a", "Z", "0", "_", "-", "é", "я"):
                given = f"x {root}{after}/y"
                self.assertEqual(strip(given, ["/srv/packs-x"]), given, repr(given))
        # the case from the review: a sentence and a PATH value
        self.assertEqual(strip(f"The dialog defaulted to {home}. PATH was {home}:/bin"),
                         "The dialog defaulted to <host-path>. PATH was <host-path>:/bin")
        self.assertEqual(strip(f"saved in {home}!"), "saved in <host-path>!")
        self.assertEqual(strip(f"HOME={home}#1"), "HOME=<host-path>#1")
        self.assertEqual(strip(f"[{temp}]"), "[<host-path>]")
        # a path below the root still swallows its whole token
        self.assertEqual(strip(f"at {home}/a.log:12."), "at <host-path>.")

    def test_strip_host_paths_known_roots(self) -> None:
        strip = summary_mod.strip_host_paths
        # $HOME, the user's home under /Users/<user> and /home/<user>
        with mock.patch.dict(os.environ, {"HOME": "/srv/home-x/ann"}), \
                mock.patch.object(summary_mod.Path, "home",
                                  return_value=Path("/srv/home-x/ann")), \
                mock.patch.object(summary_mod.getpass, "getuser", return_value="ann"):
            self.assertEqual(strip("a /srv/home-x/ann/x b"), "a <host-path> b")
            self.assertEqual(strip("a /Users/ann/x and /home/ann/y"),
                             "a <host-path> and <host-path>")
            self.assertEqual(strip("a /Users/bob/x and /srv/home-x/bob/y"),
                             "a /Users/bob/x and /srv/home-x/bob/y")
        # no user name, no home: the other roots still apply, nothing fails
        with mock.patch.object(summary_mod.getpass, "getuser", side_effect=OSError("none")), \
                mock.patch.object(summary_mod.Path, "home", side_effect=RuntimeError("none")):
            self.assertEqual(strip("/api/x"), "/api/x")
            self.assertEqual(strip(f"{tempfile.gettempdir()}/x"), "<host-path>")

    def test_strip_host_paths_with_given_roots(self) -> None:
        strip = summary_mod.strip_host_paths
        # a pack or repo root outside home and temp is replaced when given
        self.assertEqual(strip("see /srv/packs-x/wb-7/summary.md", ["/srv/packs-x"]),
                         "see <host-path>")
        self.assertEqual(strip("see /srv/packs-x/wb-7/summary.md"),
                         "see /srv/packs-x/wb-7/summary.md")
        # a root with a space is replaced whole, and only at a component end
        self.assertEqual(strip("at /work/Ann Lee/app/x.dart now", ["/work/Ann Lee"]),
                         "at <host-path> now")
        self.assertEqual(strip("/work/Ann Leeds/x", ["/work/Ann Lee"]), "/work/Ann Leeds/x")
        # one root may be given as a plain string or a path
        self.assertEqual(strip("in /srv/packs-x/a", "/srv/packs-x"), "in <host-path>")
        self.assertEqual(strip("in /srv/packs-x/a", Path("/srv/packs-x")), "in <host-path>")
        # relative and unusable roots are ignored
        self.assertEqual(strip("/api/x", ["api", "", None, "/", "\0bad", 5]), "/api/x")

    def test_strip_host_paths_is_linear_on_hostile_input(self) -> None:
        home = str(Path.home())
        for hostile in ("/" * 200_000, "/a" * 100_000, "~" * 200_000, "a/" * 100_000,
                        " /tmp" * 40_000, "file://" * 30_000, (home + " ") * 20_000,
                        "-L" + (home + "/") * 20_000, home + "a" * 200_000,
                        " " * 200_000):
            started = time.monotonic()
            summary_mod.strip_host_paths(hostile, ["/srv/packs-x"])
            self.assertLess(time.monotonic() - started, 5.0, hostile[:12])


class FixRound3PackTests(PackBase):
    """Mocked screenshots, requests per check, Not covered notes and finding
    steps cut at the evidence (fix round 3, items 2, 3, 15, 16)."""

    def mocked_shot(self, label: str, mocks) -> str:
        number = self.shot(label)
        index = self.artifacts / "shots" / "index.jsonl"
        rows = [json.loads(line) for line in index.read_text("utf-8").splitlines()]
        if mocks is None:
            rows[int(number) - 1].pop("mocks_active", None)
        else:
            rows[int(number) - 1]["mocks_active"] = mocks
        index.write_text("".join(json.dumps(row) + "\n" for row in rows), "utf-8")
        return number

    def request(self, flow_id: str, *, status=200, mocked=False, path="/api/cart",
                duration=None, at=None) -> None:
        now = time.time() if at is None else at
        row = {"id": flow_id, "started_at": iso_now(), "started_at_ms": int(now * 1000),
               "method": "GET", "url": f"https://api.example.com{path}?token={TOKEN}",
               "host": "api.example.com", "path": path, "status": status,
               "mocked": mocked, "mock_id": "m_1" if mocked else None,
               "request_headers_preview": {"Authorization": f"Bearer {TOKEN}"},
               "response_body_preview": f"secret={TOKEN}"}
        if duration is not None:
            row["duration_ms"] = duration
        with (self.artifacts / "network" / "flows.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def test_item2_mocked_screenshots_are_marked(self) -> None:
        from autonom_lib import screenshot as screenshot_mod

        ids = {}

        def during(check_id: str) -> None:
            ids["mocked"] = self.mocked_shot("error-state", 2)
            ids["real"] = self.mocked_shot("real-data", 0)
            ids["listed"] = self.mocked_shot("old-row", [{"id": "m_1"}])
            ids["png"] = self.mocked_shot("png-only", None)
            png = sorted((self.artifacts / "shots").rglob("*png-only.png"))[0]
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x00IEND\xaeB`\x82")
            self.assertTrue(screenshot_mod.embed_metadata(png, {"mocks_active": 3}))

        self.add_check("Server error shows retry", during=during, verdict="fail")
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        by_label = {item["label"]: item for item in summary["evidence"]}
        self.assertEqual((by_label["error-state"]["mocked"],
                          by_label["error-state"]["mocks_active"]), (True, 2))
        self.assertNotIn("mocked", by_label["real-data"])
        self.assertNotIn("mocks_active", by_label["real-data"])
        self.assertEqual(by_label["old-row"]["mocks_active"], 1)
        self.assertEqual(by_label["png-only"]["mocks_active"], 3)
        markdown = (pack / "summary.md").read_text("utf-8")
        mocked_lines = [line for line in markdown.splitlines() if "(mocked data)" in line]
        self.assertEqual(len(mocked_lines), 3)
        self.assertFalse(any("real-data" in line for line in mocked_lines))
        html = (pack / "index.html").read_text("utf-8")
        self.assertEqual(html.count('<span class="tag">Mocked</span>'), 3)
        self.assertIn("error-state, chk-1, mocked data", html)
        self.assertEqual(summary_mod.summary_failures(summary), [])
        bad = json.loads(json.dumps(summary))
        bad["evidence"][0]["mocked"] = "yes"
        bad["evidence"][0]["mocks_active"] = -1
        failures = summary_mod.summary_failures(bad)
        self.assertIn("evidence[0].mocked must be a boolean", failures)
        self.assertIn("evidence[0].mocks_active must be an integer >= 0", failures)

    def test_item3_requests_per_check(self) -> None:
        self.request("before", at=time.time() - 3600)

        def first(check_id: str) -> None:
            self.request("a", status=500, path="/api/cart", duration=120)
            self.request("b", status=404, mocked=True, path="/api/items")
            self.request("c", status=None, path="/api/slow")

        def second(check_id: str) -> None:
            self.request("d", status=200, path="/api/other")

        chk1 = self.add_check("Cart", during=first, verdict="fail")
        chk2 = self.add_check("Other", during=second)
        chk3 = self.add_check("Never ran", begin=False)
        self.request("after", at=time.time() + 3600)
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        checks = {item["check_id"]: item for item in summary["checks"]}
        first_requests = checks[chk1]["requests"]
        self.assertEqual([(r["method"], r["host"], r["path"], r["status"], r["mocked"])
                          for r in first_requests],
                         [("GET", "api.example.com", "/api/cart", 500, False),
                          ("GET", "api.example.com", "/api/items", 404, True),
                          ("GET", "api.example.com", "/api/slow", None, False)])
        self.assertEqual(first_requests[0]["duration_ms"], 120)
        self.assertNotIn("duration_ms", first_requests[1])
        self.assertTrue(first_requests[0]["started_at"])
        self.assertEqual(checks[chk1]["requests_truncated"], 0)
        self.assertEqual([r["path"] for r in checks[chk2]["requests"]], ["/api/other"])
        self.assertEqual(checks[chk3]["requests"], [])
        for request in first_requests:
            self.assertEqual(set(request) - {"duration_ms", "started_at"},
                             {"method", "host", "path", "status", "mocked"})
        on_disk = self.read(pack, f"checks/{chk1}.json")
        self.assertEqual(on_disk["requests"], first_requests)
        markdown = (pack / "summary.md").read_text("utf-8")
        self.assertIn("- Requests (3):", markdown)
        self.assertIn("  - GET api.example.com/api/cart → 500 (120 ms)", markdown)
        self.assertIn("  - GET api.example.com/api/items → 404 (mocked)", markdown)
        self.assertIn("  - GET api.example.com/api/slow → no status", markdown)
        html = (pack / "index.html").read_text("utf-8")
        self.assertIn("Requests <span class=\"count\">3</span>", html)
        self.assertIn("api.example.com/api/items \u2014 404 <span class=\"sub\">(mocked)",
                      html)
        for name in ("summary.json", "summary.md", "index.html", f"checks/{chk1}.json"):
            text = (pack / name).read_text("utf-8")
            self.assertNotIn(TOKEN, text, name)
            self.assertNotIn("Authorization", text, name)
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_a_value_nested_too_deep_to_print_skips_only_its_line(self) -> None:
        # json.loads reads this line, but str() of the method would raise
        # RecursionError; the line is skipped and the next one still counts.
        depth = 100_000

        def during(check_id: str) -> None:
            deep = ('{"id": "deep", "started_at_ms": %d, "method": %s, "status": 500}'
                    % (int(time.time() * 1000), "[" * depth + "]" * depth))
            with (self.artifacts / "network" / "flows.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(deep + "\n")
            self.request("after", path="/api/after")

        chk = self.add_check("Deep", during=during)
        checks = checks_mod.fold(self.session, self.record)
        found = pack_mod.check_requests(self.session, checks)
        self.assertEqual([r["path"] for r in found[chk]["requests"]], ["/api/after"])

    def test_a_deep_id_line_does_not_crash_the_steps_of_a_request_finding(self) -> None:
        # health files an automatic finding on "after"; cutting that finding's
        # steps reads every flows.jsonl row's id (repro._request_time), and the
        # deep id before it must be skipped there too.
        depth = 100_000

        def during(check_id: str) -> None:
            with (self.artifacts / "network" / "flows.jsonl").open("a", encoding="utf-8") as fh:
                fh.write('{"id": %s, "status": 200, "started_at_ms": %d}\n'
                         % ("[" * depth + "]" * depth, int(time.time() * 1000)))
            self.request("after", status=503, path="/api/after")

        self.add_check("Deep id", during=during, verdict="fail")
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        by_id = {item["id"]: item for item in summary["evidence"]}
        shown = [self.read(pack, by_id[eid]["path"]) for f in summary["findings"]
                 if f["origin"] == "auto" for eid in f["evidence"]
                 if by_id[eid]["kind"] == "request"]
        self.assertEqual([item.get("id") for item in shown], ["after"])
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_a_request_ref_to_a_deep_row_is_a_warning_not_a_crash(self) -> None:
        # The id is plain text but the path is nested too deep to print: the
        # row no longer resolves, so the pack warns about the ref and is written.
        depth = 100_000
        flows = self.artifacts / "network" / "flows.jsonl"
        ids: dict = {}

        def during(check_id: str) -> None:
            self.request("fdeep", status=200, path="/api/fdeep")
            ids["finding"] = findings_mod.add(
                self.session, self.record, check_id=check_id, severity="minor",
                title="Deep", actual="x", evidence=["request:fdeep"])["finding_id"]
            row = json.loads(flows.read_text("utf-8").splitlines()[-1])
            row["path"] = "@"
            flows.write_text(json.dumps(row).replace('"@"', "[" * depth + "]" * depth)
                             + "\n", encoding="utf-8")

        self.add_check("Deep ref", during=during, verdict="fail")
        self.assert_code(errors.EVIDENCE_REF_UNKNOWN, findings_mod.resolve_ref,
                         self.session, "request:fdeep")
        result = self.build()
        self.assertIn("request:fdeep", [w["evidence"] for w in result["warnings"]])
        self.assertTrue(pack_mod.verify(Path(result["pack"]))["ok"])

    def test_item3_requests_cap_and_unreadable_store(self) -> None:
        def during(check_id: str) -> None:
            for number in range(5):
                self.request(f"r{number}", path=f"/api/{number}")

        chk = self.add_check("Busy", during=during)
        with (self.artifacts / "network" / "flows.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("not json\n" + "[" * 5000 + "]" * 5000 + "\n[1, 2]\n")
        checks = checks_mod.fold(self.session, self.record)
        with mock.patch.object(pack_mod, "MAX_CHECK_REQUESTS", 2):
            found = pack_mod.check_requests(self.session, checks)
        self.assertEqual([r["path"] for r in found[chk]["requests"]], ["/api/0", "/api/1"])
        self.assertEqual(found[chk]["truncated"], 3)
        (self.artifacts / "network" / "flows.jsonl").unlink()
        (self.artifacts / "network" / "flows.jsonl").mkdir()
        self.assertEqual(pack_mod.check_requests(self.session, checks),
                         {chk: {"requests": [], "truncated": 0}})
        lines = summary_mod._request_lines({"check_id": chk, "requests": [  # noqa: SLF001
            {"method": "GET", "host": "h", "path": f"/{n}", "status": 200, "mocked": False}
            for n in range(summary_mod.MD_REQUESTS + 2)], "requests_truncated": 3})
        self.assertEqual(lines[0], f"- Requests ({summary_mod.MD_REQUESTS + 5}):")
        self.assertEqual(lines[-1], f"  - … and 5 more (see `checks/{chk}.json`)")
        failures = summary_mod.summary_failures({"checks": [
            {"check_id": "chk-1", "requests": [{"method": "GET", "url": "x", "mocked": False}],
             "requests_truncated": -2}]})
        self.assertIn("checks[0].requests_truncated must be an integer >= 0", failures)
        self.assertTrue(any(f.startswith("checks[0].requests must be") for f in failures))

    def test_requests_keep_incomplete_state(self) -> None:
        """A store row with ``completed: false`` keeps that and its error
        kind in the pack, and summary.md / index.html say it never completed."""
        def during(check_id: str) -> None:
            self.request("done", status=200, path="/api/done")
            row = {"id": "hung", "started_at": iso_now(),
                   "started_at_ms": int(time.time() * 1000), "method": "POST",
                   "host": "api.example.com", "path": "/api/pay", "status": None,
                   "mocked": True, "mock_id": "m_9", "completed": False,
                   "error_kind": "hang_cap", "duration_ms": 120000}
            plain = dict(row, id="gone", path="/api/gone", mocked=False, mock_id=None,
                         duration_ms=None)
            plain.pop("error_kind")
            with (self.artifacts / "network" / "flows.jsonl").open(
                    "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n" + json.dumps(plain) + "\n")

        chk = self.add_check("Payment hangs", during=during, verdict="fail")
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        requests = {r["path"]: r for r in
                    next(c for c in summary["checks"] if c["check_id"] == chk)["requests"]}
        self.assertNotIn("completed", requests["/api/done"])
        self.assertNotIn("error_kind", requests["/api/done"])
        self.assertIs(requests["/api/pay"]["completed"], False)
        self.assertEqual(requests["/api/pay"]["error_kind"], "hang_cap")
        self.assertEqual(requests["/api/gone"]["error_kind"], "other")
        self.assertEqual(summary_mod.summary_failures(summary), [])
        markdown = (pack / "summary.md").read_text("utf-8")
        self.assertIn("  - POST api.example.com/api/pay → never completed (hang_cap) "
                      "(mocked, 120000 ms)", markdown)
        self.assertIn("  - POST api.example.com/api/gone → never completed (other)", markdown)
        self.assertIn("  - GET api.example.com/api/done → 200", markdown)
        html = (pack / "index.html").read_text("utf-8")
        self.assertIn("api.example.com/api/pay \u2014 never completed (hang_cap)", html)
        self.assertTrue(pack_mod.verify(pack)["ok"])
        for bad_value in ({"completed": "no"}, {"error_kind": 3}):
            bad = json.loads(json.dumps(summary))
            check = next(c for c in bad["checks"] if c["check_id"] == chk)
            check["requests"][0].update(bad_value)
            self.assertTrue(any(f.endswith("requests must be a list of {method, host, path, "
                                           "status, mocked, duration_ms?, started_at?, "
                                           "completed?, error_kind?}")
                                for f in summary_mod.summary_failures(bad)), bad_value)
        nulled = json.loads(json.dumps(summary))
        next(c for c in nulled["checks"] if c["check_id"] == chk)["requests"][0][
            "error_kind"] = None
        self.assertEqual(summary_mod.summary_failures(nulled), [])
        self.assertEqual(summary_mod.request_text(
            {"method": "GET", "host": "h", "path": "/x", "status": None, "mocked": False,
             "completed": False}), "GET h/x → never completed")

    def test_item15_not_covered_lists_notes(self) -> None:
        self.add_check("Planned only", begin=False)
        self.add_check("Skipped on purpose", verdict="not_covered")
        noted = checks_mod.add(self.session, self.record, title="iOS only",
                               kind="ticket")["check_id"]
        checks_mod.begin(self.session, self.record, noted)
        checks_mod.end(self.session, self.record, noted, verdict="not_covered",
                       note="setOrientation is Android only")
        blocked = checks_mod.add(self.session, self.record, title="Needs live data",
                                 kind="regression")["check_id"]
        checks_mod.begin(self.session, self.record, blocked)
        checks_mod.end(self.session, self.record, blocked, verdict="blocked",
                       note="no seeded catalogue on this backend")
        markdown = (Path(self.build()["pack"]) / "summary.md").read_text("utf-8")
        self.assertIn("- chk-1 — Planned only: never ran", markdown)
        self.assertIn("- chk-2 — Skipped on purpose: no note", markdown)
        self.assertIn("- chk-3 — iOS only: setOrientation is Android only", markdown)
        self.assertIn("- chk-4 — Needs live data: no seeded catalogue on this backend",
                      markdown)

    def test_item16_finding_steps_stop_at_the_evidence(self) -> None:
        ids = {}

        def tap(text: str, stamp: str) -> None:
            with mock.patch.object(journal_mod, "_now", return_value=stamp):
                journal_mod.append(self.session, {"kind": "action", "verb": "ui tap",
                                                  "argv": ["ui", "tap", "--text", text],
                                                  "ok": True})

        def during(check_id: str) -> None:
            tap("Open", "2026-01-01T00:00:00.400Z")
            ids["shot"] = self.shot("broken")
            index = self.artifacts / "shots" / "index.jsonl"
            rows = [json.loads(line) for line in index.read_text("utf-8").splitlines()]
            rows[-1]["captured_at"] = "2026-01-01T00:00:00Z"  # whole seconds, like a capture
            index.write_text("".join(json.dumps(row) + "\n" for row in rows), "utf-8")
            tap("Later", "2026-01-01T00:00:05.000Z")

        chk = self.add_check("Cut", during=during, verdict="fail")
        fid = findings_mod.add(self.session, self.record, check_id=chk, severity="major",
                               title="Broken", actual="x",
                               evidence=[f"shot:{ids['shot']}"])["finding_id"]
        summary = self.read(Path(self.build()["pack"]), "summary.json")
        check = next(item for item in summary["checks"] if item["check_id"] == chk)
        finding = next(item for item in summary["findings"] if item["finding_id"] == fid)
        self.assertEqual([step["selector"]["text"] for step in check["repro"]],
                         ["Open", "Later"])
        self.assertEqual([step["selector"]["text"] for step in finding["repro"]], ["Open"])


class TmpRootSummaryTests(PackBase):
    """The same summary facts with the temporary AUTONOM_HOME under ``/tmp``,
    as on Linux CI (TMPDIR=/tmp)."""

    def setUp(self) -> None:
        if not Path("/tmp").is_dir():
            self.skipTest("no /tmp on this host")
        patcher = mock.patch.object(tempfile, "tempdir", "/tmp")
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.assertTrue(str(self.root).startswith("/tmp/"), self.root)

    def test_summary_facts_match_and_no_host_paths_under_tmp(self) -> None:
        assert_summary_facts_and_no_host_paths(self)


# --- secret scan ---------------------------------------------------------------------------


class SecretScanTests(PackBase):
    def test_whole_pack_holds_no_planted_secret(self) -> None:
        self.full_pretest()
        pack = Path(self.build()["pack"])
        files = [path for path in pack.rglob("*") if path.is_file()]
        self.assertGreater(len(files), 20)
        for path in files:
            data = path.read_bytes()
            name = path.relative_to(pack).as_posix()
            self.assertNotIn(TOKEN.encode(), data, name)
            self.assertNotIn(PASSWORD.encode(), data, name)
            for typed in (CANVAS_PASSWORD, CANVAS_RECORDED, INPUT_VALUE):
                self.assertNotIn(typed.encode(), data, name)
            self.assertNotIn(b'"authorization"', data.lower(), name)
            self.assertIsNone(re.search(rb"(?i)authorization:\s*bearer\s+\w", data), name)
        # the request evidence keeps method, host, path and status only
        summary = self.read(pack, "summary.json")
        request = next(e for e in summary["evidence"] if e["kind"] == "request")
        body = json.loads((pack / request["path"]).read_text("utf-8"))
        self.assertEqual(set(body), {"id", "method", "host", "path", "status"})
        # the crash text is there, masked
        crash = next(e for e in summary["evidence"] if e["kind"] == "crash")
        self.assertIn("Fatal in CartScreen", (pack / crash["path"]).read_text("utf-8"))
        # the action detail is there, typed text redacted
        action = next(e for e in summary["evidence"] if e["kind"] == "action"
                      and "type" in (e["label"] or ""))
        self.assertEqual(json.loads((pack / action["path"]).read_text("utf-8"))["text"],
                         "<redacted>")
        # every typing kind is redacted, not only "type" (contract section 10)
        details = {e["label"]: json.loads((pack / e["path"]).read_text("utf-8"))
                   for e in summary["evidence"] if e["kind"] == "action"}
        canvas = [d for label, d in details.items() if "canvas-text" in label]
        self.assertEqual(len(canvas), 2)
        for detail in canvas:
            self.assertEqual(detail["text"], "<redacted>")
            self.assertEqual(detail["kind"], "text")
        value = next(d for label, d in details.items() if "set-value" in label)
        self.assertEqual(value["value"], "<redacted>")

    def test_action_payload_redaction_rules(self) -> None:
        redact_ = pack_mod._action_payload
        self.assertEqual(redact_({"kind": "text", "text": "x", "sensitive": False})["text"],
                         "<redacted>")
        self.assertEqual(redact_({"kind": "type", "text": "x", "sensitive": True})["text"],
                         "<redacted>")
        self.assertEqual(redact_({"kind": "fill", "typed": "x", "input": "y"}),
                         {"kind": "fill", "typed": "<redacted>", "input": "<redacted>"})
        # a sensitive detail without the value and a tap's nested selector text stay
        self.assertEqual(redact_({"kind": "type", "text": None, "text_len": 3}),
                         {"kind": "type", "text": None, "text_len": 3})
        tap = {"kind": "tap", "selector": {"text": "Remove"}, "node": {"text": "Remove"}}
        self.assertEqual(redact_(tap), tap)
        self.assertIsNone(redact_(None))
        self.assertEqual(redact_(["x"]), ["x"])


# --- tp-015 round trip ---------------------------------------------------------------------


class RoundTripTests(PackBase):
    FIELDS = ("pretest_id", "title", "ticket_ref", "app_id", "base", "head",
              "created_at", "ended_at", "parent_pack", "kind", "target", "rerun_of")

    def test_every_consumed_field_survives_write_read_pack_summarize(self) -> None:
        """tp-015-S01."""
        self.add_check("Remove item", kind="ticket")
        parent = Path(self.build()["pack"])
        flows = self.root / "flows-dir"
        flows.mkdir()
        repo = self.root / "repo"
        repo.mkdir()
        self.record = self.create(
            ticket_ref="WB-8", title="Rerun of WB-7", head="feature/cart", repo=repo,
            flows_dir=flows, budget={"max_actions": 50, "max_minutes": 15,
                                     "forbid_text": ["buy"], "allow_apps": [APP]},
            parent_pack=parent, kind="rerun",
            rerun_of={"pack": str(parent), "checks": ["chk-1"]})
        written = dict(self.record)
        read_back = store.load(self.session, written["pretest_id"])
        self.assertEqual(read_back, written)
        shot_holder = {}
        self.add_check("Remove item", during=lambda cid: shot_holder.update(
            shot=self.shot("after")))
        finding = findings_mod.add(self.session, self.record, check_id="chk-1",
                                   severity="minor", title="Slow", actual="It took 3 s",
                                   expected="Under 1 s",
                                   evidence=[f"shot:{shot_holder['shot']}"])
        findings_mod.update(self.session, self.record, finding["finding_id"],
                            {"reproduced": "1/3", "stability": "intermittent",
                             "repro_runs": [], "draft_flow": str(flows / "draft.yaml")})
        result = self.build()
        pack = Path(result["pack"])
        final = store.load(self.session, written["pretest_id"])
        inner = store.find_pack_record(pack)
        summary = self.read(pack, "summary.json")
        for key in self.FIELDS:
            self.assertEqual(summary[key], final[key], key)
            self.assertEqual(inner[key], final[key], key)
        for key in ("schema", "pretest_id", "session_id", "title", "ticket_ref",
                    "created_at", "repo", "base", "head", "flows_dir", "target", "app_id",
                    "budget", "parent_pack", "kind", "rerun_of"):
            self.assertEqual(final[key], written[key], key)
            self.assertEqual(inner[key], written[key], key)
        self.assertEqual(summary["budget"], {"max_actions": 50, "max_minutes": 15})
        # status and pack: what 'status' and 'review' read
        self.assertEqual(final["status"], "packed")
        self.assertEqual(final["pack"]["integrity_sha256"], result["integrity_sha256"])
        # finding fields consumed by the summary, view and review
        out = summary["findings"][0]
        for key in ("finding_id", "check_id", "origin", "severity", "title", "expected",
                    "actual", "confirmed", "reproduced", "stability", "draft_flow"):
            self.assertEqual(out[key], findings_mod.fold(self.session, final)[0][key], key)
        self.assertEqual(self.read(pack, f"findings/{out['finding_id']}.json"), out)
        # rerun: before (parent) and after (child) per check
        check = summary["checks"][0]
        self.assertEqual(check["before"]["verdict"], "pass")
        self.assertEqual(check["before"]["check_id"], "chk-1")
        self.assertEqual(check["after"]["verdict"], "pass")
        self.assertEqual(check["after"]["evidence"], check["evidence"])
        self.assertEqual([item["title"] for item in summary["parent_checks"]], ["Remove item"])
        # the review fields round-trip through status
        written_review = review_mod.write(pack, "approve", note="Good", author="ann")
        status = review_mod.status(pack)
        for key in ("decision", "note", "author", "checks", "findings", "confirm",
                    "dismiss", "created_at", "review_id", "pretest_id"):
            self.assertEqual(status["latest"][key], written_review[key], key)
        self.assertEqual(status["pretest_id"], written["pretest_id"])

    def test_schema_files_match_what_is_written(self) -> None:
        self.full_pretest()
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        manifest = self.read(pack, "manifest.json")
        summary_schema = json.loads((SCHEMAS / "pretest-summary-v1.schema.json")
                                    .read_text("utf-8"))
        pack_schema = json.loads((SCHEMAS / "pretest-pack-v1.schema.json").read_text("utf-8"))
        self.assertEqual(summary_schema["properties"]["schema"]["const"], summary["schema"])
        self.assertEqual(pack_schema["properties"]["kind"]["const"], manifest["kind"])
        for key in summary_schema["required"]:
            self.assertIn(key, summary)
        for key in pack_schema["required"]:
            self.assertIn(key, manifest)
        for key in pack_schema["properties"]["files"]["required"]:
            self.assertTrue((pack / manifest["files"][key]).is_file(), key)
        for check in summary["checks"]:
            for key in summary_schema["$defs"]["check"]["required"]:
                self.assertIn(key, check)
        for finding in summary["findings"]:
            for key in summary_schema["$defs"]["finding"]["required"]:
                self.assertIn(key, finding)
        pattern = re.compile(summary_schema["properties"]["evidence"]["items"]
                             ["properties"]["path"]["pattern"])
        for item in summary["evidence"]:
            self.assertRegex(item["path"], pattern)
            for key in summary_schema["properties"]["evidence"]["items"]["required"]:
                self.assertIn(key, item)
        kinds = summary_schema["properties"]["evidence"]["items"]["properties"]["kind"]["enum"]
        self.assertEqual(tuple(kinds), summary_mod.EVIDENCE_KINDS)
        self.assertEqual({e["kind"] for e in summary["evidence"]},
                         set(summary_mod.EVIDENCE_KINDS))


# --- tp-018 review ------------------------------------------------------------------------


class ReviewTests(PackBase):
    def packed(self) -> tuple[Path, dict]:
        ids = self.full_pretest()
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        ids["auto"] = next(f["finding_id"] for f in summary["findings"]
                           if f["origin"] == "auto")
        return pack, ids

    def test_review_keeps_integrity(self) -> None:
        """tp-018-S01."""
        pack, ids = self.packed()
        written = review_mod.write(pack, "return-to-dev", findings=[ids["finding"]],
                                   note="500 path missing")
        path = Path(written["path"])
        self.assertEqual(path.parent, pack.resolve() / "annotations")
        self.assertEqual(path.name, "review-1.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        stored = json.loads(path.read_text("utf-8"))
        self.assertEqual(stored["schema"], "autonom.pretest-review/v1")
        self.assertEqual(stored["pack_integrity_sha256"],
                         self.read(pack, "finalized.json")["integrity_sha256"])
        self.assertEqual(review_mod.review_failures(stored), [])
        self.assertTrue(pack_mod.verify(pack)["ok"])
        status = review_mod.status(pack)
        self.assertEqual(status["decision"], "return-to-dev")
        self.assertEqual(status["latest"]["note"], "500 path missing")
        # the view renders the review state
        html = view_mod.render(self.read(pack, "summary.json"), review=status)
        self.assertIn("Returned to dev", html)

    def test_more_checks_and_confirm_dismiss_history(self) -> None:
        pack, ids = self.packed()
        review_mod.write(pack, "more-checks",
                         checks=["Rotate on results", f"token: {TOKEN}"],
                         confirm=[ids["auto"]], author="ann")
        second = review_mod.write(pack, "approve", dismiss=[ids["auto"]], author="bob")
        self.assertEqual(second["review_id"], "review-2")
        (pack / "annotations" / "review-3.json").write_text("{broken", encoding="utf-8")
        (pack / "annotations" / "notes.txt").write_text("ignored", encoding="utf-8")
        status = review_mod.status(pack)
        self.assertEqual([d["decision"] for d in status["decisions"]],
                         ["more-checks", "approve"])
        self.assertEqual(status["decision"], "approve")
        self.assertEqual(status["confirmed"], [])
        self.assertEqual(status["dismissed"], [{"finding_id": ids["auto"], "author": "bob"}])
        self.assertEqual(status["skipped"], ["review-3.json"])
        first = status["decisions"][0]
        self.assertEqual(first["checks"][0], "Rotate on results")
        self.assertNotIn(TOKEN, json.dumps(first))
        self.assertTrue(pack_mod.verify(pack)["ok"])
        # a fourth decision skips the taken number
        self.assertEqual(review_mod.write(pack, "approve")["review_id"], "review-4")

    def test_status_without_reviews(self) -> None:
        pack, _ids = self.packed()
        status = review_mod.status(pack)
        self.assertEqual((status["decision"], status["latest"], status["decisions"]),
                         (None, None, []))

    def test_review_refusals(self) -> None:
        pack, ids = self.packed()
        self.assert_code(errors.REVIEW_NEEDS_FINDING, review_mod.write, pack,
                         "return-to-dev")
        self.assert_code(errors.UNKNOWN_FINDING, review_mod.write, pack, "return-to-dev",
                         findings=["f-99"])
        self.assert_code(errors.UNKNOWN_FINDING, review_mod.write, pack, "approve",
                         confirm=["f-99"])
        error = self.assert_code(errors.NOT_AUTOMATIC, review_mod.write, pack, "approve",
                                 confirm=[ids["finding"]])
        self.assertEqual(error.extra["finding_id"], ids["finding"])
        self.assert_code(errors.NOT_AUTOMATIC, review_mod.write, pack, "approve",
                         dismiss=[ids["finding"]])
        self.assert_code(errors.INVALID_VALUE, review_mod.write, pack, "ship-it")
        self.assert_code(errors.INVALID_VALUE, review_mod.write, pack, "approve",
                         confirm=[ids["auto"]], dismiss=[ids["auto"]])
        self.assert_code(errors.INVALID_VALUE, review_mod.write, pack, "more-checks",
                         checks=["   "])
        self.assert_code(errors.INVALID_VALUE, review_mod.write, pack, "approve",
                         findings=[f"f-{n}" for n in range(1, 120)])
        self.assertFalse((pack / "annotations").exists())
        self.assert_code(errors.NOT_A_PRETEST_PACK, review_mod.write,
                         self.root / "nowhere", "approve")
        self.assert_code(errors.NOT_A_PRETEST_PACK, review_mod.status, self.root / "nowhere")

    def test_review_of_tampered_pack_is_refused(self) -> None:
        pack, ids = self.packed()
        summary = pack / "summary.md"
        summary.write_text(summary.read_text("utf-8") + "x", encoding="utf-8")
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.write, pack,
                         "approve")
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.status, pack)
        self.assertFalse((pack / "annotations").exists())

    def test_symlinked_annotations_are_refused(self) -> None:
        pack, _ids = self.packed()
        outside = self.root / "outside-annotations"
        outside.mkdir()
        os.chmod(outside, 0o755)
        (pack / "annotations").symlink_to(outside, target_is_directory=True)
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.write, pack, "approve")
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.status, pack)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
        # even if verify were passed (e.g. a swap after it ran), the writer refuses
        with mock.patch.object(pack_mod, "verify", return_value={"ok": True}):
            error = self.assert_code(errors.PATH_FORBIDDEN, review_mod.write, pack,
                                     "approve")
            self.assertEqual(error.extra["path"], str(pack.resolve() / "annotations"))
            self.assert_code(errors.PATH_FORBIDDEN, review_mod.status, pack)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
        # a plain file named annotations is refused the same way
        (pack / "annotations").unlink()
        (pack / "annotations").write_text("x", encoding="utf-8")
        with mock.patch.object(pack_mod, "verify", return_value={"ok": True}):
            self.assert_code(errors.PATH_FORBIDDEN, review_mod.write, pack, "approve")
            self.assert_code(errors.PATH_FORBIDDEN, review_mod.status, pack)
        (pack / "annotations").unlink()
        self.assertEqual(review_mod.write(pack, "approve")["review_id"], "review-1")

    def test_annotations_chmod_failure_is_an_autonom_error(self) -> None:
        pack, _ids = self.packed()
        real_chmod = os.chmod

        def failing(path, mode, *args, **kwargs):
            if Path(path).name == "annotations":
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch.object(review_mod.os, "chmod", side_effect=failing):
            error = self.assert_code(errors.PATH_FORBIDDEN, review_mod.write, pack,
                                     "approve")
        self.assertEqual(list((pack / "annotations").iterdir()), [])

    def test_annotations_mkdir_failure_is_an_autonom_error(self) -> None:
        """Contract 12.5: creating annotations/ fails with path_forbidden."""
        pack, _ids = self.packed()
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError(13, "Permission denied")):
            error = self.assert_code(errors.PATH_FORBIDDEN, review_mod.write, pack, "approve")
        self.assertIn("Permission denied", error.message)
        self.assertEqual(error.extra["path"], str(pack.resolve() / "annotations"))
        self.assertFalse((pack / "annotations").exists())

    def test_annotations_fallback_chmod_failure_is_an_autonom_error(self) -> None:
        """Contract 12.5: the chmod used without follow_symlinks support."""
        pack, _ids = self.packed()
        real_chmod = os.chmod

        def no_lchmod(path, mode, *args, **kwargs):
            if Path(path).name == "annotations":
                if kwargs.get("follow_symlinks") is False:
                    raise NotImplementedError("chmod: follow_symlinks unavailable")
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch.object(review_mod.os, "chmod", side_effect=no_lchmod):
            error = self.assert_code(errors.PATH_FORBIDDEN, review_mod.write, pack,
                                     "approve")
        self.assertIn("Operation not permitted", error.message)
        self.assertEqual(list((pack / "annotations").iterdir()), [])

        # the fallback itself works when it is allowed
        def fallback_ok(path, mode, *args, **kwargs):
            if Path(path).name == "annotations" and kwargs.get("follow_symlinks") is False:
                raise NotImplementedError("chmod: follow_symlinks unavailable")
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch.object(review_mod.os, "chmod", side_effect=fallback_ok):
            self.assertEqual(review_mod.write(pack, "approve")["review_id"], "review-1")

    def test_unreadable_annotations_is_an_autonom_error(self) -> None:
        pack, _ids = self.packed()
        review_mod.write(pack, "approve")
        with mock.patch.object(Path, "iterdir",
                               side_effect=PermissionError(13, "Permission denied")):
            with mock.patch.object(pack_mod, "verify", return_value={"ok": True}):
                self.assert_code(errors.PATH_FORBIDDEN, review_mod.write, pack, "approve")
                self.assert_code(errors.PATH_FORBIDDEN, review_mod.status, pack)

    def test_review_file_write_failures(self) -> None:
        pack, _ids = self.packed()
        real_open, real_fdopen = os.open, os.fdopen

        def refuse_review_file(path, flags, *args, **kwargs):
            # the pack's own files are still read (verify, summary)
            if Path(path).name.startswith("review-"):
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(review_mod.os, "open", side_effect=refuse_review_file):
            self.assert_code(errors.OUTPUT_NOT_WRITABLE, review_mod.write, pack, "approve")
        self.assertEqual(list((pack / "annotations").iterdir()), [])

        def short_write(fd, mode="r", *args, **kwargs):
            if "w" in mode:
                raise OSError(28, "No space left on device")
            return real_fdopen(fd, mode, *args, **kwargs)

        with mock.patch.object(review_mod.os, "fdopen", side_effect=short_write):
            error = self.assert_code(errors.OUTPUT_NOT_WRITABLE, review_mod.write, pack,
                                     "approve")
        self.assertIn("No space left", error.message)
        # the partial file is gone, so the next decision takes number 1
        self.assertEqual(list((pack / "annotations").iterdir()), [])
        self.assertEqual(review_mod.write(pack, "approve")["review_id"], "review-1")
        self.assertTrue(pack_mod.verify(pack)["ok"])

    def test_unencodable_review_text_leaves_no_partial_file(self) -> None:
        """Contract 13.2: a write that fails half way (here a lone surrogate
        that UTF-8 cannot store) is an AutonomError and leaves nothing."""
        pack, _ids = self.packed()
        error = self.assert_code(errors.OUTPUT_NOT_WRITABLE, review_mod.write, pack,
                                 "approve", note="bad \udcff text")
        self.assertIn("UnicodeEncodeError", error.message)
        self.assertEqual(list((pack / "annotations").iterdir()), [])
        # an interruption is re-raised as it is, and the partial file is gone too
        with mock.patch.object(review_mod.json, "dumps", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                review_mod.write(pack, "approve")
        self.assertEqual(list((pack / "annotations").iterdir()), [])
        self.assertEqual(review_mod.write(pack, "approve")["review_id"], "review-1")

    def test_review_of_malformed_pack_data_is_an_autonom_error(self) -> None:
        """Contract 13.2: broken JSON anywhere in the pack is a refusal."""
        pack, _ids = self.packed()
        self.assert_code(errors.NOT_A_PRETEST_PACK, review_mod.write, "bad\0path", "approve")
        self.assert_code(errors.NOT_A_PRETEST_PACK, review_mod.status, "bad\0path")
        summary = pack / "summary.json"
        summary.write_bytes(b"\xff\xfe not json")
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.write, pack, "approve")
        self.assert_code(errors.REPORT_INTEGRITY_FAILED, review_mod.status, pack)
        self.assertFalse((pack / "annotations").exists())

    def test_review_validator(self) -> None:
        self.assertEqual(review_mod.review_failures(None), ["review must be an object"])
        failures = review_mod.review_failures({"schema": "x", "decision": "return-to-dev",
                                               "checks": "a", "n": 0, "findings": []})
        for text in ("schema must be autonom.pretest-review/v1", "checks must be a list of strings",
                     "author must be a non-empty string", "n must be an integer >= 1",
                     "return-to-dev needs at least one finding"):
            self.assertIn(text, failures)
        self.assert_code(errors.INVALID_VALUE, review_mod.validate, {})

# --- follow-ups: fu-013, fu-022, fu-021 (pack part) ----------------------------------


ADB_PATH = "/opt/private-sdk/platform-tools/adb"


class FollowupHintTests(PackBase):
    """fu-013: refusals about an existing pack never say to pack again."""

    def cli(self, *argv: str):
        import subprocess

        return subprocess.run([sys.executable, str(ROOT / "scripts" / "autonom.py"), *argv],
                              capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def assert_hint_never_repacks(self, error: errors.AutonomError) -> None:
        self.assertNotIn("again into", error.hint)
        self.assertNotIn("pack the pre-test again", error.hint.lower())

    def test_finalized_repack_and_tampered_pack_hints(self) -> None:
        self.add_check("Done")
        first = self.build()
        pack = Path(first["pack"])
        # packed: another --out
        error = self.assert_code(errors.PRETEST_FINALIZED, self.build,
                                 out=self.root / "elsewhere")
        self.assertEqual(error.hint, pack_mod.FINALIZED_HINT)
        self.assertEqual(error.hint, "this pre-test is already packed; verify the original "
                                     "pack with 'autonom report verify <pack>' or start a "
                                     "new pre-test")
        self.assert_hint_never_repacks(error)
        # the command the hint names is accepted and verifies the original
        verified = self.cli("report", "verify", str(pack))
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["ok"])
        # packed, then tampered: re-pack refuses with the same hint
        blob = pack / "summary.md"
        original = blob.read_bytes()
        blob.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        error = self.assert_code(errors.REPORT_INTEGRITY_FAILED, self.build)
        self.assertEqual(error.hint, pack_mod.FINALIZED_HINT)
        self.assertIn({"path": blob.relative_to(pack).as_posix(), "reason": "digest-mismatch"},
                      error.extra["failures"])
        self.assertEqual(store.load(self.session, self.record["pretest_id"])["status"],
                         "packed")
        # verify itself (report verify) never says to pack again either
        direct = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assert_hint_never_repacks(direct)
        self.assertIn("start a new pre-test", direct.hint)
        refused = self.cli("report", "verify", str(pack))
        self.assertEqual(refused.returncode, 2)
        payload = json.loads(refused.stderr or refused.stdout)
        self.assertEqual(payload["error_code"], errors.REPORT_INTEGRITY_FAILED)
        self.assertNotIn("again into", payload["hint"])
        blob.write_bytes(original)
        self.assertTrue(self.build()["idempotent"])

    def test_unreadable_pack_hint_never_repacks(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        with mock.patch.object(pack_mod, "_verify_pack", side_effect=ValueError("boom")):
            error = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assert_hint_never_repacks(error)
        with mock.patch.object(pack_mod, "_scan_refusal", side_effect=ValueError("boom")):
            error = self.assert_code(errors.REPORT_INTEGRITY_FAILED, pack_mod.verify, pack)
        self.assert_hint_never_repacks(error)

    def test_unmarked_tampered_pack_gives_the_finalized_hint(self) -> None:
        """A finalized pack whose record was never marked (crash) and that was
        changed since: ``_adopt`` uses the same hint as every other refusal
        about an existing pack (contract B.1); a fresh pack into another
        --out still works, the hint just does not offer it."""
        self.add_check("Done")
        with mock.patch.object(store, "mark_packed", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                self.build()
        pack = store.pretest_dir(self.session, self.record["pretest_id"]) / "pack"
        self.record.update(store.load(self.session, self.record["pretest_id"]))
        summary = pack / "summary.md"
        summary.write_text(summary.read_text(encoding="utf-8") + "x", encoding="utf-8")
        error = self.assert_code(errors.REPORT_INTEGRITY_FAILED, self.build)
        self.assertEqual(error.hint, pack_mod.FINALIZED_HINT)
        self.assertIn("autonom report verify <pack>", error.hint)
        self.assert_hint_never_repacks(error)
        self.assertFalse(hasattr(pack_mod, "ADOPT_HINT"))
        fresh = self.build(out=self.root / "fresh")
        self.assertFalse(fresh["idempotent"])


class FollowupWriteCheckTests(PackBase):
    """fu-022: the output is checked before any check is closed."""

    def open_check(self) -> str:
        return self.add_check("Still open", verdict=None)

    def checks_file(self) -> Path:
        return store.pretest_dir(self.session, self.record["pretest_id"]) / store.CHECKS_FILE

    def assert_open_and_endable(self, check_id: str) -> None:
        record = store.load(self.session, self.record["pretest_id"])
        self.assertEqual(record["status"], "active")
        state = checks_mod.fold(self.session, record)[0]
        self.assertEqual((state["check_id"], state["state"]), (check_id, "open"))
        self.assertEqual(store.active_ids(self.session),
                         (self.record["pretest_id"], check_id))
        ended = checks_mod.end(self.session, record, check_id, verdict="pass")
        self.assertEqual((ended["state"], ended["verdict"]), ("ended", "pass"))

    def test_unwritable_parent_leaves_the_open_check_open(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        check_id = self.open_check()
        before = self.checks_file().read_bytes()
        locked = self.root / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        for out in (locked / "pack", locked / "deeper" / "pack"):
            with self.subTest(out=out):
                error = self.assert_code(errors.OUTPUT_NOT_WRITABLE, self.build, out=out)
                self.assertIn(str(out), error.message)
                self.assertTrue(error.extra["reason"])
                self.assertEqual(self.checks_file().read_bytes(), before)
                self.assertFalse(out.exists())
        self.assertEqual(list(locked.iterdir()), [])
        self.assertFalse((store.pretest_dir(self.session, self.record["pretest_id"])
                          / "health.json").exists())
        self.assert_open_and_endable(check_id)
        # once the check is ended by hand, a writable --out packs
        result = self.build(out=self.root / "ok" / "pack")
        self.assertFalse(result["idempotent"])

    def test_parent_that_is_a_file_is_refused_first(self) -> None:
        check_id = self.open_check()
        blocker = self.root / "a-file"
        blocker.write_text("x", encoding="utf-8")
        self.assert_code(errors.OUTPUT_NOT_WRITABLE, self.build, out=blocker / "pack")
        self.assert_open_and_endable(check_id)

    def test_probe_leaves_nothing_behind(self) -> None:
        self.add_check("Done")
        parent = self.root / "packs"
        result = self.build(out=parent / "one")
        self.assertEqual(sorted(path.name for path in parent.iterdir()), ["one"])
        self.assertTrue(pack_mod.verify(result["pack"])["ok"])

    def test_bundle_write_failure_reopens_the_checks_it_closed(self) -> None:
        check_id = self.open_check()
        before = self.checks_file().read_bytes()
        with mock.patch.object(pack_mod.BundleWriter, "finalize",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(self.checks_file().read_bytes(), before)
        self.assertFalse((store.pretest_dir(self.session, self.record["pretest_id"])
                          / "pack").exists())
        # a refusal from the writer says which checks are open again
        with mock.patch.object(pack_mod, "validate_manifest", side_effect=errors.AutonomError(
                errors.INVALID_VALUE, "bad manifest", hint="bug")):
            error = self.assert_code(errors.INVALID_VALUE, self.build)
        self.assertEqual(error.extra["reopened_checks"], [check_id])
        self.assertEqual(self.checks_file().read_bytes(), before)
        self.assert_open_and_endable(check_id)
        self.assertFalse(self.build()["idempotent"])

    def test_failure_after_finalize_keeps_the_checks_ended(self) -> None:
        """The pack holds the check ends once finalized: a failure while
        marking the record never reopens them (a retry adopts the pack)."""
        check_id = self.open_check()
        with mock.patch.object(store, "mark_packed", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                self.build()
        record = store.load(self.session, self.record["pretest_id"])
        state = checks_mod.fold(self.session, record)[0]
        self.assertEqual((state["check_id"], state["state"], state["note"]),
                         (check_id, "ended", pack_mod.OPEN_AT_PACK))
        self.record.update(record)
        self.assertTrue(self.build()["idempotent"])

    def test_restore_failure_still_raises_the_write_error(self) -> None:
        self.open_check()
        with mock.patch.object(pack_mod.BundleWriter, "finalize",
                               side_effect=OSError(5, "I/O error")), \
                mock.patch.object(pack_mod.os, "truncate", side_effect=OSError(5, "ro")):
            with self.assertRaises(OSError) as caught:
                self.build()
        self.assertEqual(caught.exception.strerror, "I/O error")


class FollowupAdbReproTests(PackBase):
    """fu-021 (pack part): repro commands drop the --adb value."""

    def test_summary_and_index_hold_no_adb_path(self) -> None:
        from autonom_lib.pretest import repro as repro_mod

        def during(_check_id: str) -> None:
            journal_mod.append(self.session, {
                "kind": "action", "verb": "ui tap", "ok": True,
                "argv": ["--adb", ADB_PATH, "ui", "tap", "--text", "Remove"]})
            journal_mod.append(self.session, {
                "kind": "action", "verb": "ui swipe", "ok": True,
                "argv": [f"--adb={ADB_PATH}", "ui", "swipe", "--duration", "0.4"]})

        check_id = self.add_check("Remove item", during=during, verdict="fail")
        findings_mod.add(self.session, self.record, check_id=check_id, severity="major",
                         title="Total is stale", actual="The total still shows 30")
        record = store.load(self.session, self.record["pretest_id"])
        raw = repro_mod.steps(self.session, checks_mod.fold(self.session, record)[0])
        self.assertIn(ADB_PATH, json.dumps(raw))  # the journal holds it; the pack must not
        pack = Path(self.build()["pack"])
        for name in ("summary.json", "summary.md", "index.html", f"checks/{check_id}.json"):
            text = (pack / name).read_text(encoding="utf-8")
            self.assertNotIn(ADB_PATH, text, name)
            self.assertNotIn("--adb", text, name)
        summary = self.read(pack, "summary.json")
        steps = summary["checks"][0]["repro"]
        swipe = next(step for step in steps if step.get("verb") == "ui swipe")
        self.assertIn("--duration", swipe["args"])
        self.assertIn("0.4", swipe["args"])
        self.assertTrue(summary["findings"][0]["repro"])
        self.assertNotIn(ADB_PATH, json.dumps(summary["findings"][0]["repro"]))
        self.assertEqual(summary_mod.summary_failures(summary), [])

    def test_drop_host_flags(self) -> None:
        drop = summary_mod.drop_host_flags
        self.assertEqual(drop(["--adb", ADB_PATH, "--text", "Remove"]), ["--text", "Remove"])
        self.assertEqual(drop([f"--adb={ADB_PATH}", "x"]), ["x"])
        self.assertEqual(drop(["--adb"]), [])
        self.assertEqual(drop(["--adbx", "y", "--serial", "e-1"]),
                         ["--adbx", "y", "--serial", "e-1"])
        self.assertEqual(drop([None, 3, "--adb", "p", "z"]), [None, 3, "z"])
        self.assertEqual(drop(None), [])
        self.assertEqual(drop("--adb p"), [])
        self.assertEqual(summary_mod.repro_steps(
            ["text step", {"verb": "ui tap", "args": ["--adb", "p", "a"], "seq": 1},
             {"verb": "flow step"}]),
            ["text step", {"verb": "ui tap", "args": ["a"], "seq": 1}, {"verb": "flow step"}])
        self.assertEqual(summary_mod.repro_steps(None), [])

    def test_view_drops_adb_from_an_older_summary(self) -> None:
        self.add_check("Done")
        pack = Path(self.build()["pack"])
        summary = self.read(pack, "summary.json")
        summary["checks"][0]["repro"] = [{"verb": "ui tap", "args": ["--adb", ADB_PATH,
                                                                     "--serial", "e-1"]}]
        html_text = view_mod.render(summary)
        self.assertNotIn(ADB_PATH, html_text)
        self.assertNotIn("--adb", html_text)
        embedded = re.search(r'id="pretest-data">(.*?)</script>', html_text).group(1)
        self.assertEqual(json.loads(embedded)["checks"][0]["repro"],
                         [{"verb": "ui tap", "args": ["--serial", "e-1"]}])
        self.assertIn("--serial e-1", html_text)
        markdown = summary_mod.render_markdown(summary)
        self.assertNotIn(ADB_PATH, markdown)
        self.assertIn("--serial e-1", markdown)


if __name__ == "__main__":
    unittest.main()
