"""The host-wide control lease store (contract section 2).

Every case of `tests/fixtures/control_cases.json` (shared with the Node
Canvas suite) runs through `control.admission` and `control.lease_path`; the
writes (`acquire`, `release`, `renew`) and `status` run against a temporary
`AUTONOM_HOME` only, including a 20-process acquire race.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import control, errors  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/control_cases.json"
SERIAL = "emulator-5584"
NOW = calendar.timegm((2026, 10, 10, 14, 0, 0)) * 1000  # 2026-10-10T14:00:00.000Z
AGENT = "agent:worker-1"
OTHER = "agent:other"
HUMAN = "canvas:3277:41234"
STATUS_KEYS = ["platform", "target_id", "state", "generation", "holder", "expires_at",
               "remaining_ms", "session_id", "updated_at", "took_over_from"]
REFUSAL_KEYS = {"platform", "target_id", "capability", "generation", "holder", "expires_at"}


def _claim(raw: dict | None) -> control.Claim | None:
    if raw is None:
        return None
    return control.Claim(raw["holder_id"], raw.get("generation"))


def _files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class _Home(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.home = self.sandbox_home()
        self.set_env(XDG_STATE_HOME=None, AUTONOM_TEST_MODE=None, AUTONOM_CONTROL_NOW_MS=None,
                     AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None)
        control.end_invocation()
        self.addCleanup(control.end_invocation)

    def lease_file(self, target: str = SERIAL, platform: str = "android") -> Path:
        return control.lease_path(platform, target)

    def refused(self, code: str, call, *args, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.as_dict())
        return caught.exception


class FixtureTests(unittest.TestCase):
    """The shared fixture: Python is the reference the Node suite matches."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_covers_every_admission_row(self) -> None:
        self.assertEqual(self.fixture["schema"], 1)
        rows = {case["row"] for case in self.fixture["admission"]}
        self.assertEqual(rows, set(range(1, 11)))
        names = [case["name"] for case in self.fixture["admission"]]
        self.assertEqual(len(names), len(set(names)), "case names must be unique")
        boundary = [case for case in self.fixture["admission"]
                    if case["lease"] and case["lease"]["holder"]
                    and case["now_ms"] == case["lease"]["expires_at_ms"]]
        self.assertTrue(boundary, "a now_ms == expires_at_ms case is required")
        targets = {case["target_id"] for case in self.fixture["paths"]}
        self.assertIn("emulator-5584", targets)
        self.assertIn("127.0.0.1:5555", targets)
        self.assertIn("45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", targets)
        self.assertTrue(any(len(target) == 200 for target in targets))

    def test_every_admission_case(self) -> None:
        for case in self.fixture["admission"]:
            with self.subTest(case=case["name"]):
                refusal = control.admission(case["lease"], _claim(case["claim"]), case["now_ms"])
                got = "admitted" if refusal is None else refusal["code"]
                self.assertEqual(got, case["expect"])
                if refusal is not None:
                    self.assertTrue(REFUSAL_KEYS <= set(refusal), refusal)
                    self.assertEqual(refusal["capability"], "control.lease")
                    self.assertTrue(refusal["message"] and refusal["hint"])
                    if case["claim"] is not None:
                        self.assertEqual(refusal["claimed_holder"], case["claim"]["holder_id"])
                        self.assertEqual(refusal["claimed_generation"], case["claim"]["generation"])
                    else:
                        self.assertNotIn("claimed_holder", refusal)

    def test_every_path_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"AUTONOM_HOME": tmp}):
            for case in self.fixture["paths"]:
                with self.subTest(target=case["target_id"][:40]):
                    path = control.lease_path(case["platform"], case["target_id"])
                    self.assertEqual(path.name, case["file"])
                    self.assertEqual(path.parent, Path(tmp) / "control")
            self.assertEqual(os.listdir(tmp), [], "lease_path must create nothing")

    def test_lease_state_boundaries(self) -> None:
        held = {"schema": 1, "platform": "android", "target_id": SERIAL, "generation": 2,
                "holder": {"kind": "agent", "id": AGENT, "label": "a"}, "expires_at_ms": 5000}
        self.assertEqual(control.lease_state(None, 0), "none")
        self.assertEqual(control.lease_state({**held, "holder": None, "generation": 0}, 0), "none")
        self.assertEqual(control.lease_state({**held, "holder": None}, 0), "free")
        self.assertEqual(control.lease_state(held, 4999), "held")
        self.assertEqual(control.lease_state(held, 5000), "expired")
        self.assertEqual(control.lease_state({**held, "schema": 2}, 0), "none")

    def test_refusal_message_names_kind_label_and_generation(self) -> None:
        lease = {"schema": 1, "platform": "android", "target_id": SERIAL, "generation": 4,
                 "holder": {"kind": "human", "id": HUMAN, "label": "Canvas 3277"},
                 "expires_at": "x", "expires_at_ms": 5000}
        refusal = control.admission(lease, None, 1000)
        self.assertEqual(refusal["code"], "control_held")
        self.assertIn("human 'Canvas 3277' (generation 4)", refusal["message"])
        self.assertIn(f"--target {SERIAL}", refusal["hint"])
        stale = control.admission(lease, control.Claim(AGENT, 3), 1000)
        self.assertIn("generation 4 now", stale["hint"])


class WriteTests(_Home):
    def test_acquire_happy_path_shape_and_modes(self) -> None:
        answer = control.acquire("android", SERIAL, holder_id=AGENT, label="worker",
                                 session_id="s_0123456789", now_ms=NOW)
        self.assertEqual({key: answer[key] for key in
                          ("ok", "acquired", "renewed", "taken_over", "generation", "state")},
                         {"ok": True, "acquired": True, "renewed": False, "taken_over": False,
                          "generation": 1, "state": "held"})
        lease = answer["lease"]
        self.assertEqual(list(lease), [
            "schema", "platform", "target_id", "generation", "holder", "session_id",
            "acquired_at", "renewed_at", "expires_at", "expires_at_ms", "ttl_ms",
            "previous_holder", "reason", "updated_at", "updated_at_ms"])
        self.assertEqual(lease["holder"], {"kind": "agent", "id": AGENT, "label": "worker"})
        self.assertEqual(lease["ttl_ms"], control.DEFAULT_AGENT_TTL_S * 1000)
        self.assertEqual(lease["expires_at_ms"], NOW + 60_000)
        self.assertEqual(lease["acquired_at"], "2026-10-10T14:00:00.000Z")
        self.assertEqual(lease["expires_at"], "2026-10-10T14:01:00.000Z")
        self.assertEqual(lease["reason"], "acquired")
        self.assertIsNone(lease["previous_holder"])
        self.assertEqual(json.loads(self.lease_file().read_text()), lease)
        self.assertEqual(control.read_lease("android", SERIAL), lease)
        directory = self.home / "control"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / ".lock").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.lease_file().stat().st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(directory)), [".lock", "android__emulator-5584.json"])

    def test_human_default_ttl_and_label_default(self) -> None:
        answer = control.acquire("android", SERIAL, holder_id=HUMAN, kind="human", now_ms=NOW)
        self.assertEqual(answer["lease"]["ttl_ms"], control.HUMAN_TTL_S * 1000)
        self.assertEqual(answer["lease"]["holder"]["label"], HUMAN)
        long_id = "agent:" + "x" * 120
        answer = control.acquire("android", "emulator-5586", holder_id=long_id, now_ms=NOW)
        self.assertEqual(answer["lease"]["holder"]["label"], long_id[:80])

    def test_generation_sequence_1_2_3_4(self) -> None:
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 1)
        released = control.release("android", SERIAL, holder_id=AGENT, generation=1, now_ms=NOW + 1)
        self.assertEqual(released["lease"]["generation"], 1)
        self.assertIsNone(released["lease"]["holder"])
        self.assertEqual(released["lease"]["reason"], "released")
        self.assertFalse(released["was_expired"])
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW + 2)["generation"], 2)
        taken = control.acquire("android", SERIAL, holder_id=HUMAN, kind="human", label="Canvas 3277",
                                takeover=True, now_ms=NOW + 3)
        self.assertEqual(taken["generation"], 3)
        self.assertTrue(taken["taken_over"])
        self.assertEqual(taken["lease"]["reason"], "taken_over")
        self.assertEqual(taken["lease"]["previous_holder"]["id"], AGENT)
        # Status names the replaced holder while the takeover lease is held, renewals included.
        shown = control.status("android", SERIAL, now_ms=NOW + 3)[0]
        self.assertEqual(shown["took_over_from"]["id"], AGENT)
        control.renew("android", SERIAL, holder_id=HUMAN, generation=3, now_ms=NOW + 3)
        self.assertEqual(control.status("android", SERIAL, now_ms=NOW + 3)[0]["took_over_from"]["id"], AGENT)
        # The replaced agent learns its lease was replaced.
        self.refused("control_generation_stale", control.admit, "android", SERIAL,
                     control.Claim(AGENT, 2), now_ms=NOW + 4)
        control.release("android", SERIAL, holder_id=HUMAN, now_ms=NOW + 5)
        again = control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW + 6)
        self.assertEqual(again["generation"], 4)
        self.assertIsNone(again["lease"]["previous_holder"])
        self.assertIsNone(control.status("android", SERIAL, now_ms=NOW + 6)[0]["took_over_from"])

    def test_acquire_by_the_same_live_holder_renews_in_place(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)
        answer = control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=120, now_ms=NOW + 1000)
        self.assertTrue(answer["renewed"])
        self.assertEqual(answer["generation"], 1)
        self.assertEqual(answer["lease"]["expires_at_ms"], NOW + 1000 + 120_000)
        self.assertEqual(answer["lease"]["acquired_at"], control._iso(NOW))
        self.assertEqual(answer["lease"]["reason"], "renewed")

    def test_acquire_over_another_live_holder(self) -> None:
        control.acquire("android", SERIAL, holder_id=HUMAN, kind="human", label="Canvas 3277", now_ms=NOW)
        error = self.refused("control_held", control.acquire, "android", SERIAL,
                             holder_id=AGENT, now_ms=NOW + 1)
        payload = error.as_dict()
        self.assertEqual(payload["holder"]["id"], HUMAN)
        self.assertEqual(payload["generation"], 1)
        self.assertEqual(payload["capability"], "control.lease")
        self.assertNotIn("takeover_requires", payload)
        error = self.refused("control_held", control.acquire, "android", SERIAL,
                             holder_id=AGENT, takeover=True, now_ms=NOW + 1)
        self.assertEqual(error.as_dict()["takeover_requires"], "human")
        self.assertEqual(control.read_lease("android", SERIAL)["generation"], 1)
        # An expired holder is replaced without takeover; not counted as taken over.
        later = control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW + 31_000)
        self.assertEqual(later["generation"], 2)
        self.assertFalse(later["taken_over"])
        self.assertEqual(later["lease"]["reason"], "acquired")

    def test_renew(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=10, now_ms=NOW)
        answer = control.renew("android", SERIAL, holder_id=AGENT, generation=1, now_ms=NOW + 5000)
        self.assertEqual(answer["generation"], 1)
        self.assertTrue(answer["renewed"] and answer["acquired"])
        # no ttl: the lease keeps its own lifetime, not the kind default
        self.assertEqual(answer["lease"]["expires_at_ms"], NOW + 5000 + 10_000)
        answer = control.renew("android", SERIAL, holder_id=AGENT, generation=1, ttl_s=5,
                               now_ms=NOW + 6000)
        self.assertEqual(answer["lease"]["expires_at_ms"], NOW + 11_000)
        self.refused("control_generation_stale", control.renew, "android", SERIAL,
                     holder_id=AGENT, generation=2, now_ms=NOW + 7000)
        self.refused("control_held", control.renew, "android", SERIAL,
                     holder_id=OTHER, generation=1, now_ms=NOW + 7000)
        self.refused("control_not_held", control.renew, "android", "emulator-5586",
                     holder_id=AGENT, generation=1, now_ms=NOW)

    def test_a_renewal_without_a_ttl_keeps_the_lease_own_ttl(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=600, now_ms=NOW)
        answer = control.renew("android", SERIAL, holder_id=AGENT, generation=1, now_ms=NOW + 5000)
        self.assertEqual(answer["lease"]["ttl_ms"], 600_000)
        self.assertEqual(answer["lease"]["expires_at_ms"], NOW + 5000 + 600_000)
        # the same holder acquiring again renews in place and keeps it too
        again = control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW + 6000)
        self.assertTrue(again["renewed"])
        self.assertEqual(again["lease"]["ttl_ms"], 600_000)
        self.assertEqual(again["lease"]["expires_at_ms"], NOW + 6000 + 600_000)
        # an explicit ttl still wins, and later renewals keep that one
        control.renew("android", SERIAL, holder_id=AGENT, generation=1, ttl_s=120,
                      now_ms=NOW + 7000)
        kept = control.renew("android", SERIAL, holder_id=AGENT, generation=1, now_ms=NOW + 8000)
        self.assertEqual(kept["lease"]["expires_at_ms"], NOW + 8000 + 120_000)
        # a human lease keeps its own ttl as well
        control.acquire("android", "emulator-5586", holder_id=HUMAN, kind="human",
                        label="Canvas 3277", ttl_s=300, now_ms=NOW)
        human = control.renew("android", "emulator-5586", holder_id=HUMAN, generation=1,
                              now_ms=NOW + 1000)
        self.assertEqual(human["lease"]["expires_at_ms"], NOW + 1000 + 300_000)

    def test_a_renewal_over_an_unusable_stored_ttl_uses_the_kind_default(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=600, now_ms=NOW)
        path = self.lease_file()
        for stored in ("600", 1_500, 9_999_000, None, True):
            lease = json.loads(path.read_text(encoding="utf-8"))
            lease["ttl_ms"] = stored
            path.write_text(json.dumps(lease), encoding="utf-8")
            answer = control.renew("android", SERIAL, holder_id=AGENT, generation=1,
                                   now_ms=NOW + 1000)
            self.assertEqual(answer["lease"]["expires_at_ms"],
                             NOW + 1000 + control.DEFAULT_AGENT_TTL_S * 1000, stored)

    def test_renew_after_expiry_is_refused(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=5, now_ms=NOW)
        before = self.lease_file().read_bytes()
        error = self.refused("control_lease_expired", control.renew, "android", SERIAL,
                             holder_id=AGENT, generation=1, now_ms=NOW + 5000)
        self.assertIsNotNone(error.as_dict()["expires_at"])
        self.assertEqual(self.lease_file().read_bytes(), before, "no revival")

    def test_release_rules(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=5, now_ms=NOW)
        self.refused("control_not_held", control.release, "android", SERIAL,
                     holder_id=OTHER, now_ms=NOW)
        self.refused("control_generation_stale", control.release, "android", SERIAL,
                     holder_id=AGENT, generation=2, now_ms=NOW)
        answer = control.release("android", SERIAL, holder_id=AGENT, now_ms=NOW + 9000)
        self.assertTrue(answer["released"] and answer["was_expired"])
        self.assertEqual(answer["lease"]["generation"], 1)
        self.refused("control_not_held", control.release, "android", SERIAL,
                     holder_id=AGENT, now_ms=NOW + 9001)

    def test_status_entries(self) -> None:
        self.assertEqual(control.status(now_ms=NOW), [])
        control.acquire("android", SERIAL, holder_id=AGENT, session_id="s_1", now_ms=NOW)
        control.acquire("ios", "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", holder_id=HUMAN,
                        kind="human", now_ms=NOW)
        entries = control.status(now_ms=NOW + 1000)
        self.assertEqual([entry["platform"] for entry in entries], ["android", "ios"])
        android = entries[0]
        self.assertEqual(list(android), STATUS_KEYS)
        self.assertEqual(android["state"], "held")
        self.assertEqual(android["remaining_ms"], 59_000)
        self.assertEqual(android["session_id"], "s_1")
        self.assertEqual(control.status("ios", now_ms=NOW)[0]["holder"]["kind"], "human")
        self.assertEqual(control.status(target_id=SERIAL, now_ms=NOW)[0]["target_id"], SERIAL)
        one = control.status("android", SERIAL, now_ms=NOW + 61_000)
        self.assertEqual((one[0]["state"], one[0]["remaining_ms"]), ("expired", 0))
        control.release("android", SERIAL, holder_id=AGENT, now_ms=NOW + 62_000)
        free = control.status("android", SERIAL, now_ms=NOW + 62_000)[0]
        self.assertEqual((free["state"], free["remaining_ms"], free["generation"], free["holder"],
                          free["expires_at"]), ("free", None, 1, None, None))
        self.assertEqual(control.status("android", "emulator-5586", now_ms=NOW), [])

    def test_reads_create_nothing(self) -> None:
        before = _files(self.home)
        self.assertIsNone(control.read_lease("android", SERIAL))
        result = control.admit("android", SERIAL, None, now_ms=NOW)
        self.assertEqual((result["leased"], result["generation"], result["holder_id"]),
                         (False, None, None))
        self.assertRegex(result["operation_id"], r"^op_[0-9a-f]{12}$")
        self.refused("control_not_held", control.admit, "android", SERIAL, control.Claim(AGENT))
        self.assertEqual(control.status(now_ms=NOW), [])
        self.assertEqual(control.status("android", SERIAL, now_ms=NOW), [])
        self.refused("control_not_held", control.release, "android", SERIAL, holder_id=AGENT)
        self.refused("control_not_held", control.renew, "android", SERIAL,
                     holder_id=AGENT, generation=1)
        self.assertEqual(_files(self.home), before)
        self.assertFalse((self.home / "control").exists())
        # With the directory present, a release on another target still writes nothing.
        control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)
        before = _files(self.home)
        self.refused("control_not_held", control.release, "android", "emulator-5586", holder_id=AGENT)
        self.assertEqual(_files(self.home), before)

    def test_unreadable_file_is_no_lease_and_next_grant_continues(self) -> None:
        directory = control.control_dir()
        path = self.lease_file()
        path.write_bytes(b'{"schema": 1, "generation": 7, "holder": {"kind": "agent", "id": "x"')
        self.assertIsNone(control.read_lease("android", SERIAL))
        self.assertEqual(control.admit("android", SERIAL, None, now_ms=NOW)["leased"], False)
        entry = control.status("android", SERIAL, now_ms=NOW)[0]
        self.assertEqual(entry["state"], "unreadable")
        self.assertEqual(control.status(now_ms=NOW)[0]["state"], "unreadable")
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 8)
        self.assertEqual(control.read_lease("android", SERIAL)["generation"], 8)
        # Schema mismatch: also no lease; the generation pattern still applies.
        path.write_text(json.dumps({"schema": 2, "generation": 11}))
        self.assertIsNone(control.read_lease("android", SERIAL))
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 12)
        # Nothing recoverable: the generation restarts at 1 (accepted residual).
        path.write_bytes(b"\x00garbage")
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 1)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_temporary_leftovers_ignored_and_swept(self) -> None:
        directory = control.control_dir()
        old = directory / ".lease-crashed.tmp"
        fresh = directory / ".lease-writing.tmp"
        old.write_text("{")
        fresh.write_text("{")
        stale = time.time() - 120
        os.utime(old, (stale, stale))
        self.assertEqual(control.status(now_ms=NOW), [])
        control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(len(control.status(now_ms=NOW)), 1)

    def test_failed_write_leaves_the_old_file_and_no_temporary(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)
        before = self.lease_file().read_bytes()
        with mock.patch.object(control.os, "replace", side_effect=OSError(28, "No space left")):
            error = self.refused("output_not_writable", control.release, "android", SERIAL,
                                 holder_id=AGENT, now_ms=NOW)
        self.assertIn("No space left", error.message)
        self.assertEqual(error.extra["path"], str(self.home / "control"))
        self.assertEqual(self.lease_file().read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.home / "control")),
                         [".lock", "android__emulator-5584.json"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_unwritable_store_is_an_error_envelope(self) -> None:
        self.home.chmod(0o500)
        self.addCleanup(self.home.chmod, 0o700)
        error = self.refused("output_not_writable", control.acquire, "android", SERIAL,
                             holder_id=AGENT, now_ms=NOW)
        self.assertEqual(error.as_dict()["capability"], "control.lease")
        self.assertTrue(error.hint)

    def test_validation_refuses_before_any_file(self) -> None:
        bad = [
            dict(holder_id="bad holder"),
            dict(holder_id=""),
            dict(holder_id="a" * 129),
            dict(holder_id=AGENT, kind="robot"),
            dict(holder_id=AGENT, label="x" * 81),
            dict(holder_id=AGENT, label=""),
            dict(holder_id=AGENT, label="two\nlines"),
            dict(holder_id=AGENT, ttl_s=4),
            dict(holder_id=AGENT, ttl_s=3601),
            dict(holder_id=AGENT, ttl_s=True),
            dict(holder_id=AGENT, ttl_s=30.5),
            dict(holder_id=AGENT, session_id="not a session"),
        ]
        for kwargs in bad:
            with self.subTest(**{key: repr(value)[:30] for key, value in kwargs.items()}):
                error = self.refused("invalid_value", control.acquire, "android", SERIAL, **kwargs)
                self.assertTrue(error.hint)
        self.refused("invalid_value", control.acquire, "android", "", holder_id=AGENT)
        self.refused("invalid_value", control.renew, "android", SERIAL, holder_id=AGENT, generation=None)
        self.refused("invalid_value", control.renew, "android", SERIAL, holder_id=AGENT, generation=0)
        self.refused("invalid_value", control.release, "android", SERIAL, holder_id=AGENT,
                     generation="x")
        self.refused("invalid_value", control.admit, "android", SERIAL, None, operation_id="bad id")
        self.refused("invalid_value", control.admit, "android", SERIAL, None, operation_id="o" * 65)
        self.assertFalse((self.home / "control").exists())
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=5, label="x" * 80,
                                         now_ms=NOW)["lease"]["ttl_ms"], 5000)
        self.assertEqual(control.acquire("android", "emulator-5586", holder_id="a@b.c:d_e-f",
                                         ttl_s=3600, now_ms=NOW)["generation"], 1)

    def test_admit_results_and_refusal_payload(self) -> None:
        control.acquire("android", SERIAL, holder_id=HUMAN, kind="human", label="Canvas 3277", now_ms=NOW)
        error = self.refused("control_held", control.admit, "android", SERIAL, None, now_ms=NOW + 1)
        payload = error.as_dict()
        self.assertEqual(payload["error_code"], "control_held")
        self.assertEqual(payload["holder"], {"kind": "human", "id": HUMAN, "label": "Canvas 3277"})
        self.assertEqual((payload["platform"], payload["target_id"], payload["generation"]),
                         ("android", SERIAL, 1))
        self.assertEqual(payload["capability"], "control.lease")
        self.assertTrue(payload["hint"])
        self.assertIsNotNone(payload["expires_at"])
        self.assertNotIn("claimed_holder", payload)
        error = self.refused("control_held", control.admit, "android", SERIAL,
                             control.Claim(AGENT, 1), now_ms=NOW + 1)
        self.assertEqual((error.as_dict()["claimed_holder"], error.as_dict()["claimed_generation"]),
                         (AGENT, 1))
        result = control.admit("android", SERIAL, control.Claim(HUMAN, 1), now_ms=NOW + 1,
                               operation_id="run-1:4")
        self.assertEqual(result, {"leased": True, "generation": 1, "holder_id": HUMAN,
                                  "operation_id": "run-1:4"})
        control.release("android", SERIAL, holder_id=HUMAN, now_ms=NOW + 2)
        result = control.admit("android", SERIAL, None, now_ms=NOW + 3)
        self.assertEqual((result["leased"], result["generation"], result["holder_id"]), (False, 1, None))


RACE_SCRIPT = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from autonom_lib import control, errors
holder, start = sys.argv[2], float(sys.argv[3])
while time.time() < start:
    time.sleep(0.001)
try:
    answer = control.acquire("android", "emulator-5584", holder_id=holder)
    print(json.dumps({"result": "acquired", "generation": answer["generation"],
                      "renewed": answer["renewed"]}))
except errors.AutonomError as exc:
    print(json.dumps({"result": exc.code}))
"""


class RaceTests(_Home):
    def test_twenty_processes_race_for_one_free_target(self) -> None:
        control.acquire("android", SERIAL, holder_id="agent:seed", now_ms=NOW)
        control.release("android", SERIAL, holder_id="agent:seed", now_ms=NOW)
        start = time.time() + 1.5
        env = {**os.environ, "AUTONOM_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("AUTONOM_TEST_MODE", None)
        procs = [subprocess.Popen(
            [sys.executable, "-c", RACE_SCRIPT, str(ROOT / "scripts"), f"agent:racer-{index}",
             repr(start)],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True) for index in range(20)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out.strip().splitlines()[-1]))
        winners = [result for result in results if result["result"] == "acquired"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(winners[0]["generation"], 2)
        self.assertFalse(winners[0]["renewed"])
        self.assertEqual(sum(result["result"] == "control_held" for result in results), 19, results)
        lease = control.read_lease("android", SERIAL)
        self.assertEqual(lease["generation"], 2)
        self.assertTrue(lease["holder"]["id"].startswith("agent:racer-"))
        self.assertEqual([name for name in os.listdir(self.home / "control") if name.endswith(".tmp")], [])


class ClaimAndClockTests(_Home):
    def test_claim_from_precedence(self) -> None:
        self.assertIsNone(control.claim_from(None, None))
        self.assertEqual(control.claim_from(AGENT, None), control.Claim(AGENT, None))
        self.assertEqual(control.claim_from(AGENT, "3"), control.Claim(AGENT, 3))
        self.set_env(AUTONOM_CONTROL_HOLDER="agent:env", AUTONOM_CONTROL_GENERATION="5")
        self.assertEqual(control.claim_from(None, None), control.Claim("agent:env", 5))
        self.assertEqual(control.claim_from(AGENT, None), control.Claim(AGENT, 5))
        self.assertEqual(control.claim_from(AGENT, 2), control.Claim(AGENT, 2))
        self.assertEqual(control.claim_from(None, 7), control.Claim("agent:env", 7))
        self.set_env(AUTONOM_CONTROL_HOLDER="", AUTONOM_CONTROL_GENERATION="")
        self.assertIsNone(control.claim_from(None, None))

    def test_claim_from_validation(self) -> None:
        for holder, generation in [("bad holder", None), ("x" * 129, None), (AGENT, 0),
                                   (AGENT, "-1"), (AGENT, "1.5"), (AGENT, True), (AGENT, "")]:
            with self.subTest(holder=holder[:20], generation=generation):
                with self.assertRaises(errors.AutonomError) as caught:
                    control.claim_from(holder, generation)
                self.assertEqual(caught.exception.code, "invalid_value")
        with self.assertRaises(errors.AutonomError) as caught:
            control.claim_from(None, 3)
        self.assertEqual(caught.exception.code, "usage_error")
        self.set_env(AUTONOM_CONTROL_HOLDER="bad holder")
        with self.assertRaises(errors.AutonomError) as caught:
            control.claim_from(None, None)
        self.assertEqual(caught.exception.code, "invalid_value")
        self.assertEqual(caught.exception.extra["option"], "AUTONOM_CONTROL_HOLDER")

    def test_now_ms_seam_only_in_test_mode(self) -> None:
        self.set_env(AUTONOM_CONTROL_NOW_MS="1234")
        self.assertGreater(control._now_ms(), 1_700_000_000_000)
        self.set_env(AUTONOM_TEST_MODE="0")
        self.assertGreater(control._now_ms(), 1_700_000_000_000)
        self.set_env(AUTONOM_TEST_MODE="1")
        self.assertEqual(control._now_ms(), 1234)
        self.set_env(AUTONOM_CONTROL_NOW_MS="not a number")
        self.assertGreater(control._now_ms(), 1_700_000_000_000)

    def test_injected_clock_drives_expiry(self) -> None:
        self.set_env(AUTONOM_TEST_MODE="1", AUTONOM_CONTROL_NOW_MS=str(NOW))
        control.acquire("android", SERIAL, holder_id=AGENT, ttl_s=5)
        self.refused("control_held", control.admit, "android", SERIAL, None)
        self.set_env(AUTONOM_CONTROL_NOW_MS=str(NOW + 5000))
        self.assertFalse(control.admit("android", SERIAL, None)["leased"])
        self.refused("control_lease_expired", control.admit, "android", SERIAL, control.Claim(AGENT, 1))

    def test_new_operation_id_format(self) -> None:
        ids = {control.new_operation_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for value in ids:
            self.assertRegex(value, r"^op_[0-9a-f]{12}$")


class InvocationTests(_Home):
    def test_mutating_admits_once_per_target(self) -> None:
        calls = []
        real = control.admit

        def counting(*args, **kwargs):
            calls.append(args[:2])
            return real(*args, **kwargs)

        with mock.patch.object(control, "admit", side_effect=counting):
            control.begin_invocation(claim=None, operation_id="op-cli-1", mode="mutating")
            first = control.admit_once("android", SERIAL)
            second = control.admit_once("android", SERIAL)
            other = control.admit_once("android", "emulator-5586")
        self.assertEqual(calls, [("android", SERIAL), ("android", "emulator-5586")])
        self.assertIs(first, second)
        self.assertEqual(first["operation_id"], "op-cli-1")
        self.assertEqual(other["operation_id"], "op-cli-1")
        state = control.invocation()
        self.assertEqual(state["mode"], "mutating")
        self.assertEqual(set(state["admitted"]), {("android", SERIAL), ("android", "emulator-5586")})
        control.end_invocation()
        self.assertEqual(control.invocation(),
                         {"claim": None, "operation_id": None, "mode": None, "admitted": {}})

    def test_flow_mode_admits_and_refuses(self) -> None:
        control.acquire("android", SERIAL, holder_id=HUMAN, kind="human")
        control.begin_invocation(claim=control.Claim(AGENT), operation_id=None, mode="flow")
        self.assertRegex(control.invocation()["operation_id"], r"^op_[0-9a-f]{12}$")
        with self.assertRaises(errors.AutonomError) as caught:
            control.admit_once("android", SERIAL)
        self.assertEqual(caught.exception.code, "control_held")
        self.assertEqual(control.invocation()["admitted"], {})

    def test_non_admitting_modes_never_read(self) -> None:
        control.acquire("android", SERIAL, holder_id=HUMAN, kind="human")
        with mock.patch.object(control, "admit", side_effect=AssertionError("admitted")):
            for mode in ("readonly", "lifecycle", "host"):
                with self.subTest(mode=mode):
                    control.begin_invocation(claim=None, operation_id=None, mode=mode)
                    self.assertIsNone(control.admit_once("android", SERIAL))
                    control.end_invocation()
            self.assertIsNone(control.admit_once("android", SERIAL))  # outside any invocation

    def test_begin_resets_and_validates(self) -> None:
        control.begin_invocation(claim=None, operation_id=None, mode="mutating")
        control.admit_once("android", SERIAL)
        control.begin_invocation(claim=None, operation_id=None, mode="mutating")
        self.assertEqual(control.invocation()["admitted"], {})
        with self.assertRaises(errors.AutonomError) as caught:
            control.begin_invocation(claim=None, operation_id=None, mode="sometimes")
        self.assertEqual(caught.exception.code, "invalid_value")
        with self.assertRaises(errors.AutonomError) as caught:
            control.begin_invocation(claim=None, operation_id="bad id", mode="readonly")
        self.assertEqual(caught.exception.code, "invalid_value")


class HardeningTests(_Home):
    """Review round 1: identifier anchoring, fail-closed reads, platform
    aliasing and oversized generations."""

    def test_ids_with_a_trailing_newline_are_refused(self) -> None:
        for holder in ("agent:x\n", "agent:x\r\n", "\nagent:x"):
            with self.subTest(holder=repr(holder)):
                self.refused("invalid_value", control.claim_from, holder, None)
                self.refused("invalid_value", control.acquire, "android", SERIAL, holder_id=holder)
                self.refused("invalid_value", control.release, "android", SERIAL, holder_id=holder)
        self.set_env(AUTONOM_CONTROL_HOLDER="agent:x\n")
        error = self.refused("invalid_value", control.claim_from, None, None)
        self.assertEqual(error.extra["option"], "AUTONOM_CONTROL_HOLDER")
        self.refused("invalid_value", control.acquire, "android", SERIAL, holder_id=AGENT,
                     session_id="s_1\n")
        self.refused("invalid_value", control.admit, "android", SERIAL, None, operation_id="op_1\n")
        self.refused("invalid_value", control.begin_invocation, claim=None, operation_id="op\n",
                     mode="mutating")
        self.assertFalse((self.home / "control").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_a_lease_file_that_cannot_be_read_fails_closed(self) -> None:
        control.acquire("android", SERIAL, holder_id=HUMAN, kind="human", now_ms=NOW)
        for _ in range(3):
            control.release("android", SERIAL, holder_id=HUMAN, now_ms=NOW)
            control.acquire("android", SERIAL, holder_id=HUMAN, kind="human", now_ms=NOW)
        path = self.lease_file()
        before = path.read_bytes()
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        error = self.refused("output_not_writable", control.acquire, "android", SERIAL,
                             holder_id="agent:thief", now_ms=NOW + 1)
        self.assertEqual(error.extra["path"], str(path))
        self.assertEqual(error.extra["capability"], "control.lease")
        self.assertTrue(error.hint)
        self.refused("output_not_writable", control.acquire, "android", SERIAL,
                     holder_id="canvas:1:2", kind="human", takeover=True, now_ms=NOW + 1)
        self.refused("output_not_writable", control.release, "android", SERIAL, holder_id=HUMAN)
        self.refused("output_not_writable", control.renew, "android", SERIAL, holder_id=HUMAN,
                     generation=4)
        self.refused("output_not_writable", control.admit, "android", SERIAL, None, now_ms=NOW + 1)
        self.refused("output_not_writable", control.read_lease, "android", SERIAL)
        self.assertEqual(control.status("android", SERIAL, now_ms=NOW)[0]["state"], "unreadable")
        self.assertEqual(control.status(now_ms=NOW)[0]["state"], "unreadable")
        path.chmod(0o600)
        self.assertEqual(path.read_bytes(), before)
        lease = control.read_lease("android", SERIAL)
        self.assertEqual((lease["generation"], lease["holder"]["id"]), (4, HUMAN))

    def test_a_directory_in_place_of_the_lease_file_fails_closed(self) -> None:
        control.control_dir()
        self.lease_file().mkdir()
        self.refused("output_not_writable", control.acquire, "android", SERIAL, holder_id=AGENT,
                     now_ms=NOW)
        self.refused("output_not_writable", control.admit, "android", SERIAL, None, now_ms=NOW)
        self.assertEqual(control.status("android", SERIAL, now_ms=NOW)[0]["state"], "unreadable")
        self.assertTrue(self.lease_file().is_dir())

    def test_only_android_and_ios_are_platforms(self) -> None:
        for platform in ("android_", "Android", "ios ", "", None, "web"):
            with self.subTest(platform=platform):
                error = self.refused("invalid_value", control.acquire, platform, "x", holder_id=AGENT)
                self.assertEqual(error.extra["option"], "platform")
                self.refused("invalid_value", control.lease_path, platform, "x")
                self.refused("invalid_value", control.admit, platform, "x", None)
        self.assertFalse((self.home / "control").exists())
        self.assertEqual(control.acquire("ios", "x", holder_id=AGENT, now_ms=NOW)["generation"], 1)
        self.assertIsNone(control.read_lease("android", "x"))

    def test_a_lease_of_another_target_sharing_the_file_name_is_not_used(self) -> None:
        owner = "10.0.0.1:5555"
        alias = "10.0.0.1_5555-" + hashlib.sha256(owner.encode()).hexdigest()[:12]
        self.assertEqual(control.lease_path("android", owner), control.lease_path("android", alias))
        control.acquire("android", owner, holder_id=AGENT, now_ms=NOW)
        path = control.lease_path("android", owner)
        before = path.read_bytes()
        self.assertIsNone(control.read_lease("android", alias))
        result = control.admit("android", alias, None, now_ms=NOW + 1)
        self.assertEqual((result["leased"], result["generation"]), (False, None))
        self.refused("control_not_held", control.admit, "android", alias, control.Claim(AGENT),
                     now_ms=NOW + 1)
        for call, kwargs in ((control.acquire, dict(holder_id=OTHER)),
                             (control.acquire, dict(holder_id=HUMAN, kind="human", takeover=True)),
                             (control.release, dict(holder_id=AGENT)),
                             (control.renew, dict(holder_id=AGENT, generation=1))):
            with self.subTest(call=call.__name__, **{k: str(v) for k, v in kwargs.items()}):
                error = self.refused("invalid_value", call, "android", alias, now_ms=NOW + 1, **kwargs)
                self.assertEqual(error.extra["option"], "target_id")
        self.assertEqual(control.status("android", alias, now_ms=NOW + 1), [])
        self.assertEqual(path.read_bytes(), before)
        self.refused("control_held", control.admit, "android", owner, None, now_ms=NOW + 1)

    def test_oversized_generations_are_invalid_values(self) -> None:
        huge = "9" * 5000
        for value in (huge, str(control.MAX_GENERATION + 1), control.MAX_GENERATION + 1, "\u00b2"):
            with self.subTest(value=str(value)[:20]):
                self.refused("invalid_value", control.claim_from, AGENT, value)
                self.refused("invalid_value", control.release, "android", SERIAL, holder_id=AGENT,
                             generation=value)
                self.refused("invalid_value", control.renew, "android", SERIAL, holder_id=AGENT,
                             generation=value)
        self.set_env(AUTONOM_CONTROL_HOLDER=AGENT, AUTONOM_CONTROL_GENERATION=huge)
        error = self.refused("invalid_value", control.claim_from, None, None)
        self.assertEqual(error.extra["option"], "AUTONOM_CONTROL_GENERATION")
        self.assertEqual(control.claim_from(None, str(control.MAX_GENERATION)),
                         control.Claim(AGENT, control.MAX_GENERATION))

    def test_damaged_file_with_an_oversized_generation_is_still_granted(self) -> None:
        control.control_dir()
        path = self.lease_file()
        path.write_bytes(b'{"generation": ' + b"9" * 5000 + b', "x": {"generation": 6}')
        self.assertIsNone(control.read_lease("android", SERIAL))
        self.assertEqual(control.status("android", SERIAL, now_ms=NOW)[0]["state"], "unreadable")
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 7)
        # Valid JSON whose generation is beyond the exact JavaScript range: not schema 1.
        lease = json.loads(path.read_text())
        lease["generation"] = control.MAX_GENERATION + 1
        path.write_text(json.dumps(lease))
        self.assertIsNone(control.read_lease("android", SERIAL))
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 1)

    def test_now_ms_seam_ignores_oversized_and_non_ascii_digits(self) -> None:
        self.set_env(AUTONOM_TEST_MODE="1")
        for raw in ("9" * 5000, "\u00b2", "\u0661\u0662"):
            with self.subTest(raw=raw[:10]):
                self.set_env(AUTONOM_CONTROL_NOW_MS=raw)
                self.assertGreater(control._now_ms(), 1_700_000_000_000)


class ReviewRoundTwoTests(_Home):
    """Review round 2: deeply nested damaged content, a damaged file naming
    MAX_GENERATION, and release/renew with an unsearchable control directory."""

    def test_deeply_nested_damaged_file_is_no_lease(self) -> None:
        control.control_dir()
        path = self.lease_file()
        path.write_bytes(b"[" * 200_000)
        self.assertIsNone(control.read_lease("android", SERIAL))
        result = control.admit("android", SERIAL, None, now_ms=NOW)
        self.assertEqual((result["leased"], result["generation"]), (False, None))
        self.refused("control_not_held", control.admit, "android", SERIAL, control.Claim(AGENT),
                     now_ms=NOW)
        self.assertEqual(control.status("android", SERIAL, now_ms=NOW)[0]["state"], "unreadable")
        self.assertEqual(control.status(now_ms=NOW)[0]["state"], "unreadable")
        self.refused("control_not_held", control.release, "android", SERIAL, holder_id=AGENT,
                     now_ms=NOW)
        self.refused("control_not_held", control.renew, "android", SERIAL, holder_id=AGENT,
                     generation=1, now_ms=NOW)
        # Nesting around a generation still names it: the next grant continues after it.
        path.write_bytes(b'{"generation": 5, "x": ' + b"[" * 200_000)
        self.assertIsNone(control.read_lease("android", SERIAL))
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 6)
        # Nothing recoverable: the next grant overwrites it at generation 1.
        path.write_bytes(b"{" * 200_000)
        self.assertEqual(control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)["generation"], 1)
        self.assertEqual(control.read_lease("android", SERIAL)["holder"]["id"], AGENT)

    def test_damaged_file_naming_max_generation_is_exhausted(self) -> None:
        control.control_dir()
        path = self.lease_file()
        damaged = b'{"schema": 1, "generation": ' + str(control.MAX_GENERATION).encode() + b', "x'
        path.write_bytes(damaged)
        self.assertIsNone(control.read_lease("android", SERIAL))
        error = self.refused("invalid_value", control.acquire, "android", SERIAL, holder_id=AGENT,
                             now_ms=NOW)
        self.assertIn("exhausted", error.message)
        self.assertEqual(error.extra["option"], "generation")
        self.refused("invalid_value", control.acquire, "android", SERIAL, holder_id=HUMAN,
                     kind="human", takeover=True, now_ms=NOW)
        self.assertEqual(path.read_bytes(), damaged)
        # One below the limit is still granted, at MAX_GENERATION.
        path.write_bytes(b'{"generation": ' + str(control.MAX_GENERATION - 1).encode() + b', "x')
        granted = control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)
        self.assertEqual(granted["generation"], control.MAX_GENERATION)
        # The valid file at MAX_GENERATION refuses the same way.
        control.release("android", SERIAL, holder_id=AGENT, now_ms=NOW)
        error = self.refused("invalid_value", control.acquire, "android", SERIAL, holder_id=AGENT,
                             now_ms=NOW)
        self.assertIn("exhausted", error.message)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_release_and_renew_decide_under_the_lock_with_an_unsearchable_directory(self) -> None:
        control.acquire("android", SERIAL, holder_id=AGENT, now_ms=NOW)
        directory = control.control_dir()
        path = self.lease_file()
        directory.chmod(0)
        self.addCleanup(directory.chmod, 0o700)
        # A directory that cannot be repaired (another owner): fail closed, nothing changes.
        with mock.patch.object(control.os, "chmod", side_effect=PermissionError(1, "Operation not permitted")):
            for call, kwargs in ((control.release, dict(holder_id=AGENT)),
                                 (control.release, dict(holder_id=OTHER)),
                                 (control.renew, dict(holder_id=AGENT, generation=1)),
                                 (control.renew, dict(holder_id=OTHER, generation=1))):
                with self.subTest(call=call.__name__, holder=kwargs["holder_id"]):
                    error = self.refused("output_not_writable", call, "android", SERIAL,
                                         now_ms=NOW + 1, **kwargs)
                    self.assertEqual(error.extra["capability"], "control.lease")
                    self.assertTrue(error.hint)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0)
        # The owner's own store: the writer restores mode 0700 under the lock and
        # decides from the real lease, never answering "not held" for a held lease.
        renewed = control.renew("android", SERIAL, holder_id=AGENT, generation=1, now_ms=NOW + 2)
        self.assertEqual((renewed["generation"], renewed["renewed"]), (1, True))
        directory.chmod(0)
        self.refused("control_held", control.renew, "android", SERIAL, holder_id=OTHER,
                     generation=1, now_ms=NOW + 3)
        directory.chmod(0)
        released = control.release("android", SERIAL, holder_id=AGENT, now_ms=NOW + 4)
        self.assertTrue(released["released"])
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        lease = control.read_lease("android", SERIAL)
        self.assertEqual((lease["generation"], lease["holder"]), (1, None))
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
