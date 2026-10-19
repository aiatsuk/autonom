"""Ticket pre-test core: store, baseline, checks, findings, kit, attribution.

Module-level tests drive the library with a fake target and a temporary
``AUTONOM_HOME``; the attribution tests also drive the real CLI against the
fake adb, so they prove what lands in the journal and the screenshot index.
"""
from __future__ import annotations

import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import actions as actions_mod  # noqa: E402
from autonom_lib import device_state, errors  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import screenshot as shot_mod  # noqa: E402
from autonom_lib.metrics import snapshot as snapshot_mod  # noqa: E402
from autonom_lib.pretest import baseline as baseline_mod  # noqa: E402
from autonom_lib.pretest import checks as checks_mod  # noqa: E402
from autonom_lib.pretest import findings as findings_mod  # noqa: E402
from autonom_lib.pretest import kit as kit_mod  # noqa: E402
from autonom_lib.pretest import store  # noqa: E402

TICKET = textwrap.dedent("""\
    # WB-1 Cart total after removing an item

    Steps: open the cart, remove an item.
    Repro call: curl -H "Authorization: Bearer abcdef0123456789xyz" https://api.example.com/cart?token=s3cr3t&page=2
    Header copy: Bearer 9f8e7d6c5b4a39281706
    Test account: qa@example.com / Password: Qwerty123!
    api_key: sk-live-ABCDEFGH12345678
    AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY
    """)

KIT_IDS = ["offline", "slow", "server-error", "timeout", "background-resume",
           "process-death", "rotation", "dark-mode", "large-text", "locale",
           "permission-denied", "empty-input", "long-input", "emoji-input",
           "rtl-input", "double-tap", "fast-back", "deep-link"]

PRETEST_CODES = {
    "NO_ACTIVE_PRETEST": "no_active_pretest",
    "PRETEST_ALREADY_ACTIVE": "pretest_already_active",
    "PRETEST_FINALIZED": "pretest_finalized",
    "CHECK_ALREADY_OPEN": "check_already_open",
    "NO_OPEN_CHECK": "no_open_check",
    "UNKNOWN_CHECK": "unknown_check",
    "INVALID_VERDICT": "invalid_verdict",
    "UNKNOWN_FINDING": "unknown_finding",
    "INVALID_SEVERITY": "invalid_severity",
    "EVIDENCE_REF_UNKNOWN": "evidence_ref_unknown",
    "BUDGET_EXHAUSTED": "budget_exhausted",
    "FORBIDDEN_ACTION": "forbidden_action",
    "APP_NOT_ALLOWED": "app_not_allowed",
    "REVIEW_NEEDS_FINDING": "review_needs_finding",
    "NOT_AUTOMATIC": "not_automatic",
    "PROFILE_INVALID": "profile_invalid",
    "NOT_A_PRETEST_PACK": "not_a_pretest_pack",
    "DRAFT_UNAVAILABLE": "draft_unavailable",
}


def minimal_png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (shot_mod.PNG_SIGNATURE + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) + chunk(b"IEND", b""))


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib/cart.dart").write_text("void main() {\n  print(1);\n}\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "lib/cart.dart").write_text("void main() {\n  print(2);\n}\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "change")
    return repo


class PretestBase(unittest.TestCase):
    """A temporary AUTONOM_HOME, a session record, a fake target and repo."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        artifacts = self.root / "home/sessions/s_test"
        for name in ("shots", "trees", "logs", "network", "crashes", "flows"):
            (artifacts / name).mkdir(parents=True)
        self.session = {"session_id": "s_test", "artifacts_dir": str(artifacts),
                        "platform": "android", "target_id": "emulator-5554",
                        "app_id": "com.example.app", "network": {"enabled": False}}
        self.target = SimpleNamespace(platform="android", target_id="emulator-5554",
                                      tool="fake-adb", aliases={"serial": "emulator-5554"})
        self.repo = make_repo(self.root)
        crash = mock.patch.object(device_state, "crash_list", return_value=[])
        crash.start()
        self.addCleanup(crash.stop)
        metrics = mock.patch.object(snapshot_mod, "take", return_value=(
            {"ok": True, "platform": "android", "pid": 42, "memory": {"total_pss_kb": 1000},
             "cpu": {"process_percent": 3.0}, "metric_semantics": "android_dumpsys_meminfo_v1",
             "captured_at": "2026-10-10T10:00:00Z"}, "raw"))
        metrics.start()
        self.addCleanup(metrics.stop)

    def create(self, **overrides):
        kwargs = dict(target=self.target, ticket_text=TICKET, ticket_ref="WB-1", title=None,
                      repo=str(self.repo), base="main", head=None, flows_dir=None,
                      app_id="com.example.app", budget=None)
        kwargs.update(overrides)
        return store.create(self.session, **kwargs)

    def pretests(self) -> Path:
        return Path(self.session["artifacts_dir"]) / "pretests"

    def pretest_dirs(self) -> list[str]:
        if not self.pretests().is_dir():
            return []
        return sorted(p.name for p in self.pretests().iterdir() if p.is_dir())

    def assert_code(self, code: str, fn, *args, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        payload = caught.exception.as_dict()
        self.assertEqual(payload["error_code"], code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload.get("hint"), f"{code} has no hint")
        json.dumps(payload)  # every error payload must serialize
        return caught.exception


class ErrorCodeTests(unittest.TestCase):
    def test_every_pretest_code_exists_with_its_wire_value(self) -> None:
        for name, value in PRETEST_CODES.items():
            with self.subTest(code=name):
                self.assertEqual(getattr(errors, name), value)

    def test_reused_codes_are_still_there(self) -> None:
        self.assertEqual(errors.REPORT_INTEGRITY_FAILED, "report_integrity_failed")
        self.assertEqual(errors.NO_ACTIVE_SESSION, "no_active_session")
        self.assertEqual(errors.BACKEND_FAILED, "backend_failed")


class StartTests(PretestBase):
    def test_tp001_s01_start_writes_the_four_files_and_redacts_the_ticket(self) -> None:
        record = self.create()
        self.assertEqual(record["status"], "active")
        self.assertRegex(record["pretest_id"], r"^pt_\d{8}T\d{6}_[0-9a-f]{6}$")
        directory = self.pretests() / record["pretest_id"]
        for name in ("pretest.json", "ticket.md", "context.json", "baseline.json",
                     "budget.json"):
            path = directory / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        ticket = (directory / "ticket.md").read_text(encoding="utf-8")
        self.assertNotIn("abcdef0123456789xyz", ticket)
        self.assertNotIn("9f8e7d6c5b4a39281706", ticket)
        self.assertNotIn("s3cr3t", ticket)
        for secret in ("Qwerty123!", "sk-live-ABCDEFGH12345678", "wJalrXUtnFEMIK7MDENG"):
            self.assertNotIn(secret, ticket)
        self.assertIn("qa@example.com", ticket)
        self.assertIn("<redacted>", ticket)
        # contract 8.1: a matched line is masked from the value to its end
        self.assertIn('Repro call: curl -H "Authorization: <redacted>\n', ticket)
        self.assertIn("Header copy: Bearer <redacted>\n", ticket)
        self.assertIn("Steps: open the cart, remove an item.\n", ticket)
        self.assertIn("Cart total after removing an item", ticket)
        self.assertEqual(record["title"], "WB-1 Cart total after removing an item")
        stored = json.loads((directory / "pretest.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, record)
        store.validate_record(stored)
        for minutes in (float("nan"), float("inf"), 0):  # fix round 5, item 3
            bad = {**stored, "budget": {**stored["budget"], "max_minutes": minutes}}
            error = self.assert_code(errors.INVALID_VALUE, store.validate_record, bad)
            self.assertIn("budget.max_minutes must be a finite number > 0",
                          error.extra["failures"])
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], None))
        self.assertEqual(store.active(self.session)["pretest_id"], record["pretest_id"])
        context = json.loads((directory / "context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["schema"], "autonom.pretest-context/v1")
        self.assertEqual([f["path"] for f in context["changed_files"]], ["lib/cart.dart"])
        budget = json.loads((directory / "budget.json").read_text(encoding="utf-8"))
        self.assertEqual(budget, {"actions_used": 0, "refusals": []})

    def test_defaults_follow_tp006(self) -> None:
        record = self.create()
        self.assertEqual(record["budget"]["max_actions"], 300)
        self.assertEqual(record["budget"]["max_minutes"], 60)
        self.assertIn("купить", record["budget"]["forbid_text"])
        self.assertEqual(record["budget"]["allow_apps"], ["com.example.app"])
        self.assertEqual(record["target"], {"platform": "android",
                                            "target_id": "emulator-5554"})
        self.assertEqual(record["kind"], "pretest")
        self.assertIsNone(record["pack"])

    def test_every_optional_field_round_trips(self) -> None:
        record = self.create(
            title="Custom title", head="feature", flows_dir=str(self.repo),
            budget={"max_actions": 5, "max_minutes": 2.5, "forbid_text": ["Pay now"],
                    "allow_apps": ["com.example.app", "com.other"]},
            parent_pack=str(self.root / "parent/pack"), kind="rerun",
            rerun_of={"pack": str(self.root / "parent/pack"), "checks": ["chk-2"]})
        loaded = store.load(self.session, record["pretest_id"])
        self.assertEqual(loaded, record)
        for key, value in (("title", "Custom title"), ("head", "feature"),
                           ("kind", "rerun"), ("ticket_ref", "WB-1")):
            self.assertEqual(loaded[key], value)
        self.assertEqual(loaded["budget"]["max_minutes"], 2.5)
        self.assertEqual(loaded["rerun_of"]["checks"], ["chk-2"])
        self.assertEqual(loaded["parent_pack"], str(self.root / "parent/pack"))

    def test_a_given_context_is_copied_not_recomputed(self) -> None:
        parent = {"schema": "autonom.pretest-context/v1", "changed_files": [{"path": "x"}]}
        record = self.create(context=parent, base=None, repo=None, kind="more-checks")
        stored = store.read_context(self.session, record["pretest_id"])
        self.assertEqual(stored, parent)

    def test_tp001_s02_no_session_creates_nothing(self) -> None:
        self.assert_code(errors.NO_ACTIVE_SESSION, store.create, None, target=self.target,
                         ticket_text=TICKET, ticket_ref=None, title=None,
                         repo=str(self.repo), base="main", head=None, flows_dir=None,
                         app_id=None, budget=None)
        self.assertEqual(self.pretest_dirs(), [])

    def test_tp001_s02_second_start_is_refused_and_creates_nothing(self) -> None:
        first = self.create()
        error = self.assert_code(errors.PRETEST_ALREADY_ACTIVE, self.create)
        self.assertEqual(error.extra["pretest_id"], first["pretest_id"])
        self.assertEqual(self.pretest_dirs(), [first["pretest_id"]])

    def test_bad_inputs_are_refused_before_anything_is_written(self) -> None:
        self.assert_code(errors.INVALID_VALUE, self.create, budget={"forbid_text": ["(("]})
        self.assert_code(errors.INVALID_VALUE, self.create, budget={"max_actions": 0})
        self.assert_code(errors.INVALID_VALUE, self.create, budget={"max_minutes": -1})
        # fix round 5, item 3: only finite positive numbers
        for minutes in (float("nan"), float("inf"), float("-inf"), 0, 0.0, -0.5, True):
            error = self.assert_code(errors.INVALID_VALUE, self.create,
                                     budget={"max_minutes": minutes})
            self.assertIn("finite number > 0", error.message)
            self.assertIn("--max-minutes 60", error.as_dict()["hint"])
        for actions in (float("nan"), float("inf"), 0, -3, 2.5, True):
            self.assert_code(errors.INVALID_VALUE, self.create, budget={"max_actions": actions})
        self.assert_code(errors.INVALID_VALUE, self.create, kind="bogus")
        self.assert_code(errors.INVALID_VALUE, self.create, base=None)
        self.assert_code(errors.INVALID_VALUE, self.create, rerun_of={"checks": []})
        self.assert_code(errors.INVALID_VALUE, self.create, base="no-such-ref")
        victim = self.root / "victim.txt"
        victim.write_text("keep me\n", encoding="utf-8")
        self.assert_code(errors.INVALID_VALUE, self.create, base=f"--output={victim}")
        self.assert_code(errors.INVALID_VALUE, self.create, head=f"--output={victim}")
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
        self.assert_code(errors.NO_TARGET, self.create,
                         target=SimpleNamespace(platform="web", target_id="x"))
        self.assertEqual(self.pretest_dirs(), [])
        self.assertIsNone(store.active_ids(self.session))

    def test_a_failing_write_leaves_no_directory(self) -> None:
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(self.pretest_dirs(), [])
        self.assertIsNone(store.active_ids(self.session))

    def test_a_malformed_id_is_refused(self) -> None:
        self.assert_code(errors.INVALID_VALUE, store.pretest_dir, self.session, "../x")
        self.assert_code(errors.INVALID_VALUE, store.load, self.session, "pt_bad")

    def test_load_of_an_unknown_pretest(self) -> None:
        self.assert_code(errors.NO_ACTIVE_PRETEST, store.load, self.session,
                         "pt_20260101T000000_abcdef")

    def test_load_of_a_damaged_record(self) -> None:
        record = self.create()
        path = self.pretests() / record["pretest_id"] / "pretest.json"
        path.write_text("{", encoding="utf-8")
        self.assert_code(errors.INVALID_VALUE, store.load, self.session, record["pretest_id"])
        path.write_text(json.dumps({**record, "status": "weird"}), encoding="utf-8")
        error = self.assert_code(errors.INVALID_VALUE, store.load, self.session,
                                 record["pretest_id"])
        self.assertTrue(error.extra["failures"])
        # RISK-042: a damaged active record is not "no pre-test"
        self.assert_code(errors.PRETEST_DAMAGED, store.active, self.session)


class DamagedActiveRecordTests(PretestBase):
    """B.8 / RISK-042: an unreadable or invalid active record."""

    def record_path(self, record: dict) -> Path:
        return self.pretests() / record["pretest_id"] / "pretest.json"

    def damage(self, record: dict, content: bytes) -> Path:
        path = self.record_path(record)
        path.write_bytes(content)
        return path

    def assert_damaged(self, record: dict) -> errors.AutonomError:
        error = self.assert_code(errors.PRETEST_DAMAGED, store.active, self.session)
        path = str(self.record_path(record))
        self.assertIn(path, error.message)
        self.assertEqual(error.hint, "autonom pretest stop --force, or restore the file")
        self.assertEqual(error.extra["pretest_id"], record["pretest_id"])
        self.assertEqual(error.extra["path"], path)
        self.assertTrue(error.extra["reason"])
        return error

    def test_every_damage_form_raises_pretest_damaged(self) -> None:
        record = self.create()
        good = json.dumps(record).encode("utf-8")
        cases = {
            "truncated": (b"{", "not valid JSON"),
            "empty": (b"", "not valid JSON"),
            "array": (b"[]", "record must be an object"),
            "not utf-8": (b"\xff\xfe{", "not UTF-8 text"),
            "bad status": (json.dumps({**record, "status": "weird"}).encode(),
                           "status must be one of"),
            "missing field": (json.dumps({k: v for k, v in record.items()
                                          if k != "budget"}).encode(), "budget"),
            "other id": (json.dumps({**record, "pretest_id":
                                     "pt_20260101T000000_abcdef"}).encode(),
                         "the pointer names"),
        }
        for name, (content, reason) in cases.items():
            with self.subTest(case=name):
                self.damage(record, content)
                error = self.assert_damaged(record)
                self.assertIn(reason, error.extra["reason"])
                status = store.damaged_status(self.session)
                self.assertEqual(status["status"], "damaged")
                self.assertEqual(status["reason"], error.extra["reason"])
                self.assert_code(errors.PRETEST_DAMAGED, store.require_active, self.session)
                # attribution (the evidence hot path) never raises
                self.assertIn(store.attribution(self.session),
                              ({}, {"pretest_id": record["pretest_id"]}))
        self.damage(record, good)
        self.assertEqual(store.active(self.session)["pretest_id"], record["pretest_id"])
        self.assertIsNone(store.damaged_status(self.session))

    def test_a_directory_in_place_of_the_record_is_damage(self) -> None:
        record = self.create()
        path = self.record_path(record)
        path.unlink()
        path.mkdir()
        error = self.assert_damaged(record)
        self.assertIn("cannot be read", error.extra["reason"])

    def test_a_validator_crash_is_damage_not_a_crash(self) -> None:
        record = self.create()
        with mock.patch.object(store, "_failures_for_record", side_effect=TypeError("x")):
            error = self.assert_damaged(record)
        self.assertIn("validation failed (TypeError)", error.extra["reason"])

    def test_the_reason_is_redacted(self) -> None:
        record = self.create()
        with mock.patch.object(store, "_failures_for_record",
                               return_value=["token: sk-live-ABCDEFGH12345678"]):
            error = self.assert_damaged(record)
        self.assertNotIn("sk-live-ABCDEFGH12345678", error.extra["reason"])
        self.assertNotIn("sk-live-ABCDEFGH12345678", json.dumps(error.as_dict()))

    def test_no_record_or_a_finished_one_is_still_no_pretest(self) -> None:
        self.assertIsNone(store.active(self.session))
        self.assertIsNone(store.damaged_status(self.session))
        record = self.create()
        stopped = {**record, "status": "stopped", "ended_at": store.now()}
        self.record_path(record).write_text(json.dumps(stopped), encoding="utf-8")
        self.assertIsNone(store.active(self.session))
        self.assertIsNone(store.damaged_status(self.session))
        self.record_path(record).unlink()
        self.assertIsNone(store.active(self.session))
        self.assertIsNone(store.damaged_status(self.session))

    def test_start_is_refused_while_the_active_record_is_damaged(self) -> None:
        record = self.create()
        self.damage(record, b"{")
        self.assert_code(errors.PRETEST_DAMAGED, self.create)
        self.assertEqual(self.pretest_dirs(), [record["pretest_id"]])

    def test_stop_marks_stopped_and_keeps_the_damaged_file(self) -> None:
        record = self.create()
        check = checks_mod.add(self.session, record, title="Cart", kind="ticket")
        checks_mod.begin(self.session, record, check["check_id"])
        partial = {key: record[key] for key in ("title", "target", "created_at", "budget",
                                                 "ticket_ref", "base", "app_id")}
        partial["status"] = 7  # invalid: the record is damaged
        content = json.dumps(partial).encode("utf-8")
        self.damage(record, content)
        self.assert_damaged(record)
        stopped = store.stop(self.session, force=True)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["pretest_id"], record["pretest_id"])
        self.assertEqual(stopped["damaged"]["file"], "pretest.json.damaged")
        self.assertIn("status must be one of", stopped["damaged"]["reason"])
        directory = self.pretests() / record["pretest_id"]
        kept = directory / "pretest.json.damaged"
        self.assertEqual(kept.read_bytes(), content)
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)
        saved = store.load(self.session, record["pretest_id"])
        self.assertNotIn("damaged", saved)
        self.assertEqual(saved["status"], "stopped")
        # salvaged fields survive; the rest is rebuilt from the session
        self.assertEqual(saved["title"], record["title"])
        self.assertEqual(saved["created_at"], record["created_at"])
        self.assertEqual(saved["target"], record["target"])
        self.assertEqual(saved["budget"], record["budget"])
        self.assertEqual(saved["base"], "main")
        self.assertEqual(saved["session_id"], "s_test")
        self.assertIsNone(store.active_ids(self.session))
        self.assertIsNone(store.active(self.session))
        self.assertIsNone(store.damaged_status(self.session))
        folded = checks_mod.fold(self.session, saved)
        self.assertEqual(folded[0]["state"], "ended")
        self.assertEqual(folded[0]["verdict"], "inconclusive")
        self.assertIn(record["pretest_id"],
                      [item["pretest_id"] for item in store.list_records(self.session)])
        # a new pre-test may start
        self.assertEqual(self.create()["status"], "active")

    def test_stop_without_force_recovers_garbage_too(self) -> None:
        record = self.create()
        self.damage(record, b"\x00garbage")
        stopped = store.stop(self.session)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["title"], "Damaged pre-test")
        self.assertEqual(stopped["target"], {"platform": "android",
                                             "target_id": "emulator-5554"})
        self.assertEqual(stopped["budget"]["allow_apps"], ["com.example.app"])
        self.assertIn("not valid JSON", stopped["damaged"]["reason"])
        self.assert_code(errors.NO_ACTIVE_PRETEST, store.stop, self.session)

    def test_an_older_damaged_copy_is_never_overwritten(self) -> None:
        record = self.create()
        directory = self.pretests() / record["pretest_id"]
        (directory / "pretest.json.damaged").write_bytes(b"older")
        self.damage(record, b"newer")
        stopped = store.stop(self.session)
        self.assertEqual(stopped["damaged"]["file"], "pretest.json.damaged.1")
        self.assertEqual((directory / "pretest.json.damaged").read_bytes(), b"older")
        self.assertEqual((directory / "pretest.json.damaged.1").read_bytes(), b"newer")

    def test_stop_refuses_when_no_device_can_be_recovered(self) -> None:
        record = self.create()
        path = self.damage(record, b"{")
        session = {**self.session, "platform": None, "target_id": None}
        error = self.assert_code(errors.PRETEST_DAMAGED, store.stop, session)
        self.assertEqual(error.extra["pretest_id"], record["pretest_id"])
        self.assertEqual(path.read_bytes(), b"{")
        self.assertFalse((path.parent / "pretest.json.damaged").exists())
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], None))

    def test_stop_refuses_when_the_file_cannot_be_copied_aside(self) -> None:
        record = self.create()
        path = self.damage(record, b"{")
        with mock.patch.object(store.os, "replace", side_effect=PermissionError(13, "denied")):
            error = self.assert_code(errors.PRETEST_DAMAGED, store.stop, self.session)
        self.assertIn("could not be copied aside", error.message)
        self.assertEqual(error.extra["path"], str(path))
        self.assertEqual(path.read_bytes(), b"{")
        self.assertFalse((path.parent / "pretest.json.damaged").exists())
        self.assert_damaged(record)

    def test_the_damaged_file_is_copied_never_moved(self) -> None:
        """While the rebuilt record is written, pretest.json still exists."""
        record = self.create()
        path = self.damage(record, b"{broken")
        seen: list[tuple[bool, bytes, bytes]] = []
        real_write = store.write_json

        def watching(target, value):
            if Path(target) == path:
                kept = path.parent / "pretest.json.damaged"
                seen.append((path.exists(), path.read_bytes(), kept.read_bytes()))
            return real_write(target, value)

        with mock.patch.object(store, "write_json", side_effect=watching):
            stopped = store.stop(self.session, force=True)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(seen[0], (True, b"{broken", b"{broken"))
        self.assertEqual((path.parent / "pretest.json.damaged").read_bytes(), b"{broken")

    def write_fails_for(self, path: Path, exc: BaseException):
        real_write = store.write_json

        def failing(target, value):
            if Path(target) == path:
                raise exc
            return real_write(target, value)

        return mock.patch.object(store, "write_json", side_effect=failing)

    def test_a_failed_rebuild_write_keeps_the_guard_and_can_be_retried(self) -> None:
        record = self.create()
        path = self.damage(record, b"{")
        with self.write_fails_for(path, OSError(28, "No space left on device")):
            error = self.assert_code(errors.PRETEST_DAMAGED, store.stop, self.session,
                                     force=True)
        self.assertIn("could not be written", error.message)
        self.assertIn("stop --force", error.hint)
        self.assertEqual(path.read_bytes(), b"{")
        self.assertFalse((path.parent / "pretest.json.damaged").exists())
        # the guard's source still refuses; the pointer still names the pre-test
        self.assert_damaged(record)
        self.assert_code(errors.PRETEST_DAMAGED, store.require_active, self.session)
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], None))
        stopped = store.stop(self.session, force=True)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["damaged"]["file"], "pretest.json.damaged")
        self.assertEqual((path.parent / "pretest.json.damaged").read_bytes(), b"{")

    def test_a_crash_between_copy_and_rebuild_keeps_the_guard(self) -> None:
        """A process killed after the copy: the damaged original is still there."""
        record = self.create()
        path = self.damage(record, b"[]")

        class Killed(BaseException):
            pass

        with self.write_fails_for(path, Killed()):
            with self.assertRaises(Killed):
                store.stop(self.session, force=True)
        self.assertEqual(path.read_bytes(), b"[]")
        self.assertEqual((path.parent / "pretest.json.damaged").read_bytes(), b"[]")
        self.assert_damaged(record)
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], None))
        stopped = store.stop(self.session, force=True)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["damaged"]["file"], "pretest.json.damaged.1")
        self.assertEqual((path.parent / "pretest.json.damaged").read_bytes(), b"[]")
        self.assertEqual((path.parent / "pretest.json.damaged.1").read_bytes(), b"[]")
        self.assertIsNone(store.damaged_status(self.session))

    def test_a_directory_record_is_moved_aside_and_stopped(self) -> None:
        record = self.create()
        path = self.record_path(record)
        path.unlink()
        path.mkdir()
        stopped = store.stop(self.session, force=True)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue((path.parent / "pretest.json.damaged").is_dir())
        self.assertEqual(store.load(self.session, record["pretest_id"])["status"], "stopped")

    def test_a_failed_write_after_moving_a_directory_puts_it_back(self) -> None:
        record = self.create()
        path = self.record_path(record)
        path.unlink()
        path.mkdir()
        with self.write_fails_for(path, OSError(28, "No space left on device")):
            self.assert_code(errors.PRETEST_DAMAGED, store.stop, self.session, force=True)
        self.assertTrue(path.is_dir())
        self.assertFalse((path.parent / "pretest.json.damaged").exists())
        self.assert_damaged(record)

    def link_record_to(self, record: dict, target: Path) -> Path:
        path = self.record_path(record)
        path.unlink()
        path.symlink_to(target)
        return path

    def outside_targets(self) -> tuple[Path, Path]:
        """A 0755 directory and a 0644 file outside the pre-test directory."""
        outside = self.root / "outside"
        outside.mkdir()
        directory = outside / "shared"
        directory.mkdir()
        os.chmod(directory, 0o755)
        target = outside / "notes.txt"
        target.write_bytes(b"{")  # not a record: reads as damage through the link
        os.chmod(target, 0o644)
        return directory, target

    def assert_link_moved_aside(self, record: dict, path: Path, target: Path) -> None:
        kept = path.parent / "pretest.json.damaged"
        self.assertTrue(kept.is_symlink())
        self.assertEqual(Path(os.readlink(kept)), target)
        self.assertFalse(path.is_symlink())
        self.assertEqual(store.load(self.session, record["pretest_id"])["status"], "stopped")

    def test_a_symlinked_record_never_changes_its_targets_mode(self) -> None:
        """Fix v5 r2 item 1: stop --force moves a linked record aside without
        touching the permissions of what it points at."""
        directory, target = self.outside_targets()
        for name, linked, mode in (("directory", directory, 0o755),
                                   ("file", target, 0o644)):
            with self.subTest(target=name):
                record = self.create()
                path = self.link_record_to(record, linked)
                self.assert_damaged(record)
                stopped = store.stop(self.session, force=True)
                self.assertEqual(stopped["status"], "stopped")
                self.assertEqual(stopped["damaged"]["file"], "pretest.json.damaged")
                self.assertEqual(stat.S_IMODE(os.stat(linked).st_mode), mode)
                self.assert_link_moved_aside(record, path, linked)
        self.assertEqual(target.read_bytes(), b"{")
        self.assertEqual(os.listdir(directory), [])

    def test_a_symlinked_record_is_left_alone_without_link_chmod(self) -> None:
        """Where a link's own mode cannot be set, the mode change is skipped."""
        directory, target = self.outside_targets()
        calls: list[str] = []
        real_chmod = os.chmod

        def watching(path, mode, *args, **kwargs):
            calls.append(str(path))
            return real_chmod(path, mode, *args, **kwargs)

        for name, linked, mode in (("directory", directory, 0o755),
                                   ("file", target, 0o644)):
            with self.subTest(target=name):
                record = self.create()
                path = self.link_record_to(record, linked)
                kept = str(path.parent / "pretest.json.damaged")
                calls.clear()
                with mock.patch.object(store.os, "supports_follow_symlinks", frozenset()), \
                        mock.patch.object(store.os, "chmod", side_effect=watching):
                    stopped = store.stop(self.session, force=True)
                self.assertEqual(stopped["status"], "stopped")
                self.assertNotIn(kept, calls)
                self.assertEqual(stat.S_IMODE(os.stat(linked).st_mode), mode)
                self.assert_link_moved_aside(record, path, linked)

    def test_a_moved_aside_directory_still_becomes_owner_only(self) -> None:
        record = self.create()
        path = self.record_path(record)
        path.unlink()
        path.mkdir(mode=0o755)
        store.stop(self.session, force=True)
        kept = path.parent / "pretest.json.damaged"
        self.assertEqual(stat.S_IMODE(os.lstat(kept).st_mode), 0o600)

    def test_stop_after_a_repair_takes_the_normal_path(self) -> None:
        record = self.create()
        self.damage(record, b"{")
        damage = store.damaged_status(self.session)
        self.record_path(record).write_text(json.dumps(record), encoding="utf-8")
        stopped = store._stop_damaged(self.session, damage)  # noqa: SLF001
        self.assertEqual(stopped["status"], "stopped")
        self.assertNotIn("damaged", stopped)
        self.assertFalse((self.record_path(record).parent / "pretest.json.damaged").exists())


class ActivePointerTests(PretestBase):
    def test_garbage_active_file_reads_as_none_and_never_raises(self) -> None:
        self.pretests().mkdir()
        active = self.pretests() / "active.json"
        for content in ("", "{", "[]", '{"pretest_id": "../../x"}',
                        '{"pretest_id": "pt_20260101T000000_abcdef", "check_id": 5}'):
            active.write_text(content, encoding="utf-8")
            ids = store.active_ids(self.session)
            if ids is not None:
                self.assertEqual(ids, ("pt_20260101T000000_abcdef", None))
            self.assertIsNone(store.active(self.session))
        self.assertIsNone(store.active_ids(None))
        self.assertIsNone(store.active_ids({}))
        self.assertEqual(store.attribution(None), {})

    def test_require_active_refusals(self) -> None:
        self.assert_code(errors.NO_ACTIVE_SESSION, store.require_active, None)
        self.assert_code(errors.NO_ACTIVE_PRETEST, store.require_active, self.session)

    def test_set_open_check_moves_only_the_active_pointer(self) -> None:
        record = self.create()
        self.assertFalse(store.set_open_check(self.session, "pt_20260101T000000_abcdef",
                                              "chk-1"))
        self.assertTrue(store.set_open_check(self.session, record["pretest_id"], "chk-1"))
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], "chk-1"))
        self.assert_code(errors.UNKNOWN_CHECK, store.set_open_check, self.session,
                         record["pretest_id"], "check-1")

    def test_stop_closes_without_a_pack_and_ends_an_open_check(self) -> None:
        record = self.create()
        check = checks_mod.add(self.session, record, title="Cart", kind="ticket")
        checks_mod.begin(self.session, record, check["check_id"])
        stopped = store.stop(self.session)
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNotNone(stopped["ended_at"])
        self.assertIsNone(store.active_ids(self.session))
        folded = checks_mod.fold(self.session, stopped)
        self.assertEqual(folded[0]["state"], "ended")
        self.assertEqual(folded[0]["verdict"], "inconclusive")
        self.assertEqual(folded[0]["note"], "open at stop time")
        self.assert_code(errors.NO_ACTIVE_PRETEST, store.stop, self.session)
        # a stopped pre-test cannot open a check again
        self.assert_code(errors.NO_ACTIVE_PRETEST, checks_mod.begin, self.session,
                         stopped, check["check_id"])
        # and a new one may start
        self.assertEqual(self.create()["status"], "active")

    def test_a_record_without_its_directory_is_refused_and_not_recreated(self) -> None:
        record = self.create()
        ghost = {**record, "pretest_id": "pt_20260101T000000_abcdef"}
        self.assert_code(errors.NO_ACTIVE_PRETEST, checks_mod.add, self.session, ghost,
                         title="A", kind="ticket")
        self.assert_code(errors.NO_ACTIVE_PRETEST, findings_mod.add, self.session, ghost,
                         check_id=None, severity="info", title="t", actual="a")
        self.assert_code(errors.NO_ACTIVE_PRETEST, store.save, self.session, ghost)
        self.assertEqual(self.pretest_dirs(), [record["pretest_id"]])

    def test_list_records_reads_every_pretest(self) -> None:
        first = self.create()
        store.stop(self.session)
        second = self.create()
        ids = [item["pretest_id"] for item in store.list_records(self.session)]
        self.assertEqual(sorted(ids), sorted([first["pretest_id"], second["pretest_id"]]))


class PackedTests(PretestBase):
    def test_packed_pretest_refuses_every_change(self) -> None:
        record = self.create()
        check = checks_mod.add(self.session, record, title="Cart", kind="ticket")
        finding = findings_mod.add(self.session, record, check_id=None, severity="minor",
                                   title="Typo", actual="Totla")
        pack = self.root / "pack"
        packed = store.mark_packed(self.session, record,
                                   {"path": str(pack), "integrity_sha256": "ab" * 32})
        self.assertEqual(packed["status"], "packed")
        self.assertEqual(record["status"], "packed")
        self.assertIsNone(store.active_ids(self.session))
        error = self.assert_code(errors.PRETEST_FINALIZED, checks_mod.add, self.session,
                                 record, title="More", kind="edge")
        self.assertEqual(error.extra["pack"], str(pack))
        self.assert_code(errors.PRETEST_FINALIZED, checks_mod.begin, self.session, record,
                         check["check_id"])
        self.assert_code(errors.PRETEST_FINALIZED, checks_mod.end, self.session, record,
                         check["check_id"], verdict="pass")
        self.assert_code(errors.PRETEST_FINALIZED, findings_mod.add, self.session, record,
                         check_id=None, severity="info", title="x", actual="y")
        self.assert_code(errors.PRETEST_FINALIZED, findings_mod.update, self.session, record,
                         finding["finding_id"], {"stability": "stable"})
        # a stale in-memory copy is caught by the on-disk record under the lock
        stale = {**packed, "status": "active", "pack": None}
        self.assert_code(errors.PRETEST_FINALIZED, checks_mod.add, self.session, stale,
                         title="More", kind="edge")
        self.assert_code(errors.INVALID_VALUE, store.mark_packed, self.session, record, {})

    def test_a_stop_that_loses_the_race_with_a_pack_leaves_it_packed(self) -> None:
        record = self.create()
        pre_pack = dict(record)
        store.mark_packed(self.session, record,
                          {"path": str(self.root / "pack"), "integrity_sha256": "ab" * 32})
        # the stop read the record before the pack was written
        with mock.patch.object(store, "require_active", return_value=pre_pack):
            error = self.assert_code(errors.PRETEST_FINALIZED, store.stop, self.session)
        self.assertEqual(error.extra["pretest_id"], record["pretest_id"])
        on_disk = store.load(self.session, record["pretest_id"])
        self.assertEqual(on_disk["status"], "packed")
        self.assertIsNotNone(on_disk["pack"])
        self.assert_code(errors.PRETEST_FINALIZED, checks_mod.add, self.session, pre_pack,
                         title="More", kind="edge")
        # packing twice is refused too
        self.assert_code(errors.PRETEST_FINALIZED, store.mark_packed, self.session, pre_pack,
                         {"path": str(self.root / "pack2"), "integrity_sha256": "cd" * 32})
        self.assertEqual(store.load(self.session, record["pretest_id"])["pack"]["path"],
                         str(self.root / "pack"))

    def test_stop_and_pack_clear_the_pointer_before_the_final_record(self) -> None:
        for verb in ("stop", "pack"):
            with self.subTest(verb=verb):
                record = self.create()
                seen: list = []
                real_save = store.save

                def spy(session, value, _seen=seen, _real=real_save):
                    _seen.append((value["status"], store.active_ids(session)))
                    return _real(session, value)

                with mock.patch.object(store, "save", side_effect=spy):
                    if verb == "stop":
                        store.stop(self.session)
                    else:
                        store.mark_packed(self.session, record,
                                          {"path": str(self.root / f"p-{verb}"),
                                           "integrity_sha256": "ab" * 32})
                final = [item for item in seen if item[0] != "active"]
                self.assertEqual(len(final), 1)
                self.assertIsNone(final[0][1], "active.json outlived the status write")

    def test_a_crash_before_the_final_record_stops_attribution_at_once(self) -> None:
        record = self.create()
        real_write = store.write_json
        calls = {"n": 0}

        def fail_once(path, value):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return real_write(path, value)

        with mock.patch.object(store, "write_json", side_effect=fail_once):
            with self.assertRaises(KeyboardInterrupt):
                store.stop(self.session)
        # an interrupted write puts the pointer back: the pre-test is still active
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], None))
        self.assertEqual(store.active(self.session)["status"], "active")
        # a process that dies between the two writes runs no restore code
        with mock.patch.object(store, "save", side_effect=SystemExit), \
                mock.patch.object(store, "_write_active"):
            with self.assertRaises(SystemExit):
                store.stop(self.session)
        self.assertIsNone(store.active_ids(self.session))
        journal_mod.append(self.session, {"kind": "note", "text": "after"})
        lines, _total = journal_mod.read(self.session)
        self.assertEqual(lines[-1].get("text"), "after")
        self.assertNotIn("pretest_id", lines[-1])
        self.assertNotIn("check_id", lines[-1])
        # and the next pre-test may start
        self.assertEqual(self.create()["status"], "active")

    def test_stop_without_a_pointer_stops_the_newest_active_record(self) -> None:
        older = self.create()
        (self.pretests() / "active.json").unlink()  # crash: record active, no pointer
        self.assertIsNone(store.active(self.session))
        self.assertEqual(self.create()["status"], "active")  # a new one may start
        newest = store.active(self.session)
        store.stop(self.session)  # stops the pointed one first
        self.assertEqual(store.load(self.session, newest["pretest_id"])["status"], "stopped")
        self.assertEqual(store.load(self.session, older["pretest_id"])["status"], "active")
        stopped = store.stop(self.session)  # then repairs the orphan
        self.assertEqual(stopped["pretest_id"], older["pretest_id"])
        self.assertEqual(store.load(self.session, older["pretest_id"])["status"], "stopped")
        self.assert_code(errors.NO_ACTIVE_PRETEST, store.stop, self.session)

    def test_an_orphan_with_an_open_check_is_stopped_with_the_check_ended(self) -> None:
        record = self.create()
        checks_mod.add(self.session, record, title="x", kind="ticket")
        checks_mod.begin(self.session, record, "chk-1")
        (self.pretests() / "active.json").unlink()
        store.stop(self.session)
        state = checks_mod.fold(self.session, record)[0]
        self.assertEqual((state["state"], state["verdict"]), ("ended", "inconclusive"))

    def test_attribution_ignores_a_pointer_to_a_closed_record(self) -> None:
        record = self.create()
        store.stop(self.session)
        (self.pretests() / "active.json").write_text(
            json.dumps({"pretest_id": record["pretest_id"], "check_id": "chk-1"}),
            encoding="utf-8")
        self.assertEqual(store.attribution(self.session), {})
        journal_mod.note(self.session, "after a crash")
        last = journal_mod.read(self.session)[0][-1]
        self.assertNotIn("pretest_id", last)
        self.assertNotIn("check_id", last)
        # a pointer to a record that does not exist is ignored the same way
        (self.pretests() / "active.json").write_text(
            json.dumps({"pretest_id": "pt_20260101T000000_abcdef", "check_id": None}),
            encoding="utf-8")
        self.assertEqual(store.attribution(self.session), {})

    def test_stop_clears_a_stale_pointer_left_by_an_older_build(self) -> None:
        record = self.create()
        store.stop(self.session)
        (self.pretests() / "active.json").write_text(
            json.dumps({"pretest_id": record["pretest_id"], "check_id": None}),
            encoding="utf-8")
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], None))
        self.assert_code(errors.NO_ACTIVE_PRETEST, store.stop, self.session)
        self.assertIsNone(store.active_ids(self.session))
        self.assertFalse((self.pretests() / "active.json").exists())
        self.assertEqual(store.load(self.session, record["pretest_id"])["status"], "stopped")

    def test_find_pack_record(self) -> None:
        record = self.create()
        pack = self.root / "pack"
        pack.mkdir()
        self.assert_code(errors.NOT_A_PRETEST_PACK, store.find_pack_record, pack)
        (pack / "pretest.json").write_text("not json", encoding="utf-8")
        self.assert_code(errors.NOT_A_PRETEST_PACK, store.find_pack_record, pack)
        (pack / "pretest.json").write_text(json.dumps({"schema": "x"}), encoding="utf-8")
        self.assert_code(errors.NOT_A_PRETEST_PACK, store.find_pack_record, pack)
        (pack / "pretest.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(store.find_pack_record(pack)["pretest_id"], record["pretest_id"])


class BaselineTests(PretestBase):
    def test_tp005_s01_unreadable_crashes_are_explicit_and_start_succeeds(self) -> None:
        failure = errors.AutonomError(errors.BACKEND_FAILED, "adb logcat failed")
        with mock.patch.object(device_state, "crash_list", side_effect=failure):
            record = self.create()
        doc = store.read_baseline(self.session, record["pretest_id"])
        self.assertEqual(doc["schema"], "autonom.pretest-baseline/v1")
        self.assertEqual(doc["crashes"]["state"], "unavailable")
        self.assertIn("backend_failed", doc["crashes"]["reason"])
        self.assertNotIn("names", doc["crashes"])
        self.assertEqual(record["status"], "active")

    def test_available_parts_carry_their_values(self) -> None:
        crashes = [{"name": None, "date": "10-10", "raw": "10-10 12:00 E crash a"},
                   {"name": "App-2026.ips", "date": "2026-10-10 12:01", "raw": "x"}]
        flows = Path(self.session["artifacts_dir"]) / "network/flows.jsonl"
        flows.write_text('{"id": "1"}\n{"id": "2"}\n', encoding="utf-8")
        with mock.patch.object(device_state, "crash_list", return_value=crashes):
            doc = baseline_mod.capture(self.target, self.session, "com.example.app")
        self.assertEqual(doc["crashes"]["state"], "available")
        self.assertEqual(doc["crashes"]["latest"],
                         {"name": "App-2026.ips", "time": "2026-10-10 12:01"})
        self.assertTrue(doc["crashes"]["names"][0].startswith("logcat:"))
        self.assertEqual(doc["network"], {"state": "available", "flows_line_count": 2})
        self.assertEqual(doc["metrics"]["state"], "available")
        self.assertEqual(doc["metrics"]["snapshot"]["memory"], {"total_pss_kb": 1000})
        self.assertEqual(doc["logs"]["state"], "available")
        self.assertTrue(doc["logs"]["cursor_ts"].endswith("Z"))

    def test_missing_capabilities_are_unavailable_never_zero(self) -> None:
        with mock.patch.object(snapshot_mod, "take",
                               side_effect=errors.AutonomError(errors.APP_NOT_RUNNING, "dead")):
            doc = baseline_mod.capture(self.target, self.session, "com.example.app")
        self.assertEqual(doc["metrics"]["state"], "unavailable")
        self.assertIn("app_not_running", doc["metrics"]["reason"])
        self.assertEqual(doc["network"]["state"], "unavailable")
        self.assertNotIn("flows_line_count", doc["network"])
        none = baseline_mod.capture(None, None, None)
        for part in ("crashes", "network", "metrics"):
            self.assertEqual(none[part]["state"], "unavailable", part)
            self.assertTrue(none[part]["reason"])
        empty = baseline_mod.capture(self.target, {**self.session,
                                                   "network": {"enabled": True}}, None)
        self.assertEqual(empty["network"], {"state": "available", "flows_line_count": 0})
        self.assertEqual(empty["metrics"]["state"], "unavailable")


class CheckTests(PretestBase):
    def test_tp007_s01_lifecycle(self) -> None:
        record = self.create()
        added = checks_mod.add(self.session, record, title="Remove item updates total",
                               kind="ticket", why="the ticket's acceptance point")
        self.assertEqual(added["check_id"], "chk-1")
        self.assertEqual(added["state"], "added")
        began = checks_mod.begin(self.session, record, "chk-1")
        self.assertEqual(began["state"], "open")
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], "chk-1"))
        ended = checks_mod.end(self.session, record, "chk-1", verdict="fail",
                               note="500 not handled", run_id="fr_abc")
        self.assertEqual(store.active_ids(self.session), (record["pretest_id"], None))
        listed = checks_mod.fold(self.session, record)
        self.assertEqual(len(listed), 1)
        state = listed[0]
        self.assertEqual(state, ended)
        self.assertEqual(state["state"], "ended")
        self.assertEqual(state["verdict"], "fail")
        self.assertEqual(state["note"], "500 not handled")
        self.assertEqual(state["run_ids"], ["fr_abc"])
        for key in ("began_at", "ended_at"):
            self.assertIsNotNone(state[key])
        self.assertIsInstance(state["duration_s"], float)
        self.assertGreaterEqual(state["duration_s"], 0)
        self.assertIsNotNone(state["seq_from"])
        self.assertIsNotNone(state["seq_to"])
        schema = json.loads((ROOT / "schemas/pretest-check-v1.schema.json").read_text())
        for key in schema["$defs"]["state"]["required"]:
            self.assertIn(key, state)
        path = self.pretests() / record["pretest_id"] / "checks.ndjson"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([line["event"] for line in lines], ["add", "begin", "end"])
        for line in lines:
            checks_mod.validate_event(line)

    def test_journal_window_brackets_the_check(self) -> None:
        record = self.create()
        journal_mod.append(self.session, {"kind": "note", "text": "before"})
        checks_mod.add(self.session, record, title="A", kind="ticket")
        checks_mod.begin(self.session, record, "chk-1")
        journal_mod.append(self.session, {"kind": "note", "text": "inside"})
        state = checks_mod.end(self.session, record, "chk-1", verdict="pass")
        self.assertEqual((state["seq_from"], state["seq_to"]), (2, 2))

    def test_tp007_s02_one_open_check(self) -> None:
        record = self.create()
        checks_mod.add(self.session, record, title="A", kind="ticket")
        checks_mod.add(self.session, record, title="B", kind="edge", case="offline")
        checks_mod.begin(self.session, record, "chk-1")
        error = self.assert_code(errors.CHECK_ALREADY_OPEN, checks_mod.begin, self.session,
                                 record, "chk-2")
        self.assertEqual(error.extra["open_check_id"], "chk-1")
        self.assertIn("chk-1", error.message)
        # beginning the check that is already open is an idempotent retry
        lines = (self.pretests() / record["pretest_id"] / "checks.ndjson").read_text()
        again = checks_mod.begin(self.session, record, "chk-1")
        self.assertEqual(again["state"], "open")
        self.assertEqual(
            (self.pretests() / record["pretest_id"] / "checks.ndjson").read_text(), lines)

    def _crash_after_event(self, fn, *args, **kwargs):
        """Run a verb whose pointer update never happens (a crash between
        the event and ``active.json``)."""
        with mock.patch.object(store, "set_open_check", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                fn(*args, **kwargs)

    def test_a_crash_between_event_and_pointer_is_repaired_by_a_retry(self) -> None:
        record = self.create()
        checks_mod.add(self.session, record, title="A", kind="ticket")
        checks_mod.add(self.session, record, title="B", kind="ticket")
        pretest_id = record["pretest_id"]
        path = self.pretests() / pretest_id / "checks.ndjson"

        self._crash_after_event(checks_mod.begin, self.session, record, "chk-1")
        self.assertEqual(store.active_ids(self.session), (pretest_id, None))
        # attribution follows the recorded events, not the stale pointer
        self.assertEqual(store.attribution(self.session),
                         {"pretest_id": pretest_id, "check_id": "chk-1"})
        journal_mod.note(self.session, "during chk-1")
        self.assertEqual(journal_mod.read(self.session)[0][-1]["check_id"], "chk-1")
        events_before = path.read_text()
        state = checks_mod.begin(self.session, record, "chk-1")  # the retry
        self.assertEqual(state["state"], "open")
        self.assertEqual(path.read_text(), events_before)  # no second begin event
        self.assertEqual(store.active_ids(self.session), (pretest_id, "chk-1"))

        self._crash_after_event(checks_mod.end, self.session, record, "chk-1",
                                verdict="fail", note="500")
        self.assertEqual(store.active_ids(self.session), (pretest_id, "chk-1"))
        self.assertEqual(store.attribution(self.session), {"pretest_id": pretest_id})
        events_before = path.read_text()
        # an ended check is never ended again, even with the same verdict (fix
        # round 3, item 12): refused, but the pointer is repaired first
        error = self.assert_code(errors.NO_OPEN_CHECK, checks_mod.end, self.session,
                                 record, "chk-1", verdict="fail", note="500 again")
        self.assertEqual((error.extra["state"], error.extra["verdict"]), ("ended", "fail"))
        self.assertIn("finding add --check chk-1", error.hint)
        self.assertEqual(path.read_text(), events_before)  # no second end event
        self.assertEqual(store.active_ids(self.session), (pretest_id, None))
        self.assertEqual(checks_mod.fold(self.session, record)[0]["note"], "500")
        # a different verdict is not a retry: refused, the pointer stays right
        error = self.assert_code(errors.NO_OPEN_CHECK, checks_mod.end, self.session,
                                 record, "chk-1", verdict="pass")
        self.assertIsNone(error.extra["open_check_id"])
        self.assertEqual(checks_mod.fold(self.session, record)[0]["verdict"], "fail")

    def test_any_verb_repairs_a_pointer_that_disagrees_with_the_events(self) -> None:
        record = self.create()
        checks_mod.add(self.session, record, title="A", kind="ticket")
        checks_mod.add(self.session, record, title="B", kind="ticket")
        pretest_id = record["pretest_id"]
        checks_mod.begin(self.session, record, "chk-1")
        self._crash_after_event(checks_mod.end, self.session, record, "chk-1",
                                verdict="pass")
        self.assertEqual(store.active_ids(self.session), (pretest_id, "chk-1"))
        # begin of an ended check is still refused, but the pointer is fixed first
        self.assert_code(errors.INVALID_VALUE, checks_mod.begin, self.session, record,
                         "chk-1")
        self.assertEqual(store.active_ids(self.session), (pretest_id, None))
        # a pointer naming the wrong open check is corrected by the refusal path too
        checks_mod.begin(self.session, record, "chk-2")
        store.set_open_check(self.session, pretest_id, "chk-1")
        self.assertEqual(store.attribution(self.session)["check_id"], "chk-2")
        self.assert_code(errors.CHECK_ALREADY_OPEN, checks_mod.begin, self.session,
                         record, "chk-1")
        self.assertEqual(store.active_ids(self.session), (pretest_id, "chk-2"))

    def test_unreadable_check_events_fall_back_to_the_pointer(self) -> None:
        """Contract 8.3 item 4: the folded events are the truth, but when the
        events file exists and cannot be read the pointer's check is used;
        with no events file at all there is no open check."""
        record = self.create()
        pretest_id = record["pretest_id"]
        path = self.pretests() / pretest_id / "checks.ndjson"
        self.assertFalse(path.exists())
        store.set_open_check(self.session, pretest_id, "chk-1")
        self.assertEqual(store.attribution(self.session), {"pretest_id": pretest_id})
        checks_mod.add(self.session, record, title="A", kind="ticket")
        checks_mod.begin(self.session, record, "chk-1")
        path.unlink()
        path.mkdir()  # a directory where the events file should be
        self.addCleanup(lambda: path.rmdir() if path.is_dir() else None)
        self.assertEqual(store.attribution(self.session),
                         {"pretest_id": pretest_id, "check_id": "chk-1"})
        store.set_open_check(self.session, pretest_id, None)
        self.assertEqual(store.attribution(self.session), {"pretest_id": pretest_id})
        # a failing fold also falls back to the pointer, and never raises
        path.rmdir()
        path.write_text("")
        store.set_open_check(self.session, pretest_id, "chk-1")
        with mock.patch.object(checks_mod, "fold", side_effect=RuntimeError("boom")):
            self.assertEqual(store.attribution(self.session),
                             {"pretest_id": pretest_id, "check_id": "chk-1"})

    def test_check_refusals(self) -> None:
        record = self.create()
        checks_mod.add(self.session, record, title="A", kind="ticket")
        self.assert_code(errors.UNKNOWN_CHECK, checks_mod.begin, self.session, record, "chk-9")
        self.assert_code(errors.UNKNOWN_CHECK, checks_mod.end, self.session, record, "chk-9",
                         verdict="pass")
        error = self.assert_code(errors.NO_OPEN_CHECK, checks_mod.end, self.session, record,
                                 "chk-1", verdict="pass")
        self.assertIsNone(error.extra["open_check_id"])
        self.assert_code(errors.INVALID_VERDICT, checks_mod.end, self.session, record,
                         "chk-1", verdict="passed")
        self.assert_code(errors.INVALID_VALUE, checks_mod.add, self.session, record,
                         title="  ", kind="ticket")
        self.assert_code(errors.INVALID_VALUE, checks_mod.add, self.session, record,
                         title="x", kind="smoke")
        self.assert_code(errors.INVALID_VALUE, checks_mod.add, self.session, record,
                         title="x", kind="edge", case="earthquake")
        self.assert_code(errors.FLOW_FILE_NOT_FOUND, checks_mod.add, self.session, record,
                         title="x", kind="regression", flow=str(self.root / "missing.yaml"))
        checks_mod.add(self.session, record, title="B", kind="ticket")
        checks_mod.begin(self.session, record, "chk-2")
        error = self.assert_code(errors.NO_OPEN_CHECK, checks_mod.end, self.session, record,
                                 "chk-1", verdict="pass")
        self.assertEqual(error.extra["open_check_id"], "chk-2")
        checks_mod.end(self.session, record, "chk-2", verdict="pass")
        self.assert_code(errors.INVALID_VALUE, checks_mod.begin, self.session, record, "chk-2")
        self.assertEqual(len(checks_mod.fold(self.session, record)), 2)

    def test_flow_paths_are_stored_absolute_and_text_is_redacted(self) -> None:
        record = self.create()
        flow = self.root / "f.yaml"
        flow.write_text("x", encoding="utf-8")
        state = checks_mod.add(self.session, record, title="Login with token=abc123",
                               kind="regression", flow=str(flow),
                               why="Authorization: Bearer abcdef123456")
        self.assertEqual(state["flow"], str(flow.resolve()))
        self.assertNotIn("abc123", state["title"])
        self.assertNotIn("abcdef123456", state["why"])

    def test_validator_rejects_malformed_events(self) -> None:
        self.assertTrue(checks_mod.event_failures("x"))
        self.assertTrue(checks_mod.event_failures({"event": "add", "check_id": "chk-1"}))
        self.assertTrue(checks_mod.event_failures(
            {"event": "end", "check_id": "chk-1", "ts": "2026-10-10T00:00:00Z",
             "journal_seq": 0, "verdict": "great", "note": None, "run_id": None}))
        self.assert_code(errors.INVALID_VALUE, checks_mod.validate_event, {"event": "x"})
        self.assertEqual(checks_mod.event_failures(
            {"event": "begin", "check_id": "chk-1", "ts": "2026-10-10T00:00:00.123Z",
             "journal_seq": 3}), [])

    def test_a_torn_line_is_skipped(self) -> None:
        record = self.create()
        checks_mod.add(self.session, record, title="A", kind="ticket")
        path = self.pretests() / record["pretest_id"] / "checks.ndjson"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "add", "check_id": "chk-2", "ti')
        self.assertEqual([c["check_id"] for c in checks_mod.fold(self.session, record)],
                         ["chk-1"])
        # the next append closes the torn line first and is never absorbed by it
        added = checks_mod.add(self.session, record, title="B", kind="ticket")
        self.assertEqual(added["check_id"], "chk-2")
        self.assertEqual([c["check_id"] for c in checks_mod.fold(self.session, record)],
                         ["chk-1", "chk-2"])
        lines = path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[-1], "")
        self.assertTrue(lines[-3].endswith('"ti'))
        self.assertEqual(json.loads(lines[-2])["title"], "B")


class FindingTests(PretestBase):
    def setUp(self) -> None:
        super().setUp()
        self.record = self.create()
        checks_mod.add(self.session, self.record, title="Cart", kind="ticket")
        checks_mod.begin(self.session, self.record, "chk-1")
        self.artifacts = Path(self.session["artifacts_dir"])

    def findings_file(self) -> Path:
        return self.pretests() / self.record["pretest_id"] / "findings.ndjson"

    def seed_artifacts(self) -> None:
        (self.artifacts / "shots/0001_120000_cart.png").write_bytes(minimal_png())
        shot_mod.append_index(self.session, {"file": "shots/0001_120000_cart.png",
                                             "label": "cart", "task": None})
        (self.artifacts / "trees/0001_120000.json").write_text("{}", encoding="utf-8")
        (self.artifacts / "actions").mkdir(exist_ok=True)
        (self.artifacts / "actions/0003_tap.json").write_text("{}", encoding="utf-8")
        (self.artifacts / "network/flows.jsonl").write_text(json.dumps(
            {"id": "abc", "method": "GET", "host": "api.example.com", "path": "/cart",
             "status": 500}) + "\n", encoding="utf-8")
        (self.artifacts / "flows/fr_123").mkdir()
        (self.artifacts / "crashes/App-1.ips.txt").write_text("trace", encoding="utf-8")
        health = self.pretests() / self.record["pretest_id"] / "health.json"
        health.write_text(json.dumps({"crashes": {"state": "available", "new": [
            {"name": "logcat:0123abcd", "time": "10-10 12:00"}]}}), encoding="utf-8")

    def test_tp009_s01_unknown_evidence_writes_nothing(self) -> None:
        error = self.assert_code(
            errors.EVIDENCE_REF_UNKNOWN, findings_mod.add, self.session, self.record,
            check_id="chk-1", severity="major", title="Total wrong", expected="9",
            actual="10", evidence=["shot:999"])
        self.assertEqual(error.extra["ref"], "shot:999")
        self.assertFalse(self.findings_file().exists())
        self.seed_artifacts()
        findings_mod.add(self.session, self.record, check_id="chk-1", severity="major",
                         title="Total wrong", actual="10", evidence=["shot:1"])
        before = self.findings_file().read_bytes()
        self.assert_code(errors.EVIDENCE_REF_UNKNOWN, findings_mod.add, self.session,
                         self.record, check_id="chk-1", severity="major", title="x",
                         actual="y", evidence=["shot:1", "request:nope"])
        self.assertEqual(self.findings_file().read_bytes(), before)

    def test_every_ref_kind_resolves(self) -> None:
        self.seed_artifacts()
        cases = {
            "shot:1": ("shot", "shots/0001_120000_cart.png"),
            "shot:0001_120000_cart.png": ("shot", "shots/0001_120000_cart.png"),
            "shot:0001_120000_cart": ("shot", "shots/0001_120000_cart.png"),
            "tree:0001_120000.json": ("tree", "trees/0001_120000.json"),
            "tree:0001_120000": ("tree", "trees/0001_120000.json"),
            "action:0003_tap": ("action", "actions/0003_tap.json"),
            "request:abc": ("request", "network/flows.jsonl"),
            "run:fr_123": ("run", "flows/fr_123"),
            "crash:App-1.ips": ("crash", "crashes/App-1.ips.txt"),
            "crash:logcat:0123abcd": ("crash", "health.json"),
        }
        for ref, (kind, suffix) in cases.items():
            with self.subTest(ref=ref):
                resolved = findings_mod.resolve_ref(self.session, ref)
                self.assertEqual(resolved["ref"], ref)
                self.assertEqual(resolved["kind"], kind)
                self.assertTrue(resolved["path"].endswith(suffix), resolved["path"])
        request = findings_mod.resolve_ref(self.session, "request:abc")
        self.assertEqual(request["data"]["status"], 500)
        shot = findings_mod.resolve_ref(self.session, "shot:1")
        self.assertEqual(shot["data"]["index"], 1)
        log = findings_mod.resolve_ref(
            self.session, "log:2026-10-10T10:00:00Z..2026-10-10T10:05:00.500Z")
        self.assertEqual(log["kind"], "log")
        self.assertEqual(log["data"]["to"], "2026-10-10T10:05:00.500Z")
        finding = findings_mod.add(self.session, self.record, check_id="chk-1",
                                   severity="major", title="Total wrong", expected="9",
                                   actual="10", evidence=list(cases) + ["shot:1"])
        self.assertEqual(finding["evidence"], list(cases))
        single = findings_mod.add(self.session, self.record, check_id=None, severity="info",
                                  title="One ref", actual="a", evidence="shot:1")
        self.assertEqual(single["evidence"], ["shot:1"])

    def test_unresolvable_refs_of_every_kind(self) -> None:
        self.seed_artifacts()
        (self.artifacts / "shots/0002_120001_gone.png").write_bytes(minimal_png())
        shot_mod.append_index(self.session, {"file": "shots/0002_120001_gone.png"})
        (self.artifacts / "shots/0002_120001_gone.png").unlink()
        for ref in ("shot:0", "shot:3", "shot:nope.png", "shot:2", "tree:missing.json",
                    "tree:../journal.ndjson", "tree:/etc/passwd", "action:0009_tap",
                    "request:zzz", "log:yesterday..today", "log:2026-10-10T10:00:00Z",
                    "log:2026-10-10T10:05:00Z..2026-10-10T10:00:00Z", "run:fr_nope",
                    "run:../flows", "crash:nope", "crash:../../x", "video:1", "shot:",
                    "nonsense", ""):
            with self.subTest(ref=ref):
                self.assert_code(errors.EVIDENCE_REF_UNKNOWN, findings_mod.resolve_ref,
                                 self.session, ref)
        self.assert_code(errors.EVIDENCE_REF_UNKNOWN, findings_mod.resolve_ref,
                         self.session, None)

    def test_finding_refusals(self) -> None:
        def add(**kwargs):
            base = dict(check_id="chk-1", severity="major", title="t", actual="a")
            base.update(kwargs)
            return findings_mod.add(self.session, self.record, **base)

        self.assert_code(errors.INVALID_SEVERITY, add, severity="critical")
        self.assert_code(errors.UNKNOWN_CHECK, add, check_id="chk-7")
        self.assert_code(errors.INVALID_VALUE, add, origin="robot")
        self.assert_code(errors.INVALID_VALUE, add, source="gut")
        self.assert_code(errors.INVALID_VALUE, add, title=" ")
        self.assert_code(errors.INVALID_VALUE, add, actual="")
        self.assert_code(errors.INVALID_VALUE, add, confirmed="yes")
        self.assert_code(errors.INVALID_VALUE, add, data=["x"])
        self.assert_code(errors.INVALID_VALUE, add, evidence=[f"log:{i}" for i in range(51)])
        self.assertFalse(self.findings_file().exists())

    def test_add_update_fold(self) -> None:
        first = findings_mod.add(self.session, self.record, check_id="chk-1",
                                 severity="major", title="Total wrong", expected="9",
                                 actual="10 with password=hunter2")
        self.assertEqual(first["finding_id"], "f-1")
        self.assertEqual(first["origin"], "agent")
        self.assertTrue(first["confirmed"])
        self.assertNotIn("hunter2", first["actual"])
        auto = findings_mod.add(self.session, self.record, check_id=None, severity="info",
                                title="New crash", actual="App crashed", origin="auto",
                                source="crash", confirmed=False,
                                data={"key": "crash:x", "token": "abc"})
        self.assertEqual(auto["finding_id"], "f-2")
        self.assertEqual(auto["data"], {"key": "crash:x", "token": "<redacted>"})
        updated = findings_mod.update(self.session, self.record, "f-1",
                                      {"reproduced": "1/3", "stability": "intermittent",
                                       "repro_runs": ["fr_1", "fr_2", "fr_3"],
                                       "draft_flow": "/abs/draft.yaml"})
        self.assertEqual(updated["reproduced"], "1/3")
        folded = findings_mod.fold(self.session, self.record)
        self.assertEqual([f["finding_id"] for f in folded], ["f-1", "f-2"])
        self.assertEqual(folded[0]["stability"], "intermittent")
        self.assertIsNone(folded[1]["stability"])
        schema = json.loads((ROOT / "schemas/pretest-finding-v1.schema.json").read_text())
        for key in schema["$defs"]["state"]["required"]:
            self.assertIn(key, folded[0])
        for line in self.findings_file().read_text().splitlines():
            findings_mod.validate_event(json.loads(line))
        self.assertEqual(stat.S_IMODE(self.findings_file().stat().st_mode), 0o600)

    def test_update_refusals(self) -> None:
        findings_mod.add(self.session, self.record, check_id="chk-1", severity="minor",
                         title="t", actual="a")
        self.assert_code(errors.UNKNOWN_FINDING, findings_mod.update, self.session,
                         self.record, "f-9", {"stability": "stable"})
        for fields in ({}, {"severity": "blocker"}, {"reproduced": "4/3"},
                       {"reproduced": "x"}, {"stability": "sometimes"},
                       {"repro_runs": "fr_1"}, {"draft_flow": 3}):
            with self.subTest(fields=fields):
                self.assert_code(errors.INVALID_VALUE, findings_mod.update, self.session,
                                 self.record, "f-1", fields)
        self.assertEqual(len(self.findings_file().read_text().splitlines()), 1)

    def test_validator_rejects_malformed_events(self) -> None:
        self.assertTrue(findings_mod.event_failures([]))
        self.assertTrue(findings_mod.event_failures({"event": "add", "finding_id": "f-1"}))
        self.assertTrue(findings_mod.event_failures(
            {"event": "update", "finding_id": "f-1", "ts": "2026-10-10T00:00:00Z",
             "journal_seq": 1, "fields": {"confirmed": True}}))
        self.assert_code(errors.INVALID_VALUE, findings_mod.validate_event, {})


class KitTests(unittest.TestCase):
    def test_exactly_the_eighteen_ids_in_order(self) -> None:
        self.assertEqual(kit_mod.ids(), KIT_IDS)
        for case in kit_mod.KIT:
            self.assertEqual(set(case), {"id", "title", "risk", "verbs"})
            self.assertTrue(case["title"] and case["risk"])
            self.assertTrue(case["verbs"])
        schema = json.loads((ROOT / "schemas/pretest-check-v1.schema.json").read_text())
        self.assertEqual(schema["$defs"]["kit_id"]["enum"], KIT_IDS)

    def test_tp016_coverage(self) -> None:
        checks = [
            {"check_id": "chk-1", "case": "offline", "state": "ended", "verdict": "pass",
             "began_at": "t"},
            {"check_id": "chk-2", "case": "server-error", "state": "ended",
             "verdict": "fail", "began_at": "t"},
            {"check_id": "chk-3", "case": "rotation", "state": "added", "verdict": None,
             "began_at": None},
            {"check_id": "chk-4", "case": None, "state": "ended", "verdict": "pass"},
        ]
        coverage = {item["id"]: item for item in kit_mod.coverage(checks)}
        self.assertEqual(list(coverage), KIT_IDS)
        self.assertEqual(coverage["offline"],
                         {"id": "offline", "title": "No network", "covered": True,
                          "checks": ["chk-1"], "verdicts": ["pass"]})
        self.assertTrue(coverage["server-error"]["covered"])
        self.assertEqual(coverage["server-error"]["verdicts"], ["fail"])
        self.assertFalse(coverage["rotation"]["covered"])
        self.assertEqual(coverage["rotation"]["checks"], ["chk-3"])
        self.assertEqual(sum(1 for item in coverage.values() if item["covered"]), 2)


class SchemaTests(unittest.TestCase):
    def test_schema_files_match_the_validators(self) -> None:
        record_schema = json.loads((ROOT / "schemas/pretest-v1.schema.json").read_text())
        self.assertEqual(record_schema["$schema"],
                         "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(record_schema["properties"]["schema"]["const"], "autonom.pretest/v1")
        failures = store._failures_for_record({})  # noqa: SLF001
        for key in record_schema["required"]:
            self.assertIn(f"missing {key}", failures)
        for name in ("pretest-check-v1", "pretest-finding-v1"):
            schema = json.loads((ROOT / f"schemas/{name}.schema.json").read_text())
            self.assertEqual(schema["$schema"],
                             "https://json-schema.org/draft/2020-12/schema")
        check_schema = json.loads((ROOT / "schemas/pretest-check-v1.schema.json").read_text())
        self.assertEqual(check_schema["$defs"]["verdict"]["enum"], list(checks_mod.VERDICTS))
        self.assertEqual(check_schema["$defs"]["kind"]["enum"], list(checks_mod.KINDS))
        finding_schema = json.loads(
            (ROOT / "schemas/pretest-finding-v1.schema.json").read_text())
        self.assertEqual(finding_schema["$defs"]["severity"]["enum"],
                         list(findings_mod.SEVERITIES))
        self.assertEqual(sorted(finding_schema["$defs"]["fields"]["properties"]),
                         sorted(findings_mod.UPDATE_FIELDS))


class RedactionTests(unittest.TestCase):
    """Contract sections 8 and 8.1: the fixed list of free-text forms, masked
    strictly from the start of the value to the end of that line."""

    def assert_exact(self, cases: dict) -> None:
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(store.redact_text(text), expected)

    def test_ticket_text_loses_credentials_and_keeps_other_lines(self) -> None:
        text = ("Authorization: Bearer abc.def.ghi123\n"
                "cookie: session=xyz\n"
                "use token ghp_1234567890abcdef\n"
                "jwt eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl\n"
                "url https://user:pw@h.example/p?x=1\n"
                '{"password": "hunter2"}\n'
                "Steps: open the cart.\n")
        clean = store.redact_text(text)
        for secret in ("abc.def.ghi123", "session=xyz", "ghp_1234567890abcdef",
                       "c2lnbmF0dXJl", ":pw@", "hunter2"):
            self.assertNotIn(secret, clean)
        self.assertIn("Steps: open the cart.\n", clean)
        self.assertIn("x=1", clean)
        self.assertEqual(clean.count("\n"), text.count("\n"))
        self.assertIsNone(store.redact_text(None))
        self.assertEqual(store.redact_text(""), "")

    def test_1_credential_headers_mask_to_the_end_of_the_line(self) -> None:
        self.assert_exact({
            "Authorization: Bearer abc.def": "Authorization: <redacted>",
            "Set-Cookie: sid=1; Path=/": "Set-Cookie: <redacted>",
            "see Proxy-Authorization: Basic Zm9v here": "see Proxy-Authorization: <redacted>",
            "curl -H 'Accept: x' -H 'cookie: a=b; c=d' --header \"Authorization: Basic dXNl\" "
            "https://x.example/p": "curl -H 'Accept: x' -H 'cookie: <redacted>",
            'curl -H "X-Api-Key: k1" -H "X-Auth-Token: t2" -d x': 'curl -H "X-Api-Key: <redacted>',
            "x-auth-token:abc\nnext line": "x-auth-token:<redacted>\nnext line",
        })

    def test_2_auth_scheme_words_in_any_case(self) -> None:
        self.assert_exact({
            "Bearer abc": "Bearer <redacted>",
            "Bearer: abc": "Bearer: <redacted>",
            "bearer: x y z": "bearer: <redacted>",
            "BASIC Zm9v rest": "BASIC <redacted>",
            "Token: a1": "Token: <redacted>",
            "Header copy: Bearer 9f8e7d6c5b4a39281706": "Header copy: Bearer <redacted>",
            "use token ghp_1234567890abcdef": "use token <redacted>",
            # strict: prose after a scheme word is masked too (documented)
            "the token is refreshed every hour.": "the token <redacted>",
            "Bearer tokens expire": "Bearer <redacted>",
        })
        for kept in ("tokens are fine", "a token.", "tokenizer: x", "basically fine"):
            with self.subTest(kept=kept):
                self.assertEqual(store.redact_text(kept), kept)

    def test_3_named_secrets_mask_the_value_to_the_end_of_the_line(self) -> None:
        self.assert_exact({
            "Test account: qa@example.com / Password: Qwerty123!":
                "Test account: qa@example.com / Password: <redacted>",
            "secret: hunter2": "secret: <redacted>",
            "api_key: sk-live-ABCDEFGH12345678": "api_key: <redacted>",
            "API key = abc def": "API key = <redacted>",
            "Access Key: AKIA 1": "Access Key: <redacted>",
            "private-key = abcdef": "private-key = <redacted>",
            "Client Secret: two words": "Client Secret: <redacted>",
            "clientSecret: 'two words' then x": "clientSecret: <redacted>",
            '{"password": "hunter2", "user": "bob"}': '{"password": <redacted>',
            "**Password:** hunter2 again": "**Password:** <redacted>",
            "**Session**: abc": "**Session**: <redacted>",
            "**PIN** 1234 then OK": "**PIN** <redacted>",
            "Auth — xyz 1": "Auth — <redacted>",
            "PIN: 4321, then tap OK": "PIN: <redacted>",
            "otp=123456": "otp=<redacted>",
            "Cookie=abc": "Cookie=<redacted>",
            "user: bob password: x y": "user: bob password: <redacted>",
            "Пароль: 12345 (do not change)":
                "Пароль: <redacted>",
            "Код из СМС — 4321":
                "Код из СМС — <redacted>",
            "**Токен:** abc": "**Токен:** <redacted>",
            "Ключ API = k1": "Ключ API = <redacted>",
            "Секретный код: 7":
                "Секретный код: <redacted>",
            "a\r\nsecret: x\r\nb": "a\r\nsecret: <redacted>\r\nb",
        })

    def test_4_env_lines(self) -> None:
        self.assert_exact({
            "DB_PASSWORD=p@ss MY_TOKEN=abc": "DB_PASSWORD=<redacted>",
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG": "AWS_SECRET_ACCESS_KEY=<redacted>",
            "export MYAUTHX=1": "export MYAUTHX=<redacted>",
            "SESSIONID=abc": "SESSIONID=<redacted>",
            "COOKIE_JAR=/tmp/x": "COOKIE_JAR=<redacted>",
            "API_URL=https://api.example.com": "API_URL=https://api.example.com",
        })

    def test_5_existing_helpers_still_apply(self) -> None:
        self.assertEqual(store.redact_text("jwt eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl"),
                         "jwt <redacted>")
        clean = store.redact_text("url https://user:pw@h.example/p?q=1&x=1")
        self.assertNotIn(":pw@", clean)
        self.assertIn("&x=1", clean)
        self.assertEqual(store.redact_text("form password=x&y=1"), "form password=<redacted>")

    def test_prose_and_tracker_keys_stay_readable(self) -> None:
        for prose in ("Ticket Key: WB-123", "Steps: open the cart", "Expected: total updates",
                      "Time: 12:30", "Monkey: banana", "Keyboard=dark", "Author: John",
                      "Shipping: free", "see http://x.example/a",
                      "Шаги: открыть "
                      "корзину"):
            with self.subTest(prose=prose):
                self.assertEqual(store.redact_text(prose), prose)

    def test_masking_is_linear_on_hostile_input(self) -> None:
        size = 200_000
        for name, text in (("words", "a " * size), ("pairs", "a: " * size),
                           ("stars", "**" * size), ("quotes", '"' * size),
                           ("one word", "Ab" * size + ":"), ("schemes", "token " * size),
                           ("dashes", "a-" * size), ("spaces", "a" + " " * size + "b")):
            with self.subTest(shape=name):
                started = time.monotonic()
                store.redact_text(text)
                self.assertLess(time.monotonic() - started, 5.0)

    def test_round_4_blank_runs_finish_fast(self) -> None:
        """Contract 8.2 item 1: the three named inputs, plus the same shapes
        with Unicode blanks, each well under a second."""
        size = 200_000
        shapes = {"colon spaces": "a:" + " " * size,
                  "cookie tabs": "cookie:" + "\t" * size,
                  "equals spaces": "x =" + " " * size,
                  "nbsp": "password:" + "\u00a0" * size,
                  "em space": "Bearer" + "\u2003" * size,
                  "ideographic": "API" + "\u3000" * size + "key",
                  "name then blanks": "secret" + " " * size + ":",
                  "env blanks": " " * size + "TOKEN=x",
                  "blank run between words": ("a" + " " * 1000) * 200}
        for name, text in shapes.items():
            with self.subTest(shape=name):
                started = time.monotonic()
                store.redact_text(text)
                store.clip(text, 4000)
                self.assertLess(time.monotonic() - started, 1.0)

    def test_no_masking_pattern_has_two_adjacent_blank_runs(self) -> None:
        blank = r"(?:\\s|\[ \\t\]|\[\^\\S\\n\])[*+?]"
        for name, pattern in store.MASKING_PATTERNS.items():
            with self.subTest(pattern=name):
                self.assertIsNone(re.search(blank + r"\)?" + blank, pattern.pattern),
                                  pattern.pattern)

    # contract 8.3 item 2: the hostile shapes, each against every pattern
    ROUND_5_SHAPES = {
        "dash jwt": "-eyJ" * 50_000, "letters": "a" * 200_000,
        "colon blanks": "a:" + " " * 200_000, "schemes": "http" * 50_000,
        "at signs": "@" * 100_000, "dotted jwt": "eyJ." * 50_000,
        "bearer words": "Bearer " * 30_000,
        # beyond the contract list: other runs the patterns could restart in
        "jwt segments": "eyJaaaa.bbbb" * 30_000, "url schemes": "a://" * 50_000,
        "escaped fields": '"password":"\\"' * 30_000, "form fields": "password=" * 30_000,
        "brackets": "[" * 200_000, "percent": "%" * 200_000,
    }

    def test_round_5_every_masking_pattern_is_linear(self) -> None:
        for name, pattern in store.MASKING_PATTERNS.items():
            for shape, text in self.ROUND_5_SHAPES.items():
                for how in ("sub", "search"):
                    with self.subTest(pattern=name, shape=shape, how=how):
                        started = time.monotonic()
                        if how == "sub":
                            pattern.sub("x", text)
                        else:
                            pattern.search(text)
                        self.assertLess(time.monotonic() - started, 1.0)
        for shape, text in {**self.ROUND_5_SHAPES,
                            "url with a bracket key": "https://h/?" + "[" * 200_000 + "=1",
                            "url with bracket groups": "https://h/?" + "[a]" * 60_000 + "=1"
                            }.items():
            for fn in (store.redact_text, lambda value: store.clip(value, 4000)):
                with self.subTest(shape=shape, fn=getattr(fn, "__name__", "clip")):
                    started = time.monotonic()
                    fn(text)
                    self.assertLess(time.monotonic() - started, 1.0)

    def test_round_5_the_pattern_list_is_complete_and_anchored(self) -> None:
        listed = set(map(id, store.MASKING_PATTERNS.values()))
        for name, value in vars(store).items():
            if isinstance(value, re.Pattern) and name not in ("PRETEST_ID", "CHECK_ID"):
                with self.subTest(unlisted=name):
                    self.assertIn(id(value), listed)
        self.assertIn(id(store.redact._FIELD_RE), listed)  # noqa: SLF001
        self.assertIn(id(store.redact._FORM_RE), listed)  # noqa: SLF001
        # a token, name, header or URL starts behind a lookbehind or at "^"
        for name in ("_HEADER", "_SCHEME", "_ENV_LINE", "_PAIR", "_JWT", "_URL"):
            with self.subTest(anchored=name):
                source = re.sub(r"^\(\?[a-z]+\)", "", store.MASKING_PATTERNS[name].pattern)
                self.assertTrue(source.startswith(("(?<!", "(?<=", "^")), source)
        # no \b before a class that also holds "-" (with or without literal
        # letters between them, as in the old "\beyJ[A-Za-z0-9_-]")
        for name, pattern in store.MASKING_PATTERNS.items():
            with self.subTest(word_boundary=name):
                self.assertIsNone(re.search(r"\\b\w*\[[^\]]*-", pattern.pattern),
                                  pattern.pattern)
        self.assertIsNotNone(re.search(r"\\b\w*\[[^\]]*-", r"\beyJ[A-Za-z0-9_-]{4,}"))

    def test_round_5_jwt_still_masks_and_keeps_neighbours(self) -> None:
        token = "eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl"
        self.assertEqual(store.redact_text(f"jwt {token} end"), "jwt <redacted> end")
        self.assertEqual(store.redact_text(f"({token})"), "(<redacted>)")
        # inside a longer token-like run it is part of that run, not a JWT start
        self.assertEqual(store.redact_text(f"x-{token}"), f"x-{token}")
        self.assertEqual(store.redact_text("-eyJ" * 10), "-eyJ" * 10)

    def test_round_5_url_scrubbing_matches_the_shared_helper(self) -> None:
        import random

        rnd = random.Random(5)
        alphabet = "[]a%5D+-tokenT "
        for _ in range(20_000):
            name = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
            self.assertEqual(store._query_key(name),  # noqa: SLF001
                             store.redact._query_key(name), name)  # noqa: SLF001
        for url in ("https://h/p?token=abc&page=2", "https://u:pw@h/p#access_token=x&y=1",
                    "https://h/p?a=1#b", "http://h", "http://u:p@h?token[]=1&Token[0]=2",
                    "https://h/?access%5Ftoken=s&a[[b]=1", "https://h/?key=a=b&&=x"):
            with self.subTest(url=url):
                self.assertEqual(store._scrub_url(url),  # noqa: SLF001
                                 store.redact.scrub_url(url))
        self.assertEqual(store.redact_text("see https://h/?token[0]=s3cr3t&page=2 now"),
                         "see https://h/?token[0]=<redacted>&page=2 now")

    def test_rework_env_value_after_blanks_is_masked(self) -> None:
        # rule 4 with blanks after "=" (contract 8.2 item 2), in joined names
        # that the field rule does not split into words
        cases = {
            "GITHUBTOKEN= ghp_abc123": "GITHUBTOKEN= <redacted>",
            "DBPASSWORD=\thunter2": "DBPASSWORD=\t<redacted>",
            "export AWSSECRET= xyz": "export AWSSECRET= <redacted>",
            "MYAUTHX= v": "MYAUTHX= <redacted>",
            "SESSIONID= 　s1 tail": "SESSIONID= 　<redacted>",
            "line one\nAPIKEY=  k1\nline three": "line one\nAPIKEY=  <redacted>\nline three",
        }
        self.assert_exact(cases)
        for text, expected in cases.items():
            with self.subTest(clip=text):
                self.assertEqual(store.clip(text, 200), expected)
        # nothing after the blanks: nothing to mask; a non-secret name is kept
        for kept in ("GITHUBTOKEN=  ", "DBPASSWORD=\t", "API_URL= https://x.example"):
            with self.subTest(kept=kept):
                self.assertEqual(store.redact_text(kept), kept)

    def test_rework_env_blanks_before_equals_are_masked(self) -> None:
        # rule 4 with blanks before "=" (contract 8.2 item 2; dotenv parsers
        # accept "NAME = value"), in joined names the field rule does not split
        cases = {
            "GITHUBTOKEN = ghp_abc123": "GITHUBTOKEN = <redacted>",
            "DBPASSWORD =hunter2": "DBPASSWORD =<redacted>",
            "DBPASSWORD = hunter2": "DBPASSWORD = <redacted>",
            "export AWSSECRETKEY\t= xyz": "export AWSSECRETKEY\t= <redacted>",
            "export MYAUTHX =　v": "export MYAUTHX =　<redacted>",
            "COOKIEJAR   =   c1 tail": "COOKIEJAR   =   <redacted>",
            "line one\nAPIKEY =k1\r\nline three": "line one\nAPIKEY =<redacted>\r\nline three",
            "DB_PASSWORD = p": "DB_PASSWORD = <redacted>",
        }
        self.assert_exact(cases)
        for text, expected in cases.items():
            with self.subTest(clip=text):
                self.assertEqual(store.clip(text, 200), expected)
        # a blank value, a non-secret name or a lower-case word stay unchanged
        for kept in ("GITHUBTOKEN =  ", "DBPASSWORD \t=", "API_URL = https://x.example",
                     "HOME = /tmp", "x = 1"):
            with self.subTest(kept=kept):
                self.assertEqual(store.redact_text(kept), kept)
        # a long blank run before "=" is walked once
        started = time.monotonic()
        store.redact_text("GITHUBTOKEN" + " " * 400_000 + "= v")
        store.redact_text("GITHUBTOKEN" + " " * 400_000 + "x")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_rework_query_key_is_linear_on_empty_bracket_groups(self) -> None:
        # CPU time, not wall time: under the parallel check run a preempted
        # call counts only the time it actually ran, so load cannot inflate it
        def best(fn, value, calls: int = 1, rounds: int = 2) -> float:
            times = []
            for _ in range(rounds):
                started = time.process_time()
                for _ in range(calls):
                    fn(value)
                times.append(time.process_time() - started)
            return min(times)

        def key(n: int, tail: str = "") -> str:
            return "a" + "[]" * n + tail

        def url(n: int, tail: str = "") -> str:
            return "https://h/?a" + "[]" * n + tail + "=1"

        # the reviewer's 1.6 MB line: the key helpers alone stay fast
        self.assertEqual(store._query_key(key(800_000)), "a")  # noqa: SLF001
        self.assertLess(best(store._query_key, key(800_000)), 1.0)  # noqa: SLF001
        self.assertLess(best(store._scrub_url, url(800_000)), 1.0)  # noqa: SLF001
        self.assertEqual(store._scrub_url(url(800_000)),  # noqa: SLF001
                         store.redact.scrub_url(url(800_000)))
        # a run followed by another character is the shape the old trailing
        # "(\[..\])+$" regex retries at every "[" (over 1 s at this size);
        # the reversed, anchored match fails at once
        self.assertEqual(store._query_key(key(10_000, "x")), key(10_000, "x"))  # noqa: SLF001
        self.assertLess(best(store._query_key, key(10_000, "x")), 0.1)  # noqa: SLF001
        self.assertLess(best(store.redact_text, url(10_000, "x")), 0.2)
        # eight times the input costs about eight times the time; quadratic
        # would be 64. The bound sits between them on a log scale (sqrt(8 *
        # 64) ~ 23), so it leaves about 3x for noise on either side. Each
        # sample repeats the call until the small one takes 20 ms, and the
        # sizes alternate over five rounds so both see the same machine.
        for fn, make in ((store._query_key, key), (store.redact_text, url)):  # noqa: SLF001
            with self.subTest(fn=fn.__name__):
                small_value, large_value = make(50_000), make(400_000)
                calls = 1
                while best(fn, small_value, calls, rounds=1) < 0.02:
                    calls *= 2
                small = large = float("inf")
                for _ in range(5):
                    small = min(small, best(fn, small_value, calls, rounds=1))
                    large = min(large, best(fn, large_value, calls, rounds=1))
                self.assertLess(large / small, 23.0, (calls, small, large))


    def test_round_4_unicode_blanks_are_separators(self) -> None:
        self.assert_exact({
            "Password:\u00a0hunter2": "Password:\u00a0<redacted>",
            "Authorization:\u2003Bearer x": "Authorization:\u2003<redacted>",
            "Bearer\u3000abc": "Bearer\u3000<redacted>",
            "token\u00a0:\u00a0abc": "token\u00a0:\u00a0<redacted>",
            "API\u00a0key\u2003=\u3000k1": "API\u00a0key\u2003=\u3000<redacted>",
            "Client\u00a0\u00a0Secret: x": "Client\u00a0\u00a0Secret: <redacted>",
            "\u3000export\u00a0DB_PASSWORD=p": "\u3000export\u00a0DB_PASSWORD=<redacted>",
            "**PIN**\u00a01234": "**PIN**\u00a0<redacted>",
            "Пароль\u00a0—\u00a0123": "Пароль\u00a0—\u00a0<redacted>",
        })
        # a secret name with nothing after it masks nothing
        for kept in ("Password:", "password: \u00a0 ", "TOKEN=", "Bearer\u3000",
                     "**Password:** "):
            with self.subTest(kept=kept):
                self.assertEqual(store.redact_text(kept), kept)

    def test_round_4_mask_first_then_cut(self) -> None:
        calls: list[str] = []
        real = store.redact_text

        def spy(text):
            calls.append(text)
            return real(text)

        long_tail = "x" * 5000 + " password: y"
        with mock.patch.object(store, "redact_text", side_effect=spy):
            note = checks_mod._text(long_tail, 4000)  # noqa: SLF001
            actual = findings_mod._text("a" * 5000, 4000)  # noqa: SLF001
        # the whole text reaches the masking, the cut comes after it
        self.assertEqual([len(call) for call in calls], [len(long_tail), 5000])
        self.assertEqual(note, "x" * 4000)
        self.assertEqual(actual, "a" * 4000)
        # a URL whose password straddles the limit is masked before the cut
        url = "see https://qa:Sup3rS3cretPassw0rd@h.example/p"
        for limit in range(10, len(url) + 1):
            with self.subTest(limit=limit):
                clipped = store.clip(url, limit)
                self.assertLessEqual(len(clipped), limit)
                for leak in ("Sup3r", "S3cret", "Passw0rd", "rd@h"):
                    self.assertNotIn(leak, clipped)
        # a cut that would split the marker cuts before it, never inside it
        self.assertEqual(store.clip("pin: " + "1" * 10, 8), "pin:")
        self.assertEqual(store.clip("pin: " + "1" * 10, 15), "pin: <redacted>")
        self.assertEqual(store.clip("pin: " + "1" * 10, 14), "pin:")
        for limit in range(0, 40):
            with self.subTest(marker_limit=limit):
                clipped = store.clip("ab Bearer xyz and <redacted>", limit)
                self.assertLessEqual(len(clipped), limit)
                self.assertNotRegex(clipped, r"<(?!redacted>)[a-z]*$")
        self.assertIsNone(store.clip(None, 5))
        self.assertEqual(store.clip("   ", 5), "")
        self.assertIsNone(checks_mod._text("   ", 10))  # noqa: SLF001

    def test_titles_and_baseline_reasons_are_masked_before_the_cut(self) -> None:
        self.assertEqual(
            baseline_mod._unavailable(  # noqa: SLF001
                "adb failed: https://u:topsecretpw@h.example/x " + "y" * 400)["state"],
            "unavailable")
        reason = baseline_mod._unavailable(  # noqa: SLF001
            "adb failed: https://u:topsecretpw@h.example/x " + "y" * 400)["reason"]
        self.assertNotIn("topsecretpw", reason)
        self.assertLessEqual(len(reason), 300)


class AttributionModuleTests(PretestBase):
    def capture(self, **kwargs) -> dict:
        def fake_capture(_target, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(minimal_png())
            return output

        with mock.patch.object(shot_mod, "capture_target", side_effect=fake_capture), \
                mock.patch.object(shot_mod, "foreground", return_value=None), \
                mock.patch.object(shot_mod, "active_mocks",
                                  return_value={"active": 0, "ids": []}):
            return shot_mod.capture_evidence(self.target, self.session, **kwargs)

    def test_tp004_s01_open_check_attributes_journal_shots_and_details(self) -> None:
        record = self.create()
        checks_mod.add(self.session, record, title="Cart", kind="ticket")
        checks_mod.begin(self.session, record, "chk-1")
        journal_mod.record_action(self.session, verb="ui tap", argv=["ui", "tap"],
                                  payload={"ok": True}, ok=True)
        journal_mod.note(self.session, "saw the total")
        result = self.capture(label="cart")
        lines, _total = journal_mod.read(self.session)
        for entry in lines:
            self.assertEqual(entry["pretest_id"], record["pretest_id"])
            self.assertEqual(entry["check_id"], "chk-1")
        row = shot_mod.load_index(self.session)[-1]
        self.assertEqual(row["task"], "chk-1")
        self.assertEqual(row["pretest_id"], record["pretest_id"])
        self.assertEqual(row["check_id"], "chk-1")
        self.assertIn("/shots/chk-1/", result["path"])
        self.assertEqual(shot_mod.read_metadata(Path(result["path"]))["check_id"], "chk-1")
        explicit = self.capture(task="mine")
        self.assertEqual(explicit["metadata"]["task"], "mine")
        self.assertEqual(explicit["metadata"]["check_id"], "chk-1")
        rel = actions_mod.record_detail(self.session, "tap", {"kind": "tap", "x": 1})
        detail = json.loads((Path(self.session["artifacts_dir"]) / rel).read_text())
        self.assertEqual(detail["task"], "chk-1")
        self.assertEqual(detail["pretest_id"], record["pretest_id"])
        self.assertEqual(detail["check_id"], "chk-1")
        # the open check finding a shot by its new name
        ref = findings_mod.resolve_ref(self.session, "shot:" + Path(result["path"]).name)
        self.assertEqual(ref["data"]["task"], "chk-1")
        # after the check ends, entries keep the pre-test id only
        checks_mod.end(self.session, record, "chk-1", verdict="pass")
        journal_mod.note(self.session, "between checks")
        last = journal_mod.read(self.session)[0][-1]
        self.assertEqual(last["pretest_id"], record["pretest_id"])
        self.assertNotIn("check_id", last)
        between = self.capture()
        self.assertIsNone(between["metadata"]["task"])
        self.assertNotIn("check_id", between["metadata"])

    def test_tp004_s02_no_pretest_no_new_keys(self) -> None:
        journal_mod.record_action(self.session, verb="ui tap", argv=["ui", "tap"],
                                  payload={"ok": True}, ok=True)
        journal_mod.note(self.session, "plain")
        for entry in journal_mod.read(self.session)[0]:
            self.assertNotIn("pretest_id", entry)
            self.assertNotIn("check_id", entry)
        self.assertEqual(set(journal_mod.read(self.session)[0][0]),
                         {"seq", "ts", "kind", "verb", "argv", "ok", "origin"})
        self.capture(label="plain")
        row = shot_mod.load_index(self.session)[-1]
        self.assertEqual(set(row), {"captured_at", "label", "task", "session_id", "platform",
                                    "target_id", "app_id", "foreground", "mocks_active",
                                    "file", "width", "height"})
        payload = {"kind": "tap", "x": 1}
        rel = actions_mod.record_detail(self.session, "tap", payload)
        text = (Path(self.session["artifacts_dir"]) / rel).read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(payload, ensure_ascii=False, indent=2))
        self.assertEqual(payload, {"kind": "tap", "x": 1})
        self.assertFalse(self.pretests().exists())

    def test_a_stopped_pretest_stops_attributing(self) -> None:
        self.create()
        store.stop(self.session)
        journal_mod.note(self.session, "after")
        self.assertNotIn("pretest_id", journal_mod.read(self.session)[0][-1])

    def test_attribution_failure_never_loses_an_entry(self) -> None:
        self.create()
        with mock.patch.object(store, "attribution", side_effect=RuntimeError("boom")):
            journal_mod.note(self.session, "still written")
            rel = actions_mod.record_detail(self.session, "tap", {"kind": "tap"})
        self.assertEqual(journal_mod.read(self.session)[0][-1]["text"], "still written")
        self.assertIsNotNone(rel)


ATTRIBUTION_DUMP = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,2400]">
        <node index="0" text="Continue" resource-id="com.example.app:id/next" class="android.widget.Button" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[0,0][200,100]" />
      </node>
    </hierarchy>
    """)


class AttributionCliTests(unittest.TestCase):
    """tp-004-S01/S02 through the real CLI choke point and the fake adb."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        # While a pre-test is active a coordinate tap needs a readable tree
        # (DEC-007), so the fake device serves one with a harmless node there.
        dump = self.cwd / "dump.xml"
        dump.write_text(ATTRIBUTION_DUMP, encoding="utf-8")
        state = self.cwd / "state.json"
        state.write_text(json.dumps({"devices": [["emulator-5554", "device", ""]],
                                     "ui_dump": str(dump)}),
                         encoding="utf-8")
        self.env = {**os.environ, "AUTONOM_FAKE_STATE": str(state),
                    "AUTONOM_HOME": str(self.cwd / "home")}
        patch = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.cwd / "home")})
        patch.start()
        self.addCleanup(patch.stop)

    def cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *argv, "--adb", str(FAKE_ADB)],
                              capture_output=True, text=True, env=self.env, cwd=self.cwd,
                              timeout=60)

    def session(self) -> dict:
        found = list((self.cwd / "home/sessions").glob("s_*/session.json"))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text(encoding="utf-8"))

    def journal(self, session: dict) -> list[dict]:
        path = Path(session["artifacts_dir"]) / "journal.ndjson"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_cli_entries_and_screenshot_rows_carry_the_open_check(self) -> None:
        self.assertEqual(self.cli("session", "start", "--serial", "emulator-5554",
                                  "--app-id", "com.example.app").returncode, 0)
        session = self.session()
        self.assertEqual(self.cli("ui", "tap", "--x", "1", "--y", "2").returncode, 0)
        plain = self.journal(session)[-1]
        self.assertEqual(plain["verb"], "ui tap")
        self.assertNotIn("pretest_id", plain)
        self.assertNotIn("check_id", plain)

        target = SimpleNamespace(platform="android", target_id="emulator-5554")
        with mock.patch.object(device_state, "crash_list", return_value=[]), \
                mock.patch.object(snapshot_mod, "take",
                                  side_effect=errors.AutonomError("x", "no metrics")):
            record = store.create(session, target=target, ticket_text="t", ticket_ref=None,
                                  title=None, repo=None, base=None, head=None,
                                  flows_dir=None, app_id=None, budget=None,
                                  context={"schema": "autonom.pretest-context/v1"})
        checks_mod.add(session, record, title="Tap", kind="ticket")
        checks_mod.begin(session, record, "chk-1")
        tap = self.cli("ui", "tap", "--x", "10", "--y", "20")
        self.assertEqual(tap.returncode, 0, tap.stderr)
        shot = self.cli("screenshot")
        self.assertEqual(shot.returncode, 0, shot.stderr)
        entries = self.journal(session)
        tap_entry = [e for e in entries if e.get("verb") == "ui tap"][-1]
        shot_entry = [e for e in entries if e.get("verb") == "screenshot"][-1]
        for entry in (tap_entry, shot_entry):
            self.assertEqual(entry["pretest_id"], record["pretest_id"])
            self.assertEqual(entry["check_id"], "chk-1")
        row = shot_mod.load_index(session)[-1]
        self.assertEqual(row["task"], "chk-1")
        self.assertEqual(row["pretest_id"], record["pretest_id"])


CONCURRENT = """
import json, sys
sys.path.insert(0, {scripts!r})
from autonom_lib.pretest import checks, findings, store
session = json.loads({session!r})
record = store.load(session, {pretest_id!r})
for index in range(12):
    checks.add(session, record, title=f"{{sys.argv[1]}}-{{index}}", kind="ticket")
    findings.add(session, record, check_id=None, severity="info",
                 title=f"{{sys.argv[1]}}-{{index}}", actual="x")
"""


class ConcurrencyTests(PretestBase):
    def test_two_processes_append_without_losing_or_reusing_ids(self) -> None:
        record = self.create()
        script = CONCURRENT.format(scripts=str(ROOT / "scripts"),
                                   session=json.dumps(self.session),
                                   pretest_id=record["pretest_id"])
        workers = [subprocess.Popen([sys.executable, "-c", script, name],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env={**os.environ})
                   for name in ("a", "b")]
        for worker in workers:
            _out, err = worker.communicate(timeout=120)
            self.assertEqual(worker.returncode, 0, err.decode())
        folded = checks_mod.fold(self.session, record)
        self.assertEqual([c["check_id"] for c in folded],
                         [f"chk-{n}" for n in range(1, 25)])
        self.assertEqual(sorted(c["title"] for c in folded),
                         sorted([f"a-{i}" for i in range(12)] + [f"b-{i}" for i in range(12)]))
        found = findings_mod.fold(self.session, record)
        self.assertEqual([f["finding_id"] for f in found], [f"f-{n}" for n in range(1, 25)])
        path = self.pretests() / record["pretest_id"] / "checks.ndjson"
        self.assertEqual(len(path.read_text().splitlines()), 24)


class ReadLinesTests(unittest.TestCase):
    def test_torn_deep_and_non_object_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.ndjson"
            path.write_text("\n".join([
                '{"a": 1}', "[" * 1_000_000 + "]" * 1_000_000, "[1]", "3",
                '{"b": 2}', '{"torn": ']) + "\n", encoding="utf-8")
            self.assertEqual(store.read_lines(path), [{"a": 1}, {"b": 2}])
            self.assertEqual(store.read_lines(Path(tmp) / "missing"), [])

    def test_an_object_nested_too_deep_to_print_is_skipped(self) -> None:
        # json.loads reads it, but str() of the id would raise RecursionError
        # in every reader that compares ids (repro, findings).
        deep = "[" * 100_000 + "]" * 100_000
        ok = "[" * 50 + "]" * 50
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.ndjson"
            path.write_text("\n".join([
                '{"id": %s}' % deep, '{"id": "a", "v": %s}' % ok]) + "\n",
                encoding="utf-8")
            rows = store.read_lines(path)
            self.assertEqual([row["id"] for row in rows], ["a"])
            self.assertEqual(str(rows[0]["v"]).count("["), 50)


if __name__ == "__main__":
    unittest.main()
