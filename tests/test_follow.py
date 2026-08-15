"""Live observation (§2L): bounded follows, stream catalog, path confinement."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
UI_DUMP = ROOT / "tests/fixtures/ui_dump.xml"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors, follow  # noqa: E402
from autonom_lib.network import store  # noqa: E402


class FakeTime:
    """Deterministic clock: sleep() advances monotonic time, nothing waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FollowFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.log"
        self.emitted: list = []

    def _emit(self, payload) -> None:
        self.emitted.append(payload)

    def lines(self) -> list[str]:
        return [e["text"] for e in self.emitted
                if isinstance(e, dict) and e.get("kind") == "line"]

    def eof(self) -> dict:
        self.assertEqual(self.emitted[-1]["kind"], "eof")
        return self.emitted[-1]

    def test_starts_at_eof_and_reads_only_appended_lines(self) -> None:
        self.path.write_text("old line\n", encoding="utf-8")

        def writer() -> None:
            time.sleep(0.05)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("first\nsecond\n")

        thread = threading.Thread(target=writer)
        thread.start()
        eof = follow.follow_file(self.path, source="t", emit=self._emit,
                                 max_lines=2, max_seconds=5, poll_ms=20)
        thread.join()
        self.assertEqual(self.lines(), ["first", "second"])
        self.assertEqual(eof["reason"], "max_lines")
        self.assertNotIn("old line", self.lines())

    def test_from_start_replays_the_existing_file(self) -> None:
        self.path.write_text("a\nb\nc\n", encoding="utf-8")
        follow.follow_file(self.path, source="t", emit=self._emit,
                           from_start=True, max_lines=3, max_seconds=5)
        self.assertEqual(self.lines(), ["a", "b", "c"])

    def test_grep_filters_and_only_emitted_lines_count(self) -> None:
        self.path.write_text("err one\ninfo\nerr two\nnoise\n", encoding="utf-8")
        follow.follow_file(self.path, source="t", emit=self._emit,
                           from_start=True, grep="^err", max_lines=2,
                           max_seconds=5)
        self.assertEqual(self.lines(), ["err one", "err two"])

    def test_quiet_file_expires_on_max_seconds(self) -> None:
        self.path.write_text("", encoding="utf-8")
        fake = FakeTime()
        eof = follow.follow_file(self.path, source="t", emit=self._emit,
                                 max_seconds=3, poll_ms=100,
                                 clock=fake.clock, sleep=fake.sleep)
        self.assertEqual(eof["reason"], "max_seconds")
        self.assertEqual(eof["lines"], 0)

    def test_missing_file_is_polled_for_until_it_appears(self) -> None:
        def writer() -> None:
            time.sleep(0.05)
            self.path.write_text("born\n", encoding="utf-8")

        thread = threading.Thread(target=writer)
        thread.start()
        follow.follow_file(self.path, source="t", emit=self._emit,
                           max_lines=1, max_seconds=5, poll_ms=20)
        thread.join()
        self.assertEqual(self.lines(), ["born"])

    def test_rotation_reopens_and_reads_the_new_file(self) -> None:
        self.path.write_text("one long original line\n", encoding="utf-8")

        def rotate() -> None:
            time.sleep(0.05)
            self.path.write_text("rotated\n", encoding="utf-8")  # shrinks

        thread = threading.Thread(target=rotate)
        thread.start()
        follow.follow_file(self.path, source="t", emit=self._emit,
                           from_start=True, max_lines=2, max_seconds=5,
                           poll_ms=20)
        thread.join()
        self.assertEqual(self.lines(), ["one long original line", "rotated"])

    def test_raw_mode_passes_lines_through_verbatim(self) -> None:
        self.path.write_text('{"kind":"action"}\n', encoding="utf-8")
        follow.follow_file(self.path, source="journal", emit=self._emit,
                           from_start=True, raw=True, max_lines=1,
                           max_seconds=5)
        self.assertEqual(self.emitted[0], '{"kind":"action"}')

    def test_invalid_grep_is_a_positioned_refusal(self) -> None:
        with self.assertRaises(errors.AutonomError) as ctx:
            follow.follow_file(self.path, source="t", emit=self._emit,
                               grep="(", max_seconds=1)
        self.assertEqual(ctx.exception.code, errors.BACKEND_FAILED)


class ConfineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "artifacts"
        (self.base / "output").mkdir(parents=True)

    def test_relative_and_absolute_inside_are_allowed(self) -> None:
        inside = self.base / "output/app.log"
        inside.write_text("x\n", encoding="utf-8")
        self.assertEqual(follow.confine(self.base, "output/app.log"), inside.resolve())
        self.assertEqual(follow.confine(self.base, str(inside)), inside.resolve())

    def test_escapes_are_refused(self) -> None:
        for raw in ("../secrets.txt", "/etc/passwd", "output/../../x"):
            with self.assertRaises(errors.AutonomError) as ctx:
                follow.confine(self.base, raw)
            self.assertEqual(ctx.exception.code, errors.PATH_FORBIDDEN)

    def test_symlink_escape_is_refused(self) -> None:
        outside = Path(self.tmp.name) / "outside.log"
        outside.write_text("secret\n", encoding="utf-8")
        (self.base / "output/link.log").symlink_to(outside)
        with self.assertRaises(errors.AutonomError):
            follow.confine(self.base, "output/link.log")


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "output").mkdir()
        (self.base / "logs").mkdir()
        self.record = {"artifacts_dir": str(self.base), "streams": []}

    def test_registered_and_scanned_streams_with_hints(self) -> None:
        (self.base / "output/flutter_run.log").write_text("l1\n", encoding="utf-8")
        (self.base / "logs/stream.ndjson").write_text("{}\n", encoding="utf-8")
        (self.base / "journal.ndjson").write_text("{}\n", encoding="utf-8")
        self.record["streams"] = [{"id": "log_stream", "kind": "device_log",
                                   "path": "logs/stream.ndjson", "pid": 42}]
        entries = follow.catalog(self.record)
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(set(by_id), {"log_stream", "output:flutter_run.log", "journal"})
        scanned = by_id["output:flutter_run.log"]
        self.assertEqual(scanned["kind"], "output")
        self.assertTrue(scanned["abs_path"].endswith("output/flutter_run.log"))
        self.assertIn(scanned["abs_path"], scanned["shell_hint"])
        self.assertIn("autonom logs follow --path output/flutter_run.log",
                      scanned["follow_hint"])
        self.assertEqual(by_id["log_stream"]["pid"], 42)
        self.assertEqual(by_id["journal"]["follow_hint"], "autonom journal --follow")

    def test_registered_stream_not_yet_written_is_listed_as_missing(self) -> None:
        self.record["streams"] = [{"id": "later", "kind": "output",
                                   "path": "output/later.log"}]
        entries = follow.catalog(self.record)
        self.assertEqual(entries[0]["exists"], False)

    def test_resolve_source_understands_ids_and_dir_forms(self) -> None:
        (self.base / "output/app.log").write_text("", encoding="utf-8")
        self.record["streams"] = [{"id": "mine", "kind": "output",
                                   "path": "output/app.log"}]
        self.assertEqual(follow.resolve_source(self.record, "mine"),
                         (self.base / "output/app.log").resolve())
        self.assertEqual(follow.resolve_source(self.record, "output:app.log"),
                         (self.base / "output/app.log").resolve())
        with self.assertRaises(errors.AutonomError) as ctx:
            follow.resolve_source(self.record, "nope")
        self.assertEqual(ctx.exception.code, errors.STREAM_NOT_FOUND)


class FollowPollTests(unittest.TestCase):
    def test_emits_each_new_item_once_and_stops_at_max(self) -> None:
        batches = [[{"id": "f_1"}], [], [{"id": "f_2"}, {"id": "f_3"}]]
        emitted: list = []
        fake = FakeTime()
        eof = follow.follow_poll(
            lambda: batches.pop(0) if batches else [],
            emit=emitted.append, interval=1, max_seconds=60, max_items=3,
            clock=fake.clock, sleep=fake.sleep)
        flows = [e["flow"]["id"] for e in emitted if e["kind"] == "flow"]
        self.assertEqual(flows, ["f_1", "f_2", "f_3"])
        self.assertEqual(eof["reason"], "max")

    def test_quiet_store_expires_on_max_seconds(self) -> None:
        fake = FakeTime()
        eof = follow.follow_poll(lambda: [], emit=lambda _: None, interval=1,
                                 max_seconds=5, clock=fake.clock,
                                 sleep=fake.sleep)
        self.assertEqual(eof["reason"], "max_seconds")
        self.assertEqual(eof["count"], 0)

    def test_long_interval_never_overshoots_max_seconds(self) -> None:
        fake = FakeTime()
        follow.follow_poll(lambda: [], emit=lambda _: None, interval=60,
                           max_seconds=5, clock=fake.clock, sleep=fake.sleep)
        self.assertLessEqual(fake.now, 5.1,
                             "--max-seconds is a hard bound, not a hint")


class FollowProcessTests(unittest.TestCase):
    def test_final_unterminated_line_is_flushed_at_stream_end(self) -> None:
        emitted: list = []
        eof = follow.follow_process(
            [sys.executable, "-c",
             "import sys; sys.stdout.write('done\\npartial-tail')"],
            source="t", emit=emitted.append, max_seconds=10)
        lines = [e["text"] for e in emitted if e.get("kind") == "line"]
        self.assertEqual(lines, ["done", "partial-tail"])
        self.assertEqual(eof["reason"], "stream_ended")
        self.assertEqual(eof["lines"], 2)


class SinceIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        network = Path(self.tmp.name) / "network"
        network.mkdir()
        flows = [{"id": f"f_{i:04d}", "host": "api.example.com",
                  "method": "GET", "status": 200, "path": "/v1",
                  "url": "https://api.example.com/v1",
                  "started_at": "2026-08-15T00:00:00Z"} for i in range(3)]
        (network / "flows.jsonl").write_text(
            "".join(json.dumps(f) + "\n" for f in flows), encoding="utf-8")
        self.record = {"artifacts_dir": self.tmp.name}

    def test_since_id_returns_only_later_flows(self) -> None:
        payload = store.listing(self.record, since_id="f_0000")
        ids = [flow["id"] for flow in payload["requests"]]
        self.assertEqual(sorted(ids), ["f_0001", "f_0002"])

    def test_unknown_since_id_warns_instead_of_hiding_traffic(self) -> None:
        payload = store.listing(self.record, since_id="f_9999")
        self.assertEqual(payload["total_matched"], 3)
        codes = [w["code"] for w in payload.get("warnings", [])]
        self.assertIn("since_id_not_found", codes)


class FollowCliTests(unittest.TestCase):
    """End-to-end over the real CLI with a fake adb and a sandboxed home."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({"ui_dump": str(UI_DUMP)}), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
        })
        result = self._cli("session", "start", "--app-id", "com.example.app")
        assert result.returncode == 0, result.stderr
        self.artifacts = Path(json.loads(result.stdout)["session"]["artifacts_dir"])

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554",
             "--adb", str(ROOT / "tests/fakes/fake_adb.py"), *args],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=60,
        )

    def _ndjson(self, stdout: str) -> list[dict]:
        return [json.loads(line) for line in stdout.splitlines() if line.strip()]

    def test_session_outputs_catalogs_written_files(self) -> None:
        (self.artifacts / "output/build.log").write_text("hello\n", encoding="utf-8")
        result = self._cli("session", "outputs")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ids = [s["id"] for s in payload["streams"]]
        self.assertIn("output:build.log", ids)
        entry = next(s for s in payload["streams"] if s["id"] == "output:build.log")
        self.assertTrue(Path(entry["abs_path"]).is_file())
        self.assertIn("tail -f", entry["shell_hint"])

    def test_logs_follow_path_emits_ndjson_lines_then_eof(self) -> None:
        (self.artifacts / "output/app.log").write_text("one\ntwo\n", encoding="utf-8")
        result = self._cli("logs", "follow", "--path", "output/app.log",
                           "--from-start", "--max-lines", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self._ndjson(result.stdout)
        self.assertEqual([e["text"] for e in events[:2]], ["one", "two"])
        self.assertEqual(events[-1]["kind"], "eof")
        self.assertEqual(events[-1]["reason"], "max_lines")

    def test_logs_follow_refuses_paths_outside_artifacts(self) -> None:
        result = self._cli("logs", "follow", "--path", "../../etc/passwd",
                           "--max-seconds", "1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"], "path_forbidden")

    def test_logs_follow_unknown_source_names_the_catalog(self) -> None:
        result = self._cli("logs", "follow", "--source", "bogus",
                           "--max-seconds", "1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"], "stream_not_found")

    def test_network_requests_follow_from_start_streams_flows(self) -> None:
        flows_file = self.artifacts / "network/flows.jsonl"
        flows = [{"id": "f_0001", "host": "a.example.com", "method": "GET",
                  "status": 200, "path": "/x", "url": "https://a.example.com/x",
                  "started_at": "2026-08-15T00:00:00Z"},
                 {"id": "f_0002", "host": "b.example.com", "method": "GET",
                  "status": 500, "path": "/y", "url": "https://b.example.com/y",
                  "started_at": "2026-08-15T00:00:01Z"}]
        flows_file.write_text(
            "".join(json.dumps(f) + "\n" for f in flows), encoding="utf-8")
        result = self._cli("network", "requests", "follow", "--from-start",
                           "--max", "2", "--interval", "0.05",
                           "--max-seconds", "10")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self._ndjson(result.stdout)
        ids = [e["flow"]["id"] for e in events if e["kind"] == "flow"]
        self.assertEqual(ids, ["f_0001", "f_0002"])
        self.assertEqual(events[-1], {"kind": "eof", "reason": "max", "count": 2})

    def test_network_requests_list_supports_since_id(self) -> None:
        flows_file = self.artifacts / "network/flows.jsonl"
        flows_file.write_text(
            json.dumps({"id": "f_0001", "host": "a", "started_at": "x"}) + "\n"
            + json.dumps({"id": "f_0002", "host": "b", "started_at": "y"}) + "\n",
            encoding="utf-8")
        result = self._cli("network", "requests", "list", "--since-id", "f_0001")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([f["id"] for f in payload["requests"]], ["f_0002"])

    def test_journal_follow_streams_raw_entries(self) -> None:
        self._cli("note", "add", "checkpoint reached")
        result = self._cli("journal", "--follow", "--from-start",
                           "--max-lines", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self._ndjson(result.stdout)
        self.assertEqual(events[0]["kind"], "action")  # session start journals
        self.assertEqual(events[-1]["kind"], "eof")

    def test_device_follow_streams_fake_logcat_until_it_ends(self) -> None:
        result = self._cli("logs", "follow", "--source", "device",
                           "--max-seconds", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self._ndjson(result.stdout)
        self.assertEqual(events[-1]["kind"], "eof")
        self.assertIn(events[-1]["reason"], {"stream_ended", "max_seconds"})

    def test_consumer_closing_the_pipe_is_a_clean_exit(self) -> None:
        # `autonom … --follow | head -1`: EPIPE must end the stream quietly,
        # never with a traceback breaching the one-error-envelope contract
        process = subprocess.Popen(
            [sys.executable, str(CLI), "--platform", "android",
             "--serial", "emulator-5554",
             "--adb", str(ROOT / "tests/fakes/fake_adb.py"),
             "journal", "--follow", "--from-start", "--max-seconds", "5"],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        assert process.stdout is not None
        process.stdout.readline()
        process.stdout.close()  # the reader walks away mid-stream
        _, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn("Traceback", stderr)


class IosDeviceFollowTests(unittest.TestCase):
    """The iOS device branch honors --session-id and dead stream writers."""

    UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({
            "installed": ["com.example.app"],
            "ios_log": ['{"live": 1}', '{"live": 2}'],
        }), encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "AUTONOM_HOME": str(Path(self.tmp.name) / "home"),
            "AUTONOM_FAKE_STATE": str(state),
            "AUTONOM_FAKE_LOG": str(Path(self.tmp.name) / "log.jsonl"),
        })
        result = self._cli("session", "start", "--app-id", "com.example.app")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        self.session_id = payload["session"]["session_id"]
        self.artifacts = Path(payload["session"]["artifacts_dir"])

    def _cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), "--platform", "ios",
             "--target", self.UDID,
             "--simctl", str(ROOT / "tests/fakes/fake_simctl.py"),
             "--idb", str(ROOT / "tests/fakes/fake_idb.py"), *args],
            env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=60,
        )

    def test_session_id_replays_that_sessions_recorded_stream(self) -> None:
        (self.artifacts / "logs/stream.ndjson").write_text(
            '{"recorded": "line"}\n', encoding="utf-8")
        result = self._cli("logs", "follow", "--source", "device",
                           "--session-id", self.session_id, "--max-lines", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[0]["text"], '{"recorded": "line"}')

    def test_session_id_without_a_recording_refuses(self) -> None:
        result = self._cli("logs", "follow", "--source", "device",
                           "--session-id", self.session_id,
                           "--max-seconds", "1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         "stream_not_found")

    def test_dead_stream_writer_falls_back_to_live_logs(self) -> None:
        # a stream file exists but no writer pid is alive: tailing the dead
        # file would show nothing forever — the live spawn must win
        (self.artifacts / "logs/stream.ndjson").write_text(
            '{"stale": true}\n', encoding="utf-8")
        result = self._cli("logs", "follow", "--source", "device",
                           "--max-seconds", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        texts = [json.loads(line).get("text", "")
                 for line in result.stdout.splitlines()]
        self.assertIn('{"live": 1}', texts)


if __name__ == "__main__":
    unittest.main()
