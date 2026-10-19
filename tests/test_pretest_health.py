"""Ticket pre-test health: deltas, automatic findings, repro steps, verdict.

Module-level tests against a fake target and a temporary ``AUTONOM_HOME``.
Device reads (crash list, logcat, metrics snapshot) are patched; captured
traffic and the journal are real files in the temporary session.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import device_state, errors, logs  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib.metrics import snapshot as snapshot_mod  # noqa: E402
from autonom_lib.pretest import checks as checks_mod  # noqa: E402
from autonom_lib.pretest import findings as findings_mod  # noqa: E402
from autonom_lib.pretest import health as health_mod  # noqa: E402
from autonom_lib.pretest import repro as repro_mod  # noqa: E402
from autonom_lib.pretest import store  # noqa: E402
from autonom_lib.pretest import verdict as verdict_mod  # noqa: E402

APP = "com.example.app"
OWN_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-8888-7777-6666-555555555555"


def iso(stamp: datetime) -> str:
    stamp = stamp.astimezone(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def logcat(stamp: datetime, level: str, tag: str, message: str, pid: int = 4242) -> str:
    local = stamp.astimezone()
    return (f"{local.strftime('%m-%d %H:%M:%S')}.{local.microsecond // 1000:03d}  "
            f"{pid}  {pid} {level} {tag}: {message}")


def snapshot(pss_kb: float = 100_000, cpu: float = 5.0) -> dict:
    return {"ok": True, "platform": "android", "pid": 42,
            "memory": {"total_pss_kb": pss_kb}, "cpu": {"process_percent": cpu},
            "metric_semantics": "android_dumpsys_meminfo_v1",
            "captured_at": "2026-10-10T10:00:00Z"}


class HealthBase(unittest.TestCase):
    """A temporary AUTONOM_HOME, a session, a fake Android target and an
    active pre-test whose clock is pinned for check windows."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.artifacts = self.root / "home/sessions/s_test"
        for name in ("shots", "trees", "logs", "network", "crashes", "flows"):
            (self.artifacts / name).mkdir(parents=True)
        self.session = {"session_id": "s_test", "artifacts_dir": str(self.artifacts),
                        "platform": "android", "target_id": "emulator-5554",
                        "app_id": APP, "network": {"enabled": True}}
        self.target = SimpleNamespace(platform="android", target_id="emulator-5554",
                                      tool="fake-adb", aliases={"serial": "emulator-5554"})
        self.crashes: list = []
        self.crash_error: Exception | None = None
        self.log_lines: list[str] = []
        self.log_error: Exception | None = None
        self.tail_calls: list[dict] = []
        self.metric = snapshot()
        self.metric_error: Exception | None = None
        for target, name, fake in (
                (device_state, "crash_list", self._crash_list),
                (logs, "tail_logcat_detailed", self._tail),
                (snapshot_mod, "take", self._take)):
            patcher = mock.patch.object(target, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Check windows are placed an hour back, so every timestamp is in the past.
        self.t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        self.record = store.create(
            self.session, target=self.target, ticket_text="# WB-7 Cart\nRemove an item.",
            ticket_ref="WB-7", title=None, repo=None, base="main", head=None,
            flows_dir=None, app_id=APP, budget=None, context={"changed_files": []})
        self.set_baseline(cursor_ts=iso(self.t0))

    # --- fakes ------------------------------------------------------------------

    def _crash_list(self, target, app_id=None):
        if self.crash_error is not None:
            raise self.crash_error
        return list(self.crashes)

    def _tail(self, adb, serial, **kwargs):
        # Behaves like the real reader: the grep first, then the line cap.
        if self.log_error is not None:
            raise self.log_error
        self.tail_calls.append(kwargs)
        lines = list(self.log_lines)
        pattern = logs.compile_grep(kwargs.get("grep"))
        if pattern:
            lines = [line for line in lines if pattern.search(line)]
        max_lines = kwargs.get("max_lines", 200)
        if max_lines > 0:
            lines = lines[-max_lines:]
        return {"entries": [{"line": line} for line in lines], "warnings": [],
                "filter": "uid", "uid": 10001, "pid": None}

    def _take(self, target, app_id):
        if self.metric_error is not None:
            raise self.metric_error
        return dict(self.metric), "raw"

    # --- helpers ----------------------------------------------------------------

    def at(self, minutes: float) -> datetime:
        return self.t0 + timedelta(minutes=minutes)

    @contextmanager
    def clock(self, minutes: float):
        with mock.patch.object(store, "now", return_value=iso(self.at(minutes))):
            yield

    def pretest_dir(self) -> Path:
        return store.pretest_dir(self.session, self.record["pretest_id"])

    def set_baseline(self, **changes) -> dict:
        path = self.pretest_dir() / store.BASELINE_FILE
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["taken_at"] = iso(self.t0)
        doc["logs"] = {"state": "available", "cursor_ts": changes.pop("cursor_ts", iso(self.t0))}
        doc.update(changes)
        store.write_json(path, doc)
        return doc

    def check(self, title: str, begin: float | None = None, end: float | None = None,
              verdict: str = "pass") -> str:
        with self.clock(begin if begin is not None else 0):
            check_id = checks_mod.add(self.session, self.record, title=title,
                                      kind="ticket")["check_id"]
        if begin is not None:
            with self.clock(begin):
                checks_mod.begin(self.session, self.record, check_id)
        if end is not None:
            with self.clock(end):
                checks_mod.end(self.session, self.record, check_id, verdict=verdict)
        return check_id

    def flows(self, *rows: dict) -> None:
        path = self.artifacts / "network/flows.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def flow(self, flow_id: str, status, minutes: float, *, mocked: bool = False,
             path: str = "/api/cart") -> dict:
        started = self.at(minutes)
        return {"id": flow_id, "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "started_at_ms": int(started.timestamp() * 1000), "method": "GET",
                "url": f"https://api.example.com{path}", "host": "api.example.com",
                "path": path, "status": status, "mocked": mocked,
                "mock_id": "m_1" if mocked else None}

    def findings(self) -> list[dict]:
        return findings_mod.fold(self.session, self.record)

    def assert_code(self, code: str, fn, *args, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        payload = caught.exception.as_dict()
        self.assertEqual(payload["error_code"], code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload.get("hint"), f"{code} has no hint")
        json.dumps(payload)
        return caught.exception


# --- attribution ---------------------------------------------------------------------


class AttributeTests(unittest.TestCase):
    CHECKS = [
        {"check_id": "chk-1", "state": "ended", "began_at": "2026-10-10T10:00:00.000Z",
         "ended_at": "2026-10-10T10:10:00.000Z"},
        {"check_id": "chk-2", "state": "added", "began_at": None, "ended_at": None},
        {"check_id": "chk-3", "state": "open", "began_at": "2026-10-10T10:20:00.000Z",
         "ended_at": None},
    ]

    def test_inside_a_window_and_on_its_edges(self) -> None:
        attribute = health_mod.attribute
        self.assertEqual(attribute("2026-10-10T10:05:00Z", self.CHECKS), "chk-1")
        self.assertEqual(attribute("2026-10-10T10:00:00.000Z", self.CHECKS), "chk-1")
        self.assertEqual(attribute("2026-10-10T10:10:00.000Z", self.CHECKS), "chk-1")
        self.assertIsNone(attribute("2026-10-10T10:15:00Z", self.CHECKS))
        self.assertIsNone(attribute("2026-10-10T09:59:59Z", self.CHECKS))

    def test_open_check_window_has_no_end(self) -> None:
        self.assertEqual(health_mod.attribute("2026-10-11T00:00:00Z", self.CHECKS), "chk-3")

    def test_other_time_shapes_and_bad_input(self) -> None:
        stamp = datetime(2026, 10, 10, 10, 5, tzinfo=timezone.utc)
        self.assertEqual(health_mod.attribute(stamp, self.CHECKS), "chk-1")
        self.assertEqual(health_mod.attribute(stamp.timestamp(), self.CHECKS), "chk-1")
        for bad in (None, "", "yesterday", True, object()):
            self.assertIsNone(health_mod.attribute(bad, self.CHECKS))
        self.assertIsNone(health_mod.attribute(stamp, []))
        self.assertIsNone(health_mod.attribute(stamp, [None, "chk-1"]))


# --- crashes ----------------------------------------------------------------------


class CrashTests(HealthBase):
    def test_new_crash_is_attributed_to_its_check(self) -> None:
        self.check("Remove item", begin=10, end=20)
        local = self.at(15).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.crashes = [{"name": "App-2026-10-10-120000.ips", "date": local, "raw": "x"}]
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual(crashes["state"], "available")
        self.assertEqual(len(crashes["new"]), 1)
        self.assertEqual(crashes["new"][0]["name"], "App-2026-10-10-120000.ips")
        self.assertEqual(crashes["new"][0]["check_id"], "chk-1")

    def test_known_crashes_are_not_new(self) -> None:
        old = {"name": "Old.ips", "date": "2026-01-01 10:00:00", "raw": "x"}
        self.set_baseline(crashes={"state": "available", "names": ["Old.ips"],
                                   "latest": {"name": "Old.ips", "time": old["date"]}})
        self.crashes = [old]
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual(crashes, {"state": "available", "new": []})

    def test_android_crash_lines_of_one_process_are_one_crash(self) -> None:
        self.check("Remove item", begin=10, end=20)
        lines = [logcat(self.at(12), "E", "AndroidRuntime", "FATAL EXCEPTION: main", 900),
                 logcat(self.at(12), "E", "AndroidRuntime", "Process: com.example.app", 900),
                 logcat(self.at(12), "E", "AndroidRuntime", "at Foo.bar(Foo.kt:12)", 900),
                 logcat(self.at(30), "E", "AndroidRuntime", "FATAL EXCEPTION: io", 901)]
        self.crashes = [{"name": None, "date": line.split()[0], "raw": line} for line in lines]
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual(len(crashes["new"]), 2)
        self.assertTrue(crashes["new"][0]["name"].startswith("logcat:"))
        self.assertEqual(crashes["new"][0]["check_id"], "chk-1")
        self.assertIsNone(crashes["new"][1]["check_id"])

    def test_unavailable_baseline_stays_unavailable(self) -> None:
        self.set_baseline(crashes={"state": "unavailable", "reason": "idb crash list failed"})
        self.crashes = [{"name": "New.ips", "date": None, "raw": "x"}]
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual(crashes["state"], "unavailable")
        self.assertIn("baseline", crashes["reason"])
        self.assertIn("idb crash list failed", crashes["reason"])
        self.assertEqual(crashes["new"], [])

    def test_failed_read_now_is_unavailable_with_reason(self) -> None:
        self.crash_error = errors.AutonomError(errors.BACKEND_FAILED, "adb went away")
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual(crashes["state"], "unavailable")
        self.assertIn("adb went away", crashes["reason"])
        no_target = health_mod.compute(self.session, self.record, None)
        for block in ("crashes", "logs", "metrics"):
            self.assertEqual(no_target[block]["state"], "unavailable", block)


    def capped_baseline(self, entries: list[dict], latest_time: str) -> None:
        names = [health_mod.baseline_mod.crash_name(e) for e in entries]
        self.set_baseline(crashes={"state": "available",
                                   "names": names[-health_mod.baseline_mod.MAX_CRASH_NAMES:],
                                   "latest": {"name": names[-1], "time": latest_time}})

    def test_capped_android_baseline_keeps_a_same_day_new_crash(self) -> None:
        self.check("Remove item", begin=10, end=20)
        old = [logcat(self.at(-30), "E", "AndroidRuntime", f"at Foo.m{i}(Foo.kt)", 800 + i)
               for i in range(600)]
        new = [logcat(self.at(12), "E", "AndroidRuntime", f"crash line {i}", 9999)
               for i in range(5)]
        entries = [{"name": None, "date": line.split()[0], "raw": line} for line in old]
        # The newest baseline crash is on the same day as the new one.
        self.capped_baseline(entries, new[0].split()[0])
        self.crashes = entries + [{"name": None, "date": line.split()[0], "raw": line}
                                  for line in new]
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual(crashes["state"], "available")
        self.assertEqual(len(crashes["new"]), 1, crashes)
        self.assertEqual(crashes["new"][0]["check_id"], "chk-1")
        self.assertEqual(crashes["new"][0]["name"],
                         health_mod.baseline_mod.crash_name(self.crashes[600]))

    def test_capped_android_baseline_after_the_buffer_rotated(self) -> None:
        gone = [{"name": None, "date": "01-01",
                 "raw": logcat(self.at(-30), "E", "AndroidRuntime", f"gone {i}", 700)}
                for i in range(500)]
        day = logcat(self.at(12), "E", "AndroidRuntime", "x").split()[0]
        self.capped_baseline(gone, day)
        older = [logcat(self.at(-60 * 48), "E", "AndroidRuntime", f"older {i}", 701)
                 for i in range(3)]
        newer = [logcat(self.at(12), "E", "AndroidRuntime", "new crash", 702)]
        self.crashes = [{"name": None, "date": line.split()[0], "raw": line}
                        for line in older + newer]
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual(len(crashes["new"]), 1, crashes)
        self.assertEqual(crashes["new"][0]["name"],
                         health_mod.baseline_mod.crash_name(self.crashes[-1]))

    def test_capped_ios_baseline_compares_times(self) -> None:
        def local(minutes: float) -> str:
            return self.at(minutes).astimezone().strftime("%Y-%m-%d %H:%M:%S")

        known = [{"name": f"Old-{i}.ips", "date": local(-60), "raw": "x"} for i in range(500)]
        self.capped_baseline(known, local(0))
        self.crashes = known + [
            {"name": "Older-unlisted.ips", "date": local(-10), "raw": "x"},
            {"name": "Same-second.ips", "date": local(0), "raw": "x"},
            {"name": "Later.ips", "date": local(15), "raw": "x"},
            {"name": "No-date.ips", "date": None, "raw": "x"}]
        crashes = health_mod.compute(self.session, self.record, self.target)["crashes"]
        self.assertEqual([c["name"] for c in crashes["new"]],
                         ["Same-second.ips", "Later.ips", "No-date.ips"])

    def test_watermark_shapes(self) -> None:
        self.assertEqual(health_mod._watermark("2026-03-04"),
                         ("day", health_mod.date(2026, 3, 4)))
        self.assertEqual(health_mod._watermark("02-30"), None)
        self.assertEqual(health_mod._watermark(None), None)
        self.assertEqual(health_mod._watermark("garbage"), None)
        kind, stamp = health_mod._watermark("2026-10-10T10:00:00Z")
        self.assertEqual((kind, stamp.isoformat()), ("exact", "2026-10-10T10:00:00+00:00"))


# --- requests ---------------------------------------------------------------------


class RequestTests(HealthBase):
    def test_classification_and_mocked_exclusion(self) -> None:
        self.check("Remove item", begin=10, end=20)
        self.flows(self.flow("f1", 500, 15),
                   self.flow("f2", 500, 15, mocked=True),
                   self.flow("f3", 404, 25),
                   self.flow("f4", 404, 25, mocked=True),
                   self.flow("f5", 200, 15),
                   self.flow("f6", None, 16),
                   self.flow("f7", 302, 15))
        failed = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual(failed["state"], "available")
        rows = {row["id"]: row for row in failed["failed"]}
        self.assertEqual(sorted(rows), ["f1", "f3", "f6"])
        self.assertEqual(rows["f1"]["reason"], "5xx")
        self.assertEqual(rows["f1"]["check_id"], "chk-1")
        self.assertEqual(rows["f3"]["reason"], "4xx-unmocked")
        self.assertIsNone(rows["f3"]["check_id"])
        self.assertEqual(rows["f6"]["reason"], "incomplete")
        self.assertIsNone(rows["f6"]["status"])
        for key in ("id", "method", "host", "path", "status", "reason", "check_id"):
            self.assertIn(key, rows["f1"])

    def test_flows_before_the_baseline_line_count_are_old(self) -> None:
        self.flows(self.flow("old", 500, -5))
        self.set_baseline(network={"state": "available", "flows_line_count": 1})
        self.flows(self.flow("new", 503, 5))
        failed = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual([row["id"] for row in failed["failed"]], ["new"])

    def test_cleared_store_falls_back_to_the_clock(self) -> None:
        self.set_baseline(network={"state": "available", "flows_line_count": 50})
        self.flows(self.flow("before", 500, -5), self.flow("after", 500, 5))
        failed = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual([row["id"] for row in failed["failed"]], ["after"])

    def test_no_capture_at_all_is_unavailable(self) -> None:
        self.set_baseline(network={"state": "unavailable", "reason": "capture not running"})
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual(block["state"], "unavailable")
        self.assertIn("capture not running", block["reason"])

    def test_capture_running_without_traffic_is_available_and_empty(self) -> None:
        self.set_baseline(network={"state": "available", "flows_line_count": 0})
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual(block, {"state": "available", "failed": []})

    def test_torn_and_non_object_lines_are_skipped(self) -> None:
        path = self.artifacts / "network/flows.jsonl"
        path.write_text('[1,2]\n{"id": "x", "status": 5\n', encoding="utf-8")
        self.flows(self.flow("ok500", 500, 5))
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual([row["id"] for row in block["failed"]], ["ok500"])


    def test_deep_and_non_object_lines_never_crash(self) -> None:
        # Past the parser's limit json.loads raises RecursionError; below it a
        # value can still be too deep for str(). Both lines are skipped, as
        # are lines that are JSON but not an object.
        too_deep = "[" * 1_000_000 + "]" * 1_000_000
        deep_value = "[" * 100_000 + "]" * 100_000
        stamp = int(self.at(5).timestamp() * 1000)
        path = self.artifacts / "network/flows.jsonl"
        path.write_text("\n".join([
            "3", "null", '"x"', "[1]", too_deep,
            '{"id": %s, "status": 500, "started_at_ms": %d}' % (deep_value, stamp),
            '{"id": "d2", "method": %s, "status": 500, "started_at_ms": %d}'
            % (deep_value, stamp),
        ]) + "\n", encoding="utf-8")
        self.flows(self.flow("ok500", 500, 5))
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual(block["state"], "available")
        self.assertEqual([row["id"] for row in block["failed"]], ["ok500"])
        written = health_mod.run(self.session, self.record, self.target)["auto_findings"]
        self.assertEqual([item["data"]["id"] for item in written
                          if item["source"] == "request"], ["ok500"])

    def incomplete(self, flow_id: str, kind: str, minutes: float, *,
                   mock_id: str | None = None) -> dict:
        row = self.flow(flow_id, None, minutes)
        row.update(completed=False, error_kind=kind,
                   mocked=bool(mock_id), mock_id=mock_id)
        return row

    def test_a_request_the_app_cancelled_is_not_a_failure(self) -> None:
        self.check("Double tap", begin=10, end=20)
        # A hang_cap row is always mocked: the addon marks the mock before
        # the hold, so it is never a failure either.
        self.flows(self.incomplete("f_abort", "client_abort", 12),
                   self.incomplete("f_cap", "hang_cap", 13, mock_id="m_hang"),
                   self.incomplete("f_up", "upstream_error", 14),
                   self.incomplete("f_other", "other", 15))
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        rows = {row["id"]: row["reason"] for row in block["failed"]}
        self.assertEqual(rows, {"f_up": "incomplete", "f_other": "incomplete"})
        written = health_mod.run(self.session, self.record, self.target)["auto_findings"]
        titles = sorted(item["title"] for item in written if item["source"] == "request")
        self.assertEqual(len(titles), 2)
        self.assertTrue(all(title.startswith("Request never completed") for title in titles))
        self.assertFalse(any("f_abort" in str(item["data"]) or "f_cap" in str(item["data"])
                             for item in written))

    def test_an_unmocked_incomplete_request_reads_never_completed(self) -> None:
        """An upstream failure or timeout with no mock is a failed request."""
        self.check("Load cart", begin=10, end=20)
        self.flows(self.incomplete("f_timeout", "upstream_error", 12))
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual([(row["id"], row["reason"]) for row in block["failed"]],
                         [("f_timeout", "incomplete")])
        written = health_mod.run(self.session, self.record, self.target)["auto_findings"]
        requests = [item for item in written if item["source"] == "request"]
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["title"].startswith("Request never completed"))
        self.assertIn("f_timeout", str(requests[0]["data"]))

    def test_repeated_flow_ids_after_a_proxy_restart(self) -> None:
        self.check("Remove item", begin=10, end=20)
        self.flows(self.flow("f_0003", 200, -5))
        self.set_baseline(network={"state": "available", "flows_line_count": 1})
        # The proxy restarted: its counter began again at f_0001.
        self.flows(self.flow("f_0003", 503, 5), self.flow("f_0003", 500, 15),
                   self.flow("f_0009", 500, 16))
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual(block["basis"], "line_count")
        self.assertEqual([(r["id"], r["line"], r.get("duplicate_id")) for r in block["failed"]],
                         [("f_0003", 2, True), ("f_0003", 3, True), ("f_0009", 4, None)])
        written = health_mod.run(self.session, self.record, self.target)["auto_findings"]
        requests = [item for item in written if item["source"] == "request"]
        self.assertEqual(len(requests), 3)
        self.assertEqual(len({item["data"]["key"] for item in requests}), 3)
        self.assertEqual([item["evidence"] for item in requests],
                         [[], [], ["request:f_0009"]])
        self.assertEqual(requests[1]["data"]["line"], 3)
        self.assertEqual(requests[1]["check_id"], "chk-1")
        self.assertEqual(health_mod.run(self.session, self.record, self.target)
                         ["auto_findings"], [])

    def test_capture_started_after_the_baseline_is_read_by_the_clock(self) -> None:
        self.set_baseline(network={"state": "unavailable", "reason": "capture not running"})
        self.flows(self.flow("before", 500, -5), self.flow("after", 500, 5))
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual((block["state"], block["basis"]), ("available", "clock"))
        self.assertEqual([row["id"] for row in block["failed"]], ["after"])
        doc = json.loads((self.pretest_dir() / store.BASELINE_FILE).read_text())
        doc.pop("taken_at")
        store.write_json(self.pretest_dir() / store.BASELINE_FILE, doc)
        block = health_mod.compute(self.session, self.record, self.target)["requests"]
        self.assertEqual(block["state"], "unavailable")
        self.assertIn("nothing tells old traffic from new", block["reason"])


# --- logs -------------------------------------------------------------------------


class LogTests(HealthBase):
    def test_error_lines_are_grouped_and_normalized(self) -> None:
        self.check("Remove item", begin=10, end=20)
        self.log_lines = [
            logcat(self.at(12), "E", "Cart", 'Failed to load item 123 id=0xdeadbeef "abc"'),
            logcat(self.at(13), "E", "Cart", 'Failed to load item 456 id=0x1f "xyz"'),
            logcat(self.at(14), "W", "Cart", "just a warning 1"),
            logcat(self.at(14), "I", "Cart", "info 2"),
            logcat(self.at(30), "F", "libc", "Fatal signal 11 (SIGSEGV)"),
            logcat(self.at(-30), "E", "Cart", "before the baseline 1"),
            "--------- beginning of main",
        ]
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual(block["state"], "available")
        groups = {g["pattern"]: g for g in block["groups"]}
        self.assertEqual(len(groups), 2, groups)
        cart = groups['Cart: Failed to load item N id=<hex> "<v>"']
        self.assertEqual(cart["count"], 2)
        self.assertEqual(cart["check_id"], "chk-1")
        self.assertEqual(cart["level"], "error")
        self.assertEqual(cart["first_ts"], iso(self.at(12)))
        fatal = groups["libc: Fatal signal N (SIGSEGV)"]
        self.assertEqual(fatal["level"], "fatal")
        self.assertIsNone(fatal["check_id"])
        self.assertEqual(block["lines"], 3)

    def test_at_most_fifty_groups_most_frequent_first(self) -> None:
        lines = [logcat(self.at(5), "E", "T", f"distinct message {chr(65 + i % 26)}"
                        f"{chr(65 + i // 26)}") for i in range(60)]
        lines += [logcat(self.at(6), "E", "T", "hot path failed 7")] * 5
        self.log_lines = lines
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual(len(block["groups"]), health_mod.MAX_LOG_GROUPS)
        self.assertEqual(block["groups"][0]["pattern"], "T: hot path failed N")
        self.assertEqual(block["groups"][0]["count"], 5)
        self.assertEqual(block["omitted_groups"], 11)
        self.assertEqual(block["omitted_lines"], 11)

    def test_secrets_in_log_lines_are_masked(self) -> None:
        self.log_lines = [logcat(self.at(5), "E", "Http",
                                 "auth failed Authorization: Bearer abcdef123456789xyz")]
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        text = json.dumps(block)
        self.assertNotIn("abcdef123456789xyz", text)
        self.assertIn("<redacted>", text)

    def test_unavailable_paths(self) -> None:
        self.log_error = errors.AutonomError(errors.BACKEND_FAILED, "logcat failed")
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual(block["state"], "unavailable")
        self.assertIn("logcat failed", block["reason"])
        self.log_error = None
        self.set_baseline(logs={"state": "unavailable", "reason": "no reader"})
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual(block["state"], "unavailable")
        self.assertEqual(block["groups"], [])
        self.set_baseline()
        self.record["app_id"] = None
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual(block["state"], "unavailable")
        self.assertIn("app id", block["reason"])
        other = SimpleNamespace(platform="web", target_id="x", tool="x")
        self.record["app_id"] = APP
        block = health_mod.compute(self.session, self.record, other)["logs"]
        self.assertEqual(block["state"], "unavailable")

    def test_ios_reads_the_session_stream_after_the_baseline(self) -> None:
        ios = SimpleNamespace(platform="ios", target_id="SIM", tool="xcrun")
        missing = health_mod.compute(self.session, self.record, ios)["logs"]
        self.assertEqual(missing["state"], "unavailable")
        self.assertIn("--log-stream", missing["reason"])
        stream = self.artifacts / "logs/stream.ndjson"

        self.session["background"] = {"log_stream_executable": "Runner",
                                      "log_stream_image_uuids": [OWN_UUID]}

        def rec(minutes: float, kind: str, message: str, uuid: str = OWN_UUID) -> str:
            local = self.at(minutes).astimezone()
            stamp = local.strftime("%Y-%m-%d %H:%M:%S.%f") + local.strftime("%z")
            return json.dumps({"timestamp": stamp, "messageType": kind,
                               "eventMessage": message, "processImageUUID": uuid,
                               "processImagePath": "/x/Runner.app/Runner"}) + "\n"

        stream.write_text(rec(-1, "Error", "old error 1"), encoding="utf-8")
        self.set_baseline(logs={"state": "available", "cursor_ts": iso(self.t0),
                                "stream": "logs/stream.ndjson", "stream_lines": 1})
        with stream.open("a", encoding="utf-8") as handle:
            handle.write(rec(5, "Error", "decode failed for 'item-9'"))
            handle.write(rec(6, "Fault", "decode failed for 'item-12'"))
            handle.write(rec(7, "Default", "fine 3"))
            handle.write(rec(7, "Error", "OtherBank: user jane.doe@corp.example login failed",
                             uuid=OTHER_UUID))
            handle.write("not json\n")
        self.check("Remove item", begin=1, end=10)
        block = health_mod.compute(self.session, self.record, ios)["logs"]
        self.assertEqual(block["state"], "available")
        self.assertEqual(len(block["groups"]), 1)
        self.assertEqual(block["groups"][0]["pattern"], "decode failed for '<v>'")
        self.assertEqual(block["groups"][0]["count"], 2)
        self.assertEqual(block["groups"][0]["check_id"], "chk-1")
        self.assertNotIn("jane.doe", json.dumps(block))
        self.assertNotIn("OtherBank", json.dumps(block))

    def test_app_error_records_and_flutter_markers_are_error_groups(self) -> None:
        """Fix round 3, item 4: a Flutter app logs at priority I; its error
        records and FlutterError markers still count."""
        self.check("Remove item", begin=10, end=20)
        self.log_lines = [
            logcat(self.at(11), "I", "flutter",
                   '{"level": "ERROR", "message": "cart failed 12", "token": "tok-SECRET-1"}'),
            logcat(self.at(11.5), "I", "flutter",
                   '{"level": "error", "message": "cart failed 13", "token": "tok-SECRET-2"}'),
            logcat(self.at(12), "I", "flutter", '{"level":"Severe","message":"db closed"}'),
            logcat(self.at(12), "W", "flutter", 'sync level=FATAL reason="disk full"'),
            logcat(self.at(13), "I", "flutter",
                   "\u2550\u2550\u2561 EXCEPTION CAUGHT BY WIDGETS LIBRARY \u255e\u2550\u2550"),
            logcat(self.at(13), "I", "flutter", "FLUTTER_ERROR RenderFlex overflowed by 12 px"),
            # not errors: an info record whose text says "error", a word glued to
            # "level", plain text, and an error record before the baseline
            logcat(self.at(14), "I", "flutter",
                   '{"level": "info", "message": "no error level=error here"}'),
            logcat(self.at(14), "I", "flutter", "loglevel=error is a setting"),
            logcat(self.at(14), "D", "flutter", "an error happened 3"),
            logcat(self.at(-30), "I", "flutter", '{"level": "error", "message": "old 1"}'),
        ]
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual(block["state"], "available")
        self.assertEqual(block["lines"], 6)
        by_level = sorted((g["level"], g["count"]) for g in block["groups"])
        self.assertEqual(by_level, [("error", 1), ("error", 1), ("error", 1), ("error", 2),
                                    ("fatal", 1)])
        self.assertTrue(all(g["check_id"] == "chk-1" for g in block["groups"]))
        dumped = json.dumps(block)
        self.assertNotIn("tok-SECRET", dumped)
        self.assertIn("flutter: FLUTTER_ERROR RenderFlex overflowed by N px",
                      [g["pattern"] for g in block["groups"]])
        # the logcat read itself asks for these lines (the grep is a pre-filter)
        grep = logs.compile_grep(health_mod.ANDROID_ERROR_GREP)
        self.assertTrue(grep.search(self.log_lines[0]))
        self.assertTrue(grep.search(self.log_lines[4]))
        self.assertFalse(grep.search(self.log_lines[8]))

    def test_app_level(self) -> None:
        cases = {
            '{"level": "error"}': "error", '{"level":"FATAL","x":1}': "fatal",
            '{"level": "severe"}': "error", 'prefix {"level": "Error", "a": [1]}': "error",
            'level=error': "error", 'x level = "fatal" y': "fatal", "LEVEL=SEVERE": "error",
            '{"level": "warning"}': None, '{"level": 3}': None, "loglevel=error": None,
            "sub.level=error": None, "levels=error": None, "": None, "plain error": None,
            'broken {"level": "error"': "error",
            "FLUTTER_ERROR x": "error", "\u2550\u2550\u2561 EXCEPTION CAUGHT BY X": "error",
            "EXCEPTION CAUGHT": None,
        }
        for message, expected in cases.items():
            self.assertEqual(health_mod.app_level(message), expected, message)
        self.assertIsNone(health_mod.app_level(None))  # type: ignore[arg-type]
        deep = '{"a": ' + "[" * 5000 + "]" * 5000 + ', "level": "error"}'
        self.assertEqual(health_mod.app_level(deep), "error")  # the regex still reads it

    def test_ios_app_error_records_count(self) -> None:
        ios = SimpleNamespace(platform="ios", target_id="SIM", tool="xcrun")
        stream = self.artifacts / "logs/stream.ndjson"
        self.session["background"] = {"log_stream_executable": "Runner",
                                      "log_stream_image_uuids": [OWN_UUID]}

        def rec(minutes: float, kind: str, message: str) -> str:
            local = self.at(minutes).astimezone()
            stamp = local.strftime("%Y-%m-%d %H:%M:%S.%f") + local.strftime("%z")
            return json.dumps({"timestamp": stamp, "messageType": kind,
                               "eventMessage": message, "processImageUUID": OWN_UUID,
                               "processImagePath": "/x/Runner.app/Runner"}) + "\n"

        stream.write_text("", encoding="utf-8")
        self.set_baseline(logs={"state": "available", "cursor_ts": iso(self.t0),
                                "stream": "logs/stream.ndjson", "stream_lines": 0})
        with stream.open("a", encoding="utf-8") as handle:
            handle.write(rec(5, "Default", 'flutter: {"level": "error", "message": "pay 1"}'))
            handle.write(rec(6, "Default", "flutter: FLUTTER_ERROR overflow 2"))
            handle.write(rec(7, "Default", 'flutter: {"level": "info", "message": "ok"}'))
        self.check("Remove item", begin=1, end=10)
        block = health_mod.compute(self.session, self.record, ios)["logs"]
        self.assertEqual(block["state"], "available")
        self.assertEqual(sorted(g["level"] for g in block["groups"]), ["error", "error"])

    def test_android_cap_counts_error_lines_only(self) -> None:
        self.check("Remove item", begin=10, end=20)
        self.log_lines = [logcat(self.at(12), "E", "Cart", "early failure 1")]
        self.log_lines += [logcat(self.at(13), "D", "Chatty", f"debug {i}")
                           for i in range(30000)]
        block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual([g["pattern"] for g in block["groups"]], ["Cart: early failure N"])
        self.assertEqual(block["groups"][0]["check_id"], "chk-1")
        self.assertNotIn("truncated_lines", block)
        call = self.tail_calls[-1]
        self.assertEqual((call["max_lines"], call["grep"], call["package"]),
                         (0, health_mod.ANDROID_ERROR_GREP, APP))

    def test_cut_window_is_reported(self) -> None:
        self.log_lines = [logcat(self.at(-5), "E", "T", "before the cursor 1")]
        self.log_lines += [logcat(self.at(1 + i), "E", "T", f"after {i}") for i in range(5)]
        with mock.patch.object(health_mod, "MAX_LOG_LINES", 3):
            block = health_mod.compute(self.session, self.record, self.target)["logs"]
        self.assertEqual(block["truncated_lines"], 2)
        self.assertEqual(block["lines"], 3)
        self.assertEqual(block["groups"][0]["first_ts"], iso(self.at(3)))

    def test_ios_filter_failure_is_unavailable(self) -> None:
        ios = SimpleNamespace(platform="ios", target_id="SIM", tool="xcrun")
        (self.artifacts / "logs/stream.ndjson").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(logs, "recorded_line_filter",
                               side_effect=errors.AutonomError(errors.BACKEND_FAILED,
                                                               "simctl failed")):
            block = health_mod.compute(self.session, self.record, ios)["logs"]
        self.assertEqual(block["state"], "unavailable")
        self.assertIn("simctl failed", block["reason"])

    def test_normalize_message(self) -> None:
        norm = health_mod.normalize_message
        self.assertEqual(norm("user 42 at 0xFF00 req 3f2a9b7c-1d2e-4f50-8a9b-0c1d2e3f4a5b"),
                         "user N at <hex> req <id>")
        self.assertEqual(norm("hash 9f8e7d6c5b4a3928 and facadecafe"),
                         "hash <hex> and facadecafe")
        self.assertEqual(norm("it's 'quoted' and \"double\""), "it's '<v>' and \"<v>\"")
        self.assertLessEqual(len(norm("x" * 5000)), health_mod.MAX_PATTERN)

    def test_every_pattern_is_linear_on_hostile_input(self) -> None:
        shapes = ["-eyJ" * 50000, "a" * 200000, "a:" + " " * 200000, "http" * 50000,
                  "@" * 100000, "eyJ." * 50000, "Bearer " * 30000, "0x" * 100000,
                  "'" * 100000, '"' * 100000, "1a" * 100000, "a" * 200000 + "!",
                  "12345678-" * 30000, "\t" * 200000, "--:" * 60000]
        for name, pattern in health_mod.PATTERNS.items():
            for shape in shapes:
                started = time.monotonic()
                pattern.sub("", shape)
                pattern.match(shape)
                self.assertLess(time.monotonic() - started, 1.0, f"{name} on {shape[:12]!r}")
        for shape in shapes:
            started = time.monotonic()
            health_mod.normalize_message(shape)
            self.assertLess(time.monotonic() - started, 2.0, shape[:12])


# --- metrics ------------------------------------------------------------------------


class MetricTests(HealthBase):
    def leads(self, before: dict, after: dict) -> list[dict]:
        self.set_baseline(metrics={"state": "available", "snapshot": before})
        self.metric = after
        return health_mod.compute(self.session, self.record, self.target)["metrics"]["leads"]

    def test_memory_needs_both_twenty_percent_and_twenty_mb(self) -> None:
        both = self.leads(snapshot(100_000), snapshot(130_000))
        self.assertEqual([lead["metric"] for lead in both], ["memory_pss_kb"])
        self.assertEqual(both[0]["delta"], 30_000)
        self.assertEqual(both[0]["before"], 100_000)
        self.assertEqual(both[0]["after"], 130_000)
        self.assertEqual(self.leads(snapshot(40_000), snapshot(50_000)), [])  # +25 %, +10 MB
        self.assertEqual(self.leads(snapshot(300_000), snapshot(330_000)), [])  # +10 %, +30 MB

    def test_cpu_needs_fifty_points(self) -> None:
        self.assertEqual([lead["metric"] for lead in
                          self.leads(snapshot(cpu=5), snapshot(cpu=60))], ["cpu_percent"])
        self.assertEqual(self.leads(snapshot(cpu=5), snapshot(cpu=54.9)), [])

    def test_ios_rss_is_compared_in_kb(self) -> None:
        before = {"memory": {"rss_bytes": 100 * 1024 * 1024}, "cpu": {"process_percent": 1}}
        after = {"memory": {"rss_bytes": 150 * 1024 * 1024}, "cpu": {"process_percent": 2}}
        self.assertEqual([lead["metric"] for lead in self.leads(before, after)],
                         ["memory_rss_kb"])

    def test_lead_is_attributed_by_snapshot_time(self) -> None:
        self.check("Remove item", begin=10, end=20)
        after = snapshot(200_000)
        after["captured_at"] = iso(self.at(15))
        self.assertEqual(self.leads(snapshot(100_000), after)[0]["check_id"], "chk-1")

    def test_unavailable_paths(self) -> None:
        self.set_baseline(metrics={"state": "unavailable", "reason": "no app id"})
        block = health_mod.compute(self.session, self.record, self.target)["metrics"]
        self.assertEqual((block["state"], block["leads"]), ("unavailable", []))
        self.set_baseline(metrics={"state": "available", "snapshot": snapshot()})
        self.metric_error = errors.AutonomError(errors.APP_NOT_RUNNING, "app is not running")
        block = health_mod.compute(self.session, self.record, self.target)["metrics"]
        self.assertEqual(block["state"], "unavailable")
        self.assertIn("app is not running", block["reason"])


# --- compute / write / auto findings ---------------------------------------------


class AutoFindingTests(HealthBase):
    def crash_and_500_during_chk1(self) -> None:
        self.check("Remove item", begin=10, end=20, verdict="pass")
        local = self.at(15).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.crashes = [{"name": "App-crash.ips", "date": local, "raw": "x"}]
        self.flows(self.flow("f500", 500, 15))

    def test_tp010_s01_crash_and_500_since_baseline(self) -> None:
        self.crash_and_500_during_chk1()
        result = health_mod.run(self.session, self.record, self.target)
        written = result["auto_findings"]
        by_source = {item["source"]: item for item in written}
        self.assertEqual(sorted(by_source), ["crash", "request"])
        for item in written:
            self.assertEqual(item["origin"], "auto")
            self.assertEqual(item["severity"], "info")
            self.assertFalse(item["confirmed"])
            self.assertEqual(item["check_id"], "chk-1")
        self.assertEqual(by_source["request"]["evidence"], ["request:f500"])
        self.assertEqual(by_source["crash"]["evidence"], ["crash:App-crash.ips"])
        self.assertEqual(by_source["crash"]["data"]["key"], "crash:App-crash.ips")
        path = Path(result["path"])
        self.assertEqual(path.name, "health.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["schema"], "autonom.pretest-health/v1")
        # automatic findings never move a verdict
        checks = checks_mod.fold(self.session, self.record)
        self.assertEqual(checks[0]["verdict"], "pass")
        self.assertEqual(verdict_mod.ticket_verdict(checks, self.findings())["verdict"],
                         "pass")

    def test_tp010_s02_mocked_500_is_not_a_finding(self) -> None:
        self.check("Remove item", begin=10, end=20)
        self.flows(self.flow("mocked", 500, 15, mocked=True))
        result = health_mod.run(self.session, self.record, self.target)
        self.assertEqual(result["health"]["requests"]["failed"], [])
        self.assertEqual([f for f in self.findings() if f["source"] == "request"], [])

    def test_idempotent_by_data_key(self) -> None:
        self.crash_and_500_during_chk1()
        first = health_mod.run(self.session, self.record, self.target)["auto_findings"]
        self.assertEqual(len(first), 2)
        self.assertEqual(health_mod.run(self.session, self.record, self.target)
                         ["auto_findings"], [])
        self.assertEqual(len(self.findings()), 2)
        self.flows(self.flow("f503", 503, 25))
        third = health_mod.run(self.session, self.record, self.target)["auto_findings"]
        self.assertEqual([item["data"]["key"] for item in third],
                         [f"request:f503@{iso(self.at(25))}"])
        self.assertIsNone(third[0]["check_id"])
        self.assertEqual([f["finding_id"] for f in self.findings()], ["f-1", "f-2", "f-3"])

    def test_log_and_metric_findings_are_worded(self) -> None:
        self.check("Remove item", begin=10, end=20)
        self.log_lines = [logcat(self.at(12), "E", "Cart", "boom 1"),
                          logcat(self.at(13), "E", "Cart", "boom 2")]
        self.set_baseline(metrics={"state": "available", "snapshot": snapshot(100_000)})
        self.metric = snapshot(200_000)
        written = health_mod.run(self.session, self.record, self.target)["auto_findings"]
        by_source = {item["source"]: item for item in written}
        self.assertIn("Cart: boom N", by_source["log"]["title"])
        self.assertEqual(by_source["log"]["evidence"],
                         [f"log:{iso(self.at(12))}..{iso(self.at(13))}"])
        self.assertEqual(by_source["log"]["check_id"], "chk-1")
        self.assertTrue(by_source["metrics"]["title"].startswith("Lead:"))
        self.assertIn("lead", by_source["metrics"]["actual"].lower())

    def test_unresolvable_evidence_is_dropped_not_the_finding(self) -> None:
        self.crash_and_500_during_chk1()
        health = health_mod.compute(self.session, self.record, self.target)
        # not written: the crash ref cannot resolve yet
        written = health_mod.auto_findings(self.session, self.record, health)
        crash = next(item for item in written if item["source"] == "crash")
        self.assertEqual(crash["evidence"], [])

    def test_unknown_check_attribution_is_dropped(self) -> None:
        health = {"schema": health_mod.HEALTH_SCHEMA, "crashes": {
            "state": "available", "new": [{"name": "X.ips", "time": None,
                                           "check_id": "chk-99"}]}}
        written = health_mod.auto_findings(self.session, self.record, health)
        self.assertIsNone(written[0]["check_id"])

    def test_unavailable_blocks_make_no_findings(self) -> None:
        health = {"schema": health_mod.HEALTH_SCHEMA,
                  "crashes": {"state": "unavailable", "reason": "x",
                              "new": [{"name": "ghost"}]},
                  "requests": {"state": "available", "failed": [{"id": None}]},
                  "logs": "nonsense", "metrics": {"state": "available", "leads": ["x"]}}
        self.assertEqual(health_mod.auto_findings(self.session, self.record, health), [])

    def test_refusals(self) -> None:
        self.assert_code(errors.INVALID_VALUE, health_mod.auto_findings,
                         self.session, self.record, {"schema": "other"})
        self.assert_code(errors.INVALID_VALUE, health_mod.auto_findings,
                         self.session, self.record, None)
        self.assert_code(errors.INVALID_VALUE, health_mod.compute,
                         self.session, {"no": "id"}, self.target)
        self.assert_code(errors.INVALID_VALUE, health_mod.compute,
                         self.session, {"pretest_id": "../x"}, self.target)
        ghost = dict(self.record, pretest_id="pt_20200101T000000_abcdef")
        self.assert_code(errors.NO_ACTIVE_PRETEST, health_mod.compute,
                         self.session, ghost, self.target)
        self.assert_code(errors.NO_ACTIVE_PRETEST, health_mod.auto_findings,
                         self.session, ghost, {"schema": health_mod.HEALTH_SCHEMA})

    def test_one_unwritable_item_does_not_cost_the_others(self) -> None:
        self.crash_and_500_during_chk1()
        health = health_mod.compute(self.session, self.record, self.target)
        real_add = findings_mod.add
        calls = []

        def flaky(*args, **kwargs):
            calls.append(kwargs["source"])
            if kwargs["source"] == "crash":
                raise errors.AutonomError(errors.INVALID_VALUE, "bad", hint="x")
            return real_add(*args, **kwargs)

        with mock.patch.object(findings_mod, "add", side_effect=flaky):
            written = health_mod.auto_findings(self.session, self.record, health)
        self.assertEqual(calls, ["crash", "request"])
        self.assertEqual([item["source"] for item in written], ["request"])
        # the skipped one is written on the next run
        again = health_mod.auto_findings(self.session, self.record, health)
        self.assertEqual([item["source"] for item in again], ["crash"])

    def test_packed_pretest_is_refused(self) -> None:
        self.crash_and_500_during_chk1()
        health = health_mod.compute(self.session, self.record, self.target)
        store.mark_packed(self.session, self.record, {"path": "/tmp/pack",
                                                      "integrity_sha256": "ab" * 32})
        self.assert_code(errors.PRETEST_FINALIZED, health_mod.auto_findings,
                         self.session, self.record, health)
        self.assert_code(errors.PRETEST_FINALIZED, health_mod.run,
                         self.session, self.record, self.target)
        self.assertEqual(self.findings(), [])

    def test_write_refuses_a_pretest_packed_meanwhile(self) -> None:
        health = health_mod.compute(self.session, self.record, self.target)
        stale = json.loads(json.dumps(self.record))  # loaded while still active
        store.mark_packed(self.session, self.record, {"path": "/tmp/pack",
                                                      "integrity_sha256": "ab" * 32})
        self.assert_code(errors.PRETEST_FINALIZED, health_mod.write,
                         self.session, stale, health)
        self.assertFalse((self.pretest_dir() / health_mod.HEALTH_FILE).exists())

    def test_run_racing_a_pack_changes_nothing(self) -> None:
        self.crash_and_500_during_chk1()
        real_compute = health_mod.compute

        def compute_then_pack(session, record, target):
            result = real_compute(session, record, target)
            store.mark_packed(session, dict(record), {"path": "/tmp/pack",
                                                      "integrity_sha256": "cd" * 32})
            return result

        with mock.patch.object(health_mod, "compute", side_effect=compute_then_pack):
            self.assert_code(errors.PRETEST_FINALIZED, health_mod.run,
                             self.session, self.record, self.target)
        self.assertFalse((self.pretest_dir() / health_mod.HEALTH_FILE).exists())
        self.assertEqual(findings_mod.fold(self.session, store.load(
            self.session, self.record["pretest_id"])), [])

    def test_missing_baseline_makes_every_block_unavailable(self) -> None:
        (self.pretest_dir() / store.BASELINE_FILE).unlink()
        self.flows(self.flow("f500", 500, 5))
        health = health_mod.compute(self.session, self.record, self.target)
        for block in ("crashes", "logs", "requests", "metrics"):
            self.assertEqual(health[block]["state"], "unavailable", block)
        self.assertEqual(health_mod.auto_findings(self.session, self.record, health), [])


# --- repro --------------------------------------------------------------------------


class ReproTests(HealthBase):
    def act(self, verb: str, argv: list[str], ok: bool = True, raw: bool = False) -> None:
        if raw:  # an entry written without the journal's own scrub
            journal_mod.append(self.session, {"kind": "action", "verb": verb, "argv": argv,
                                              "ok": ok, "origin": "agent"})
        else:
            journal_mod.record_action(self.session, verb=verb, argv=argv, payload=None, ok=ok)

    def test_tp011_s01_repro_without_secrets(self) -> None:
        self.act("ui tap", ["ui", "tap", "--desc", "Before"])  # before the check
        self.act("network mock", ["network", "mock", "add", "--match", "*/api/old",
                                  "--status", "503"])
        self.act("network mock", ["network", "mock", "clear"])
        check_id = self.check("Login", begin=1)
        self.act("ui tap", ["ui", "tap", "--desc", "Password"])
        self.act("ui type", ["ui", "type", "hunter2"])
        self.act("ui type", ["--serial", "emulator-5554", "ui", "type", "hunter2"], raw=True)
        self.act("screenshot", ["screenshot", "--label", "after-type"])
        self.act("ui tap", ["ui", "tap", "--desc", "Buy"], ok=False)
        self.act("network mock", ["network", "mock", "add", "--match", "*/api/catalog",
                                  "--status", "500", "--json", '{"token": "s3cr3t"}'])
        self.act("flow run", ["flow", "run", "login.yaml", "--env", "PASSWORD=hunter2",
                              "--env=API_TOKEN=abc123"], raw=True)
        self.act("ui tap", ["ui", "tap", "--text=Catalog", "--index", "0"])
        with self.clock(5):
            checks_mod.end(self.session, self.record, check_id, verdict="fail")
        self.act("ui tap", ["ui", "tap", "--desc", "After"])
        check = checks_mod.fold(self.session, self.record)[0]
        steps = repro_mod.steps(self.session, check)
        self.assertEqual([s["verb"] for s in steps],
                         ["ui tap", "ui type", "ui type", "network mock", "flow run",
                          "ui tap"])
        self.assertEqual(steps[0]["selector"], {"desc": "Password"})
        self.assertEqual(steps[0]["args"], [])
        self.assertEqual(steps[1]["args"], ["<redacted>"])
        self.assertEqual(steps[2]["args"], ["--serial", "emulator-5554", "<redacted>"])
        self.assertEqual(steps[3]["args"][:5], ["add", "--match", "*/api/catalog",
                                                "--status", "500"])
        self.assertEqual(steps[3]["args"][5:], ["--json", "<redacted>"])
        self.assertEqual(steps[3]["mocks_active"],
                         [{"match": "*/api/catalog", "status": 500, "enabled": True}])
        self.assertEqual(steps[1]["mocks_active"], [])
        self.assertIn("PASSWORD=<redacted>", steps[4]["args"])
        self.assertIn("--env=API_TOKEN=<redacted>", steps[4]["args"])
        self.assertEqual(steps[5]["selector"], {"text": "Catalog", "index": "0"})
        self.assertEqual(steps[5]["mocks_active"], [{"match": "*/api/catalog",
                                                     "status": 500, "enabled": True}])
        seqs = [s["seq"] for s in steps]
        self.assertEqual(seqs, sorted(seqs))
        for step in steps:
            self.assertEqual(step["origin"], "agent")
        text = json.dumps(steps)
        for secret in ("hunter2", "s3cr3t", "abc123"):
            self.assertNotIn(secret, text)

    def test_open_never_begun_and_flow_steps(self) -> None:
        never = self.check("Never")
        self.assertEqual(repro_mod.steps(self.session, checks_mod.fold(
            self.session, self.record)[0]), [])
        opened = self.check("Open", begin=1)
        journal_mod.append(self.session, {"kind": "flow_step", "verb": "flow run",
                                          "run_id": "r1", "step_index": 0,
                                          "command": "tapOn", "label": "Open cart",
                                          "ok": True, "status": "passed"})
        journal_mod.append(self.session, {"kind": "note", "text": "a note"})
        self.act("network mock", ["network", "mock", "list"])
        self.act("ui swipe", ["ui", "swipe", "--from", "1,2", "--to", "3,4"])
        states = {c["check_id"]: c for c in checks_mod.fold(self.session, self.record)}
        steps = repro_mod.steps(self.session, states[opened])
        self.assertEqual([s["verb"] for s in steps], ["flow step", "ui swipe"])
        self.assertEqual(steps[0]["args"], ["tapOn"])
        self.assertEqual(steps[0]["origin"], "flow")
        self.assertEqual(steps[0]["run_id"], "r1")
        self.assertEqual(steps[1]["args"], ["--from", "1,2", "--to", "3,4"])
        self.assertNotIn("selector", steps[1])
        finding = {"check_id": opened}
        self.assertEqual(repro_mod.for_finding(self.session, finding,
                                               list(states.values())), steps)
        self.assertEqual(repro_mod.for_finding(self.session, {"check_id": None},
                                               list(states.values())), [])
        self.assertEqual(repro_mod.for_finding(self.session, {"check_id": never},
                                               list(states.values())), [])

    def test_mock_removal_by_id_and_all(self) -> None:
        check_id = self.check("Mocks", begin=1)
        self.act("network mock", ["network", "mock", "add", "--match", "*/a"])
        self.act("network mock", ["network", "mock", "remove", "m_1"])
        self.act("network mock", ["network", "mock", "add", "--url", "https://h/b?token=zz",
                                  "--method", "POST", "--host", "h"])
        self.act("network mock", ["network", "mock", "add", "--match", "*/c"])
        self.act("network mock", ["network", "mock", "remove", "m_2"])
        self.act("network mock", ["network", "mock", "disable", "--all"])
        state = checks_mod.fold(self.session, self.record)[0]
        self.assertEqual(state["check_id"], check_id)
        steps = repro_mod.steps(self.session, state)
        self.assertEqual([s["mocks_active"] for s in steps][:2],
                         [[{"match": "*/a", "status": 200, "enabled": True}], []])
        self.assertEqual(len(steps[3]["mocks_active"]), 2)
        self.assertEqual(steps[3]["mocks_active"][0]["method"], "POST")
        self.assertNotIn("zz", json.dumps(steps))
        self.assertEqual(len(steps[4]["mocks_active"]), 2)  # unknown which rule m_2 was
        self.assertEqual(steps[5]["mocks_active"], [])

    # --- fix round 3, item 1: the mocks active at each step ------------------------

    def registry(self, *rules: dict) -> None:
        """A ``mock_state`` entry as the CLI journals it after a mock change."""
        journal_mod.append(self.session, repro_mod.mock_state_entry(list(rules)))

    @staticmethod
    def rule(identifier: str, status: int, *, enabled: bool = True,
             glob: str = "*/api/catalog") -> dict:
        return {"id": identifier, "match": {"url_glob": glob, "method": "GET", "host": None,
                                            "ignore_query": False},
                "response": {"status": status, "headers": {"X-Token": "hdr-secret"},
                             "body_path": "/tmp/body-secret.json"},
                "enabled": enabled, "note": "note-secret"}

    def test_mock_state_replay_add_update_disable_remove(self) -> None:
        self.registry(self.rule("m_1", 200, glob="*/api/old"))  # before the check
        check_id = self.check("Mocks", begin=1)
        self.act("ui tap", ["ui", "tap", "--desc", "Start"])
        self.act("network mock", ["network", "mock", "add", "--url",
                                  "https://h/api/catalog", "--status", "500"])
        self.registry(self.rule("m_1", 200, glob="*/api/old"), self.rule("m_2", 500))
        self.act("ui tap", ["ui", "tap", "--desc", "Reload"])
        self.act("network mock", ["network", "mock", "update", "m_2", "--status", "404"])
        self.registry(self.rule("m_1", 200, glob="*/api/old"), self.rule("m_2", 404))
        self.act("ui tap", ["ui", "tap", "--desc", "Reload"])
        self.act("network mock", ["network", "mock", "disable", "m_1"])
        self.registry(self.rule("m_1", 200, enabled=False, glob="*/api/old"),
                      self.rule("m_2", 404))
        self.act("network mock", ["network", "mock", "disable", "m_2"])
        self.registry(self.rule("m_1", 200, enabled=False, glob="*/api/old"),
                      self.rule("m_2", 404, enabled=False))
        self.act("ui tap", ["ui", "tap", "--desc", "Reload"])
        self.act("network mock", ["network", "mock", "remove", "m_2"])
        self.registry(self.rule("m_1", 200, enabled=False, glob="*/api/old"))
        journal_mod.append(self.session, {"kind": "flow_step", "verb": "flow run",
                                          "run_id": "r1", "step_index": 0,
                                          "command": "tapOn", "ok": True})
        state = checks_mod.fold(self.session, self.record)[0]
        self.assertEqual(state["check_id"], check_id)
        steps = repro_mod.steps(self.session, state)
        shown = [(s["verb"], [(m["id"], m["status"]) for m in s["mocks_active"]])
                 for s in steps]
        self.assertEqual(shown, [
            ("ui tap", [("m_1", 200)]),                       # only what was active
            ("network mock", [("m_1", 200), ("m_2", 500)]),   # the state the add made
            ("ui tap", [("m_1", 200), ("m_2", 500)]),
            ("network mock", [("m_1", 200), ("m_2", 404)]),   # status after the update
            ("ui tap", [("m_1", 200), ("m_2", 404)]),
            ("network mock", [("m_2", 404)]),                 # m_1 disabled
            ("network mock", []),                             # m_2 disabled
            ("ui tap", []),                                   # nothing active
            ("network mock", []),
            ("flow step", []),                                # regression step: none
        ])
        self.assertTrue(all(m["enabled"] is True for s in steps for m in s["mocks_active"]))
        self.assertEqual(steps[3]["mocks_active"][1],
                         {"id": "m_2", "match": "*/api/catalog", "method": "GET",
                          "status": 404, "enabled": True})
        text = json.dumps(steps)
        for secret in ("hdr-secret", "body-secret", "note-secret"):
            self.assertNotIn(secret, text)

    def test_argv_replay_without_mock_state_entries(self) -> None:
        """An older journal (no mock_state): add, update by id, disable, remove."""
        self.check("Mocks", begin=1)
        self.act("network mock", ["network", "mock", "add", "--match", "*/api/x",
                                  "--status", "500"])
        self.act("ui tap", ["ui", "tap", "--desc", "Reload"])
        self.act("network mock", ["network", "mock", "update", "--status", "404", "m_7"])
        self.act("ui tap", ["ui", "tap", "--desc", "Reload"])
        self.act("network mock", ["network", "mock", "disable", "m_7"])
        self.act("ui tap", ["ui", "tap", "--desc", "Reload"])
        self.act("network mock", ["network", "mock", "enable", "m_7"])
        self.act("network mock", ["network", "mock", "remove", "m_7"])
        self.act("ui tap", ["ui", "tap", "--desc", "Reload"])
        self.act("network mock", ["network", "mock", "update", "m_9", "--status", "418"],
                 ok=False)
        steps = repro_mod.steps(self.session, checks_mod.fold(self.session, self.record)[0])
        self.assertEqual([[m.get("status") for m in s["mocks_active"]] for s in steps],
                         [[500], [500], [404], [404], [], [], [404], [], []])
        self.assertEqual(steps[3]["mocks_active"][0]["id"], "m_7")  # adopted from update

    def test_mock_state_entry_shape(self) -> None:
        entry = repro_mod.mock_state_entry([
            self.rule("m_3", 503, glob="https://api.example.com/v1/x?token=abc123"),
            {"id": "m_4"}, "junk", None])
        self.assertEqual(entry["kind"], "mock_state")
        first, second = entry["mocks"]
        self.assertEqual((first["id"], first["status"], first["enabled"], first["method"]),
                         ("m_3", 503, True, "GET"))
        self.assertNotIn("abc123", json.dumps(entry))
        self.assertEqual(second, {"id": "m_4", "enabled": True})
        self.assertEqual(repro_mod.mock_state_entry("not a list"),
                         {"kind": "mock_state", "mocks": []})

    # --- fix round 3, item 16: a finding's steps end at its latest evidence ---------

    def test_finding_steps_end_at_the_latest_evidence(self) -> None:
        check_id = self.check("Cut", begin=1)
        self.act("ui tap", ["ui", "tap", "--desc", "One"])
        self.act("ui tap", ["ui", "tap", "--desc", "Two"])
        steps = repro_mod.steps(self.session, checks_mod.fold(self.session, self.record)[0])
        cut_at = checks_mod.parse_ts(steps[-1]["ts"])
        # a screenshot right after "Two", then more steps that do not belong
        shots = self.artifacts / "shots"
        (shots / "x.png").write_bytes(b"\x89PNG\r\n\x1a\nx")
        (shots / "index.jsonl").write_text(json.dumps({
            "file": "shots/x.png", "label": "after two",
            "captured_at": cut_at.strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n", encoding="utf-8")
        later = cut_at + timedelta(seconds=30)
        self.flows({"id": "rq-1", "started_at_ms": int(later.timestamp() * 1000),
                    "method": "GET", "host": "h", "path": "/p", "status": 500})
        with mock.patch.object(journal_mod, "_now", return_value=iso(later + timedelta(
                seconds=5))):
            self.act("ui tap", ["ui", "tap", "--desc", "Three"])
            self.act("ui tap", ["ui", "tap", "--desc", "Four"])
        states = checks_mod.fold(self.session, self.record)
        whole = repro_mod.steps(self.session, states[0])
        self.assertEqual(len(whole), 4)
        shot_only = repro_mod.for_finding(
            self.session, {"check_id": check_id, "evidence": ["shot:1"]}, states)
        self.assertEqual([s["selector"]["desc"] for s in shot_only], ["One", "Two"])
        # the latest cited item decides: the request came after "Two"
        both = repro_mod.for_finding(
            self.session, {"check_id": check_id, "evidence": ["shot:1", "request:rq-1"]},
            states)
        self.assertEqual([s["selector"]["desc"] for s in both], ["One", "Two"])
        self.flows({"id": "rq-2", "started_at_ms": int((later + timedelta(
            seconds=40)).timestamp() * 1000), "method": "GET", "host": "h", "path": "/p",
            "status": 500})
        to_rq2 = repro_mod.for_finding(
            self.session, {"check_id": check_id, "evidence": ["request:rq-2"]}, states)
        self.assertEqual([s["selector"]["desc"] for s in to_rq2],
                         ["One", "Two", "Three", "Four"])
        # no evidence, or none that resolves to a time: the whole check
        for evidence in ([], ["shot:99"], ["run:missing"]):
            self.assertEqual(repro_mod.for_finding(
                self.session, {"check_id": check_id, "evidence": evidence}, states), whole)

    def test_bad_check_is_refused(self) -> None:
        self.assert_code(errors.INVALID_VALUE, repro_mod.steps, self.session, "chk-1")
        self.assertEqual(repro_mod.steps(self.session, {"seq_from": True}), [])
        self.assertEqual(repro_mod.steps(self.session, {"seq_from": 1, "seq_to": None}), [])
        journal_mod.append(self.session, {"kind": "action", "verb": "ui tap",
                                          "argv": "ui tap --desc X", "ok": True})
        journal_mod.append(self.session, {"kind": "action", "verb": None, "argv": None})
        steps = repro_mod.steps(self.session, {"seq_from": 1, "seq_to": None})
        self.assertEqual([(s["verb"], s["args"]) for s in steps], [("ui tap", [])])

    def test_redact_args_shapes(self) -> None:
        args, selector = repro_mod.redact_args(
            "ui type", ["ui", "type", "--sensitive", "--", "-secret value"])
        self.assertNotIn("-secret value", args)
        args, _ = repro_mod.redact_args(
            "session start", ["session", "start", "--setenv", "TOKEN=x1", "--header",
                              "Authorization: Bearer y2", "KEY=z3", "plain"])
        self.assertEqual(args, ["--setenv", "TOKEN=<redacted>", "--header",
                                "<redacted>", "KEY=<redacted>", "plain"])
        self.assertEqual(selector, {})


# --- verdict ------------------------------------------------------------------------


def chk(check_id: str, state: str = "ended", verdict: str | None = "pass") -> dict:
    return {"check_id": check_id, "state": state,
            "verdict": verdict if state == "ended" else None}


def finding(finding_id: str, severity: str, confirmed: bool = True,
            origin: str = "agent") -> dict:
    return {"finding_id": finding_id, "severity": severity, "confirmed": confirmed,
            "origin": origin}


class VerdictTests(unittest.TestCase):
    def verdict(self, checks, findings=()) -> dict:
        return verdict_mod.ticket_verdict(checks, list(findings))

    def test_pass(self) -> None:
        out = self.verdict([chk("chk-1"), chk("chk-2")],
                           [finding("f-1", "minor"), finding("f-2", "info", False, "auto")])
        self.assertEqual(out, {"verdict": "pass", "reasons": ["all 2 check(s) passed"],
                               "not_covered": [], "blocked": [], "open": []})

    def test_fail_by_check_and_by_confirmed_blocker_or_major(self) -> None:
        self.assertEqual(self.verdict([chk("chk-1", verdict="fail")])["verdict"], "fail")
        for severity in ("blocker", "major"):
            out = self.verdict([chk("chk-1")], [finding("f-1", severity)])
            self.assertEqual(out["verdict"], "fail")
            self.assertIn(f"finding f-1 is a confirmed {severity}", out["reasons"])
        unconfirmed = self.verdict([chk("chk-1")], [finding("f-1", "blocker", False, "auto")])
        self.assertEqual(unconfirmed["verdict"], "pass")
        minor = self.verdict([chk("chk-1")], [finding("f-1", "minor")])
        self.assertEqual(minor["verdict"], "pass")

    def test_order_fail_before_blocked_before_not_covered(self) -> None:
        checks = [chk("chk-1", verdict="blocked"), chk("chk-2", "added"),
                  chk("chk-3", verdict="fail"), chk("chk-4", "open"),
                  chk("chk-5", verdict="inconclusive")]
        out = self.verdict(checks)
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["blocked"], ["chk-1"])
        self.assertEqual(out["not_covered"], ["chk-2"])
        self.assertEqual(out["open"], ["chk-4"])
        self.assertEqual(self.verdict(checks[:2] + checks[3:])["verdict"], "blocked")
        self.assertEqual(self.verdict(checks[1:2] + checks[3:])["verdict"], "not_covered")
        self.assertEqual(self.verdict(checks[3:])["verdict"], "inconclusive")

    def test_tp012_s01_unrun_check_prevents_pass(self) -> None:
        out = self.verdict([chk("chk-1"), chk("chk-2", "added")])
        self.assertEqual(out["verdict"], "not_covered")
        self.assertEqual(out["not_covered"], ["chk-2"])
        self.assertIn("check chk-2 never ran", out["reasons"])

    def test_not_covered_without_checks_or_by_verdict(self) -> None:
        empty = self.verdict([])
        self.assertEqual(empty["verdict"], "not_covered")
        self.assertEqual(empty["reasons"], ["no checks were added"])
        self.assertEqual(verdict_mod.ticket_verdict(None, None)["verdict"], "not_covered")
        out = self.verdict([chk("chk-1"), chk("chk-2", verdict="not_covered")])
        self.assertEqual((out["verdict"], out["not_covered"]), ("not_covered", ["chk-2"]))

    def test_inconclusive_by_verdict_or_open_check(self) -> None:
        out = self.verdict([chk("chk-1"), chk("chk-2", verdict="inconclusive")])
        self.assertEqual(out["verdict"], "inconclusive")
        out = self.verdict([chk("chk-1"), chk("chk-2", "open")])
        self.assertEqual((out["verdict"], out["open"]), ("inconclusive", ["chk-2"]))
        self.assertIn("check chk-2 is still open", out["reasons"])

    def test_bad_input_is_refused_with_hint(self) -> None:
        for checks, findings in (("chk-1", []), ([], {"f-1": 1}), ([1, 2], []),
                                 ([], [None]), (42, [])):
            with self.subTest(checks=checks, findings=findings):
                with self.assertRaises(errors.AutonomError) as caught:
                    verdict_mod.ticket_verdict(checks, findings)
                payload = caught.exception.as_dict()
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
                self.assertTrue(payload["hint"])

    def test_unknown_state_or_verdict_is_refused_not_passed(self) -> None:
        for bad in ({"check_id": "chk-1", "state": "ended", "verdict": None},
                    {"check_id": "chk-1", "state": "ended", "verdict": "great"},
                    {"check_id": "chk-1", "state": "bogus", "verdict": "pass"},
                    {"check_id": "chk-1"}):
            with self.subTest(check=bad):
                with self.assertRaises(errors.AutonomError) as caught:
                    verdict_mod.ticket_verdict([chk("chk-0"), bad], [])
                payload = caught.exception.as_dict()
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
                self.assertIn("checks.fold", payload["hint"])


if __name__ == "__main__":
    unittest.main()
