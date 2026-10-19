"""Mock `delay_s` / `hang` (A.2) and incomplete-request records (A.3).

Registry validation runs against a temporary registry. The addon's async
request hook runs on fake flows with a fake clock, so nothing here needs
mitmproxy, a device or real waiting.

The fakes follow the event order mitmproxy 12.2.3 really has (checked with
mitmdump and `curl --max-time`): while the request hook is awaited, a client
that hangs up only changes the client connection (`timestamp_end`, state
CLOSED); `flow.error` stays None and the error hook runs after the hook
returns. No test here sets `flow.error` or calls the error hook mid-wait.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors  # noqa: E402
from autonom_lib.network import mitm_addon, mocks, store  # noqa: E402


def run_hook(reader, flow) -> None:
    """Call the request hook the way mitmproxy does: await an awaitable result."""
    pending = reader.request(flow)
    if inspect.isawaitable(pending):
        asyncio.run(pending)


class FakeRequest:
    def __init__(self, url: str = "https://api.example.com/v1/items?token=SECRET1",
                 method: str = "GET") -> None:
        self.method = method
        self.pretty_url = url
        rest = url.split("://", 1)[1]
        self.host = rest.split("/", 1)[0]
        self.pretty_host = self.host
        self.path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        self.timestamp_start = 1_700_000_000.0
        self.headers = {"Authorization": "Bearer SECRET2"}
        self.content = b'{"password":"SECRET3"}'


class FakeError:
    def __init__(self, msg: str, timestamp: float | None = None) -> None:
        self.msg = msg
        self.timestamp = timestamp


class FakeState:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeConn:
    def __init__(self, closed: bool = False) -> None:
        self.peername = ("10.0.2.15", 5555)
        self.timestamp_end = 1.0 if closed else None
        self.state = FakeState("CLOSED" if closed else "OPEN")

    def close(self, at: float | None = 1_700_000_003.0) -> None:
        """What mitmproxy does at once when the client hangs up."""
        self.timestamp_end = at
        self.state = FakeState("CLOSED")


class FakeFlow:
    def __init__(self, url: str | None = None, method: str = "GET") -> None:
        self.request = FakeRequest(url, method) if url else FakeRequest(method=method)
        self.response = None
        self.error = None
        self.metadata: dict = {}
        self.client_conn = FakeConn()
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.error = FakeError("Connection killed.")


class FakeClock:
    """Monotonic time that only moves when the addon sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)
        await asyncio.sleep(0)


class RegistryBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "mocks"
        self.flows_dir = Path(self.tmp.name) / "s" / "network"
        self.flows_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def recorder(self, clock: FakeClock | None = None) -> mitm_addon.AutonomRecorder:
        reader = mitm_addon.AutonomRecorder()
        reader.directory = str(self.flows_dir)
        reader.mocks_path = str(mocks.registry_file(self.registry))
        reader._make_response = lambda status, body, headers: {  # noqa: SLF001
            "status": status, "body": body, "headers": headers}
        if clock is not None:
            reader._sleep = clock.sleep  # noqa: SLF001
            reader._clock = clock.clock  # noqa: SLF001
        return reader

    def records(self) -> list[dict]:
        path = self.flows_dir / "flows.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def touch_registry(self) -> None:
        """Defeat a same-mtime write on coarse file systems."""
        path = mocks.registry_file(self.registry)
        stat = path.stat()
        import os
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


class DelayValidationTests(RegistryBase):
    def assert_refused(self, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            mocks.validate_delay(**kwargs)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(caught.exception.hint, "delay is seconds in (0, 120], e.g. 2.5")
        return caught.exception

    def test_in_range_values_pass_and_are_floats(self) -> None:
        self.assertEqual(mocks.validate_delay(2.5), 2.5)
        self.assertEqual(mocks.validate_delay(120), 120.0)
        self.assertEqual(mocks.validate_delay("0.1"), 0.1)
        self.assertIsNone(mocks.validate_delay(None))
        self.assertIsNone(mocks.validate_delay(None, True))

    def test_text_must_be_a_plain_decimal(self) -> None:
        for value in ("3", "0.5", ".5", "2.", " 2.5 ", "120"):
            with self.subTest(value=value):
                self.assertEqual(mocks.validate_delay(value), float(value))
        for value in ("2_5", "1_0.5", "1e1", "1E1", "5e-1", "+3", "-0.5", "inf", "nan",
                      "0x10", "1.5.2", "\u0663", "3 s"):
            with self.subTest(value=value):
                error = self.assert_refused(delay_s=value)
                self.assertIn("is not a number of seconds in (0, 120]", error.message)

    def test_out_of_range_and_non_numbers_are_refused(self) -> None:
        for value in (0, -1, 120.0001, 1e9, math.nan, math.inf, -math.inf, "abc", "", True,
                      [1]):
            with self.subTest(value=value):
                self.assert_refused(delay_s=value)

    def test_delay_and_hang_together_are_refused(self) -> None:
        error = self.assert_refused(delay_s=2, hang=True)
        self.assertIn("--hang", error.message)

    def test_add_stores_delay_or_hang_never_both(self) -> None:
        delayed = mocks.add(url_glob="*", body_text="{}", delay_s=2.5, registry=self.registry)
        self.assertEqual(delayed["delay_s"], 2.5)
        self.assertNotIn("hang", delayed)
        held = mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        self.assertIs(held["hang"], True)
        self.assertNotIn("delay_s", held)
        plain = mocks.add(url_glob="*", body_text="{}", registry=self.registry)
        self.assertNotIn("delay_s", plain)
        self.assertNotIn("hang", plain)
        listed = {rule["id"]: rule for rule in mocks.load(self.registry)}
        self.assertEqual(listed[delayed["id"]]["delay_s"], 2.5)
        self.assertIs(listed[held["id"]]["hang"], True)

    def test_refused_add_writes_nothing(self) -> None:
        with self.assertRaises(errors.AutonomError):
            mocks.add(url_glob="*", body_text="{}", delay_s=0, registry=self.registry)
        with self.assertRaises(errors.AutonomError):
            mocks.add(url_glob="*", body_text="{}", delay_s=1, hang=True, registry=self.registry)
        self.assertEqual(mocks.load(self.registry), [])

    def test_update_switches_between_delay_and_hang(self) -> None:
        rule = mocks.add(url_glob="*", body_text="{}", delay_s=1, registry=self.registry)
        held = mocks.update(rule["id"], hang=True, registry=self.registry)
        self.assertIs(held["hang"], True)
        self.assertNotIn("delay_s", held)
        delayed = mocks.update(rule["id"], delay_s=3, registry=self.registry)
        self.assertEqual(delayed["delay_s"], 3.0)
        self.assertNotIn("hang", delayed)
        mocks.update(rule["id"], hang=True, registry=self.registry)
        released = mocks.update(rule["id"], hang=False, registry=self.registry)
        self.assertNotIn("hang", released)
        untouched = mocks.update(rule["id"], note="n", registry=self.registry)
        self.assertNotIn("hang", untouched)
        self.assertNotIn("delay_s", untouched)

    def test_refused_update_changes_nothing(self) -> None:
        rule = mocks.add(url_glob="*", body_text="{}", delay_s=1, registry=self.registry)
        for kwargs in ({"delay_s": 500}, {"delay_s": 2, "hang": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(errors.AutonomError):
                mocks.update(rule["id"], **kwargs, registry=self.registry)
        self.assertEqual(mocks.load(self.registry)[0]["delay_s"], 1.0)

    def test_get_shows_delay_and_hang(self) -> None:
        rule = mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        self.assertIs(mocks.get(rule["id"], self.registry, counts={})["hang"], True)


class AddonRuleFieldTests(unittest.TestCase):
    def test_rule_delay_ignores_bad_values_and_caps_big_ones(self) -> None:
        self.assertEqual(mitm_addon.rule_delay({"delay_s": 2}), 2.0)
        self.assertEqual(mitm_addon.rule_delay({"delay_s": 9999}), mitm_addon.HANG_CAP_S)
        for value in (None, 0, -2, "3", True, float("nan"), [1]):
            with self.subTest(value=value):
                self.assertIsNone(mitm_addon.rule_delay({"delay_s": value}))
        self.assertIsNone(mitm_addon.rule_delay(None))

    def test_rule_hangs_only_on_true(self) -> None:
        self.assertTrue(mitm_addon.rule_hangs({"hang": True}))
        for value in (None, False, 1, "true"):
            self.assertFalse(mitm_addon.rule_hangs({"hang": value}))


class AddonDelayTests(RegistryBase):
    def test_a_delayed_mock_waits_then_answers(self) -> None:
        mocks.add(url_glob="*/v1/*", body_text='{"ok":1}', delay_s=2.5, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        run_hook(self.recorder(clock), flow)
        # Short steps, so a client that hangs up is noticed within one step.
        self.assertAlmostEqual(sum(clock.sleeps), 2.5)
        self.assertLessEqual(max(clock.sleeps), mitm_addon.HANG_POLL_S)
        self.assertFalse(flow.killed)
        self.assertEqual(flow.response["status"], 200)
        self.assertEqual(flow.response["body"], b'{"ok":1}')
        self.assertEqual(flow.metadata["autonom_mock_id"], "m_1")

    def test_a_plain_mock_answers_synchronously(self) -> None:
        mocks.add(url_glob="*", body_text="{}", registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        self.assertIsNone(self.recorder(clock).request(flow))
        self.assertEqual(clock.sleeps, [])
        self.assertIsNotNone(flow.response)

    def test_a_waiting_rule_returns_an_awaitable(self) -> None:
        mocks.add(url_glob="*", body_text="{}", delay_s=1, registry=self.registry)
        pending = self.recorder(FakeClock()).request(FakeFlow())
        self.assertTrue(inspect.isawaitable(pending))
        asyncio.run(pending)

    def test_no_rule_means_no_response(self) -> None:
        flow = FakeFlow()
        run_hook(self.recorder(FakeClock()), flow)
        self.assertIsNone(flow.response)

    def test_a_client_that_hangs_up_during_the_delay_ends_the_wait(self) -> None:
        mocks.add(url_glob="*", body_text="{}", delay_s=5, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        reader = self.recorder(clock)

        def hang_up(fake: FakeClock) -> None:
            if fake.now >= 1.0:
                flow.client_conn.close(at=1_700_000_001.25)
                fake.on_sleep = None

        clock.on_sleep = hang_up
        run_hook(reader, flow)
        self.assertIsNone(flow.response)
        self.assertTrue(flow.killed)  # nothing goes upstream for a gone client
        self.assertLess(clock.now, 1.0 + mitm_addon.HANG_POLL_S + 1e-9)
        # mitmproxy runs the error hook only after the request hook returned.
        reader.error(flow)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["error_kind"], "client_abort")
        self.assertIs(records[0]["completed"], False)
        self.assertTrue(records[0]["mocked"])
        self.assertEqual(records[0]["mock_id"], "m_1")
        self.assertEqual(records[0]["duration_ms"], 1250)

    def test_a_client_gone_before_the_delay_starts_is_not_waited_for(self) -> None:
        mocks.add(url_glob="*", body_text="{}", delay_s=5, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        flow.client_conn.close()
        run_hook(self.recorder(clock), flow)
        self.assertEqual(clock.sleeps, [])
        self.assertIsNone(flow.response)
        self.assertEqual([r["error_kind"] for r in self.records()], ["client_abort"])

    def test_a_delay_does_not_block_other_flows(self) -> None:
        """Real event loop: the unmocked flow finishes while the mock still waits."""
        mocks.add(url_glob="*/slow", body_text="{}", delay_s=0.05, registry=self.registry)
        reader = self.recorder()
        order: list[str] = []

        async def run(name: str, flow: FakeFlow) -> None:
            pending = reader.request(flow)
            if inspect.isawaitable(pending):
                await pending
            order.append(name)

        async def both() -> None:
            await asyncio.gather(run("slow", FakeFlow("https://h.example/slow")),
                                 run("fast", FakeFlow("https://h.example/fast")))

        asyncio.run(both())
        self.assertEqual(order, ["fast", "slow"])


class AddonHangTests(RegistryBase):
    def test_hang_is_released_to_upstream_when_the_rule_is_removed(self) -> None:
        rule = mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        reader = self.recorder(clock)

        def remove_after_a_second(fake: FakeClock) -> None:
            if fake.now >= 1.0 and mocks.load(self.registry):
                mocks.remove(rule["id"], self.registry)
                self.touch_registry()

        clock.on_sleep = remove_after_a_second
        run_hook(reader, flow)
        self.assertIsNone(flow.response)
        self.assertFalse(flow.killed)
        self.assertNotIn("autonom_mock_id", flow.metadata)
        self.assertLess(clock.now, 2.0)
        self.assertEqual(self.records(), [])

    def test_hang_is_released_when_the_rule_is_disabled(self) -> None:
        rule = mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()

        def disable(fake: FakeClock) -> None:
            if fake.now >= 0.5:
                mocks.set_enabled(rule["id"], False, registry=self.registry)
                self.touch_registry()
                fake.on_sleep = None

        clock.on_sleep = disable
        run_hook(self.recorder(clock), flow)
        self.assertIsNone(flow.response)
        self.assertFalse(flow.killed)

    def test_hang_switched_to_a_delay_serves_the_mock(self) -> None:
        rule = mocks.add(url_glob="*", body_text='{"x":1}', hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()

        def switch(fake: FakeClock) -> None:
            if fake.now >= 0.5:
                mocks.update(rule["id"], delay_s=1, registry=self.registry)
                self.touch_registry()
                fake.on_sleep = None

        switched_at: list[float] = []

        def switch_once(fake: FakeClock) -> None:
            switch(fake)
            if fake.on_sleep is None:
                switched_at.append(fake.now)

        clock.on_sleep = switch_once
        run_hook(self.recorder(clock), flow)
        self.assertEqual(flow.response["body"], b'{"x":1}')
        # The delay starts once the hold sees the switch (next poll).
        self.assertAlmostEqual(clock.now - switched_at[0], 1.0)

    def test_hang_is_killed_at_the_cap_and_recorded_incomplete(self) -> None:
        mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        reader = self.recorder(clock)
        run_hook(reader, flow)
        self.assertTrue(flow.killed)
        self.assertIsNone(flow.response)
        self.assertAlmostEqual(clock.now, 120.0)
        self.assertLessEqual(max(clock.sleeps), mitm_addon.HANG_POLL_S)
        # mitmproxy then fires the error hook for the killed flow: no second record.
        reader.error(flow)
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["completed"], False)
        self.assertEqual(record["error_kind"], "hang_cap")
        self.assertTrue(record["mocked"])
        self.assertEqual(record["mock_id"], "m_1")
        self.assertIsNone(record["status"])

    def test_a_client_that_hangs_up_during_a_hang_ends_the_hold_once(self) -> None:
        mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        reader = self.recorder(clock)

        def hang_up(fake: FakeClock) -> None:
            if fake.now >= 3:
                flow.client_conn.close()
                fake.on_sleep = None

        clock.on_sleep = hang_up
        run_hook(reader, flow)
        self.assertTrue(flow.killed)
        self.assertIsNone(flow.response)
        self.assertLess(clock.now, 3 + mitm_addon.HANG_POLL_S + 1e-9)
        reader.error(flow)  # the queued close reaches the error hook afterwards
        kinds = [record["error_kind"] for record in self.records()]
        self.assertEqual(kinds, ["client_abort"])
        self.assertTrue(self.records()[0]["mocked"])

    def test_a_closed_state_without_an_end_time_counts_as_gone(self) -> None:
        mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()

        def close_state(fake: FakeClock) -> None:
            flow.client_conn.state = FakeState("CLOSED")
            fake.on_sleep = None

        clock.on_sleep = close_state
        run_hook(self.recorder(clock), flow)
        self.assertTrue(flow.killed)
        self.assertEqual([r["error_kind"] for r in self.records()], ["client_abort"])

    def test_cap_without_a_store_still_kills(self) -> None:
        mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()
        reader = self.recorder(clock)
        reader.directory = ""
        run_hook(reader, flow)
        self.assertTrue(flow.killed)

    def test_kill_that_raises_is_tolerated(self) -> None:
        mocks.add(url_glob="*", body_text="{}", hang=True, registry=self.registry)
        clock = FakeClock()
        flow = FakeFlow()

        def broken_kill() -> None:
            raise RuntimeError("flow not killable")

        flow.kill = broken_kill
        run_hook(self.recorder(clock), flow)
        self.assertEqual([r["error_kind"] for r in self.records()], ["hang_cap"])


class IncompleteRecordTests(RegistryBase):
    def test_error_hook_writes_one_record_without_headers_or_bodies(self) -> None:
        flow = FakeFlow()
        flow.error = FakeError("Server connection to api.example.com failed: refused",
                               timestamp=1_700_000_001.5)
        reader = self.recorder()
        reader.error(flow)
        reader.error(flow)  # a second call must not duplicate
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["completed"], False)
        self.assertEqual(record["error_kind"], "upstream_error")
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["host"], "api.example.com")
        self.assertEqual(record["path"], "/v1/items")
        self.assertEqual(record["duration_ms"], 1500)
        self.assertEqual(record["started_at"], "2023-11-14T22:13:20Z")
        self.assertFalse(record["mocked"])
        blob = json.dumps(records)
        for secret in ("SECRET1", "SECRET2", "SECRET3"):
            self.assertNotIn(secret, blob)
        for key in ("request_headers_preview", "response_headers_preview",
                    "request_body_preview", "response_body_preview"):
            self.assertNotIn(key, record)

    def test_unknown_start_time_omits_duration(self) -> None:
        flow = FakeFlow()
        flow.request.timestamp_start = None
        flow.error = FakeError("weird")
        record = mitm_addon.incomplete_record(flow, "f_0001", "other", now=1_700_000_000.0)
        self.assertNotIn("duration_ms", record)
        self.assertEqual(record["started_at"], "2023-11-14T22:13:20Z")

    def test_an_unknown_kind_is_recorded_as_other(self) -> None:
        record = mitm_addon.incomplete_record(FakeFlow(), "f_0001", "boom")
        self.assertEqual(record["error_kind"], "other")

    def test_a_flow_already_recorded_by_response_is_not_recorded_again(self) -> None:
        flow = FakeFlow()
        flow.metadata["autonom_recorded"] = True
        flow.error = FakeError("Client disconnected.")
        self.recorder().error(flow)
        self.assertEqual(self.records(), [])

    def test_no_store_directory_writes_nothing(self) -> None:
        flow = FakeFlow()
        flow.error = FakeError("Client disconnected.")
        reader = self.recorder()
        reader.directory = ""
        reader.error(flow)
        self.assertFalse((self.flows_dir / "flows.jsonl").exists())

    def test_closed_client_connection_marks_an_unclear_error_as_client_abort(self) -> None:
        flow = FakeFlow()
        flow.client_conn = FakeConn(closed=True)
        flow.error = FakeError("stream reset")
        self.recorder().error(flow)
        self.assertEqual(self.records()[0]["error_kind"], "client_abort")

    def test_client_closed_at(self) -> None:
        flow = FakeFlow()
        self.assertIsNone(mitm_addon.client_closed_at(flow))
        flow.client_conn.close(at=5.5)
        self.assertEqual(mitm_addon.client_closed_at(flow), 5.5)
        for value in (None, 0, True, "x"):
            with self.subTest(value=value):
                flow.client_conn.timestamp_end = value
                flow.client_conn.state = FakeState("OPEN")
                self.assertIsNone(mitm_addon.client_closed_at(flow))
        flow.client_conn.state = FakeState("CLOSED")
        self.assertIsNotNone(mitm_addon.client_closed_at(flow))
        flow.client_conn = None
        self.assertIsNone(mitm_addon.client_closed_at(flow))

    def test_an_http2_client_close_is_a_client_abort(self) -> None:
        """mitmdump 12.2.3 says "peer closed connection" when an HTTP/2 client leaves."""
        flow = FakeFlow()
        flow.client_conn.close()
        flow.error = FakeError("peer closed connection")
        self.recorder().error(flow)
        self.assertEqual(self.records()[0]["error_kind"], "client_abort")

    def test_error_kinds_from_messages(self) -> None:
        cases = {
            "Client disconnected.": "client_abort",
            "client closed connection": "client_abort",
            "Server connection to example.com failed": "upstream_error",
            "TLS handshake failed": "upstream_error",
            "peer closed connection without sending complete message body": "upstream_error",
            "peer closed connection": "upstream_error",
            "[Errno 8] nodename nor servname provided, or not known": "upstream_error",
            "[Errno 61] Connect call failed ('127.0.0.1', 1)": "upstream_error",
            "Connection killed.": "other",
            "": "other",
            None: "other",
        }
        for message, kind in cases.items():
            with self.subTest(message=message):
                self.assertEqual(mitm_addon.classify_error(message), kind)
        self.assertEqual(mitm_addon.classify_error("Connection killed.", client_side=True),
                         "client_abort")
        self.assertEqual(mitm_addon.classify_error("TLS failed", client_side=True),
                         "upstream_error")
        self.assertEqual(mitm_addon.classify_error("peer closed connection", client_side=True),
                         "client_abort")
        self.assertEqual(mitm_addon.classify_error("timed out", client_side=True),
                         "client_abort")

    def test_incomplete_records_list_through_the_store(self) -> None:
        flow = FakeFlow()
        flow.error = FakeError("Client disconnected.")
        self.recorder().error(flow)
        record = {"artifacts_dir": str(self.flows_dir.parent)}
        listed = store.listing(record)
        self.assertEqual(listed["count"], 1)
        self.assertIs(listed["requests"][0]["completed"], False)
        self.assertEqual(store.filter_flows(listed["requests"], status=200), [])


if __name__ == "__main__":
    unittest.main()
