"""Pre-test findings: evidence refs (fu-015, strict ``shot:<n>``).

A temporary AUTONOM_HOME and a session on a fake Android target; device
reads are patched, the screenshot index is a real file.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import contracts, device_state, errors, logs  # noqa: E402
from autonom_lib.metrics import snapshot as snapshot_mod  # noqa: E402
from autonom_lib.pretest import checks as checks_mod  # noqa: E402
from autonom_lib.pretest import findings as findings_mod  # noqa: E402
from autonom_lib.pretest import store  # noqa: E402

APP = "com.example.app"


class ShotRefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.artifacts = self.root / "home/sessions/s_test"
        for name in ("shots", "trees", "logs", "network", "crashes", "flows", "actions"):
            (self.artifacts / name).mkdir(parents=True)
        self.session = {"session_id": "s_test", "artifacts_dir": str(self.artifacts),
                        "platform": "android", "target_id": "emulator-5554",
                        "app_id": APP, "network": {"enabled": True}}
        target = SimpleNamespace(platform="android", target_id="emulator-5554",
                                 tool="fake-adb", aliases={"serial": "emulator-5554"})
        for owner, name, fake in (
                (device_state, "crash_list", lambda target, app_id=None: []),
                (logs, "tail_logcat_detailed", lambda *a, **k: {
                    "entries": [], "warnings": [], "filter": "uid", "uid": 1, "pid": None}),
                (snapshot_mod, "take", lambda target, app_id: ({
                    "ok": True, "platform": "android", "pid": 42,
                    "memory": {"total_pss_kb": 1}, "cpu": {"process_percent": 1.0},
                    "captured_at": contracts.utc_now()}, "raw"))):
            patcher = mock.patch.object(owner, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record = store.create(
            self.session, target=target, ticket_text="# WB-1 Cart\nTotal updates.",
            ticket_ref="WB-1", title=None, repo=None, base="main", head=None,
            flows_dir=None, app_id=APP, budget=None,
            context={"changed_files": [], "diff_truncated": False})
        self.check_id = checks_mod.add(self.session, self.record, title="Total",
                                       kind="ticket")["check_id"]
        checks_mod.begin(self.session, self.record, self.check_id)
        # one screenshot, row 1 of the index
        folder = self.artifacts / "shots" / self.check_id
        folder.mkdir(parents=True)
        path = folder / "0001_cart.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\ncart")
        row = {"captured_at": contracts.utc_now(), "label": "cart", "task": self.check_id,
               "file": str(path.relative_to(self.artifacts)), "width": 10, "height": 20,
               "pretest_id": self.record["pretest_id"], "check_id": self.check_id}
        (self.artifacts / "shots" / "index.jsonl").write_text(json.dumps(row) + "\n",
                                                              encoding="utf-8")

    def add(self, ref: str) -> dict:
        return findings_mod.add(self.session, self.record, check_id=self.check_id,
                                severity="minor", title="Stale total",
                                actual="Shows 30", evidence=[ref])

    def test_non_ascii_digits_and_suffixes_are_unknown(self) -> None:
        """fu-015-S01: shot:² and shot:1x (and other Unicode digits) give
        evidence_ref_unknown and the findings are unchanged."""
        before = (store.pretest_dir(self.session, self.record["pretest_id"])
                  / store.FINDINGS_FILE)
        size = before.stat().st_size if before.exists() else 0
        for ref in ("shot:²", "shot:1x", "shot:１", "shot:١", "shot:1.0",
                    "shot:-1", "shot:+1", "shot:", "shot:0", "shot:2",
                    "shot:1234567890", "shot:0000000001", "shot:" + "9" * 5000):
            with self.subTest(ref=ref):
                with self.assertRaises(errors.AutonomError) as caught:
                    self.add(ref)
                error = caught.exception
                self.assertEqual(error.code, errors.EVIDENCE_REF_UNKNOWN)
                self.assertEqual(error.extra["ref"], ref)
                self.assertTrue(error.hint)
                json.dumps(error.as_dict())
                with self.assertRaises(errors.AutonomError) as resolved:
                    findings_mod.resolve_ref(self.session, ref)
                self.assertEqual(resolved.exception.code, errors.EVIDENCE_REF_UNKNOWN)
        self.assertEqual(findings_mod.fold(self.session, self.record), [])
        self.assertEqual(before.stat().st_size if before.exists() else 0, size)

    def test_ascii_numbers_and_file_names_still_resolve(self) -> None:
        for ref in ("shot:1", "shot:01", "shot: 1", "shot:0001_cart.png", "shot:0001_cart"):
            with self.subTest(ref=ref):
                resolved = findings_mod.resolve_ref(self.session, ref)
                self.assertEqual(resolved["kind"], "shot")
                self.assertEqual(resolved["data"]["index"], 1)
        finding = self.add("shot:1")
        self.assertEqual(finding["evidence"], ["shot:1"])
        self.assertEqual(len(findings_mod.fold(self.session, self.record)), 1)


class RequestRefTests(unittest.TestCase):
    """A `request:<id>` ref reads flows.jsonl; a hostile line never crashes it."""

    setUp = ShotRefTests.setUp

    def test_deep_lines_are_skipped_and_the_real_row_resolves(self) -> None:
        deep = "[" * 100_000 + "]" * 100_000
        path = self.artifacts / "network/flows.jsonl"
        path.write_text("\n".join([
            "[" * 1_000_000 + "]" * 1_000_000,       # past the parser's limit
            '{"id": %s, "status": 500}' % deep,     # an id too deep for str()
            '{"id": null, "status": 500}',
            json.dumps({"id": "f_0001", "method": "GET", "host": "h", "path": "/p",
                        "status": 503}),
        ]) + "\n", encoding="utf-8")
        resolved = findings_mod.resolve_ref(self.session, "request:f_0001")
        self.assertEqual(resolved["data"]["status"], 503)
        for ref in ("request:None", "request:missing"):
            with self.subTest(ref=ref), self.assertRaises(errors.AutonomError) as caught:
                findings_mod.resolve_ref(self.session, ref)
            self.assertEqual(caught.exception.code, errors.EVIDENCE_REF_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
