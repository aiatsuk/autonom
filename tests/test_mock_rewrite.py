"""Rewrite rules: edit the real response (MR-06..MR-10).

The addon's request and response hooks run on fake flows whose response
mimics mitmproxy 12's message API (`get_content(strict=True)` raising
ValueError on a bad Content-Encoding, `set_content` re-encoding and setting
Content-Length). No mitmproxy, device or real waiting is needed.
"""
from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors  # noqa: E402
from autonom_lib.network import har, mitm_addon, mocks, store  # noqa: E402


class FakeHeaders:
    """Case-insensitive multi-field headers, as mitmproxy's `Headers`."""

    def __init__(self, pairs=()) -> None:
        self.fields = [(str(k), str(v)) for k, v in pairs]

    def items(self, multi: bool = False):
        if multi:
            return list(self.fields)
        return [(key, self[key]) for key in self.keys()]

    def keys(self):
        seen, result = set(), []
        for key, _ in self.fields:
            if key.lower() not in seen:
                seen.add(key.lower())
                result.append(key)
        return result

    def __contains__(self, name) -> bool:
        return any(key.lower() == str(name).lower() for key, _ in self.fields)

    def __getitem__(self, name):
        values = [v for k, v in self.fields if k.lower() == name.lower()]
        if not values:
            raise KeyError(name)
        return ", ".join(values)

    def get(self, name, default=None):
        return self[name] if name in self else default

    def __setitem__(self, name, value) -> None:
        self.fields = [(k, v) for k, v in self.fields if k.lower() != name.lower()]
        self.fields.append((name, str(value)))

    def __delitem__(self, name) -> None:
        if name not in self:
            raise KeyError(name)
        self.fields = [(k, v) for k, v in self.fields if k.lower() != name.lower()]


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers=(),
                 encoding: str | None = None) -> None:
        self.status_code = status
        self.reason = "OK"
        self.headers = FakeHeaders(headers)
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self.raw_content = gzip.compress(body) if encoding == "gzip" else body
        self.headers["Content-Length"] = str(len(self.raw_content))
        self.timestamp_end = 1_700_000_000.5

    def get_content(self, strict: bool = True) -> bytes:
        encoding = (self.headers.get("content-encoding") or "identity").lower()
        if encoding == "identity":
            return self.raw_content
        if encoding == "gzip":
            try:
                return gzip.decompress(self.raw_content)
            except OSError:
                if strict:
                    raise ValueError("Invalid gzip") from None
                return self.raw_content
        if strict:
            raise ValueError(f"Invalid Content-Encoding: {encoding}")
        return self.raw_content

    @property
    def content(self) -> bytes:
        return self.get_content(strict=False)

    def set_content(self, value: bytes) -> None:
        encoding = (self.headers.get("content-encoding") or "identity").lower()
        self.raw_content = gzip.compress(value) if encoding == "gzip" else value
        if "transfer-encoding" not in self.headers:
            self.headers["content-length"] = str(len(self.raw_content))

    def decoded(self) -> bytes:
        return self.get_content(strict=True)


class FakeRequest:
    def __init__(self, url: str = "https://api.test/items", method: str = "GET",
                 headers=(), body: bytes = b"") -> None:
        self.method = method
        self.pretty_url = url
        rest = url.split("://", 1)[1]
        self.host = rest.split("/", 1)[0]
        self.pretty_host = self.host
        self.path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        self.timestamp_start = 1_700_000_000.0
        self.headers = FakeHeaders(headers)
        self.raw_content = body
        self.content = body

    def get_content(self, strict: bool = True) -> bytes:
        return self.raw_content


class FakeState:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeConn:
    def __init__(self) -> None:
        self.peername = ("10.0.2.15", 5555)
        self.timestamp_end = None
        self.state = FakeState("OPEN")

    def close(self, at: float = 1_700_000_003.0) -> None:
        self.timestamp_end = at
        self.state = FakeState("CLOSED")


class FakeFlow:
    def __init__(self, request: FakeRequest | None = None) -> None:
        self.request = request or FakeRequest()
        self.response = None
        self.error = None
        self.metadata: dict = {}
        self.client_conn = FakeConn()
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class FakeClock:
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


def await_hook(result) -> None:
    if inspect.isawaitable(result):
        asyncio.run(result)


class Base(unittest.TestCase):
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
        reader._make_response = lambda status, body, headers: FakeResponse(  # noqa: SLF001
            status, body, headers.items())
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
        import os

        path = mocks.registry_file(self.registry)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    def run_flow(self, reader, flow: FakeFlow, upstream: FakeResponse | None) -> None:
        """Request hook; then, when nothing answered, the 'server' answers."""
        await_hook(reader.request(flow))
        if flow.response is None and not flow.killed:
            flow.response = upstream
        if flow.response is not None and not flow.killed:
            await_hook(reader.response(flow))

    def assert_refused(self, fn, *args, flag: str | None = None, **kwargs):
        with self.assertRaises(errors.AutonomError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        if flag:
            self.assertIn(flag, caught.exception.message + " " + caught.exception.hint)
        return caught.exception


# --- MR-06 ----------------------------------------------------------------------


class ReachRealServerTests(Base):
    def test_mr06_s01_status_and_header_edit(self) -> None:
        mocks.add(url_glob="*/items", mode="rewrite", status=503,
                  headers=mocks.parse_header_args(["Retry-After: 5"]),
                  remove_headers=["ETag"], registry=self.registry)
        flow = FakeFlow()
        upstream = FakeResponse(200, b'{"items":[]}', [("ETag", "abc"), ("etag", "def"),
                                                       ("Content-Type", "application/json")])
        reader = self.recorder()
        self.assertIsNone(reader.request(flow))
        self.assertIsNone(flow.response)  # INV-4: the request goes upstream
        self.assertEqual(flow.metadata, {"autonom_mock_id": "m_1",
                                         "autonom_mock_kind": "rewrite"})
        flow.response = upstream
        reader.response(flow)
        self.assertEqual(upstream.status_code, 503)
        self.assertEqual(upstream.headers["Retry-After"], "5")
        self.assertNotIn("ETag", upstream.headers)
        self.assertEqual(upstream.decoded(), b'{"items":[]}')
        record = self.records()[0]
        self.assertEqual((record["status"], record["upstream_status"]), (503, 200))
        self.assertTrue(record["mocked"])
        self.assertEqual((record["mock_id"], record["mock_kind"]), ("m_1", "rewrite"))
        self.assertEqual(record["rewrite"], {"applied": ["status", "remove_headers", "headers"],
                                             "error": None})

    def test_without_status_the_upstream_status_is_kept(self) -> None:
        added = mocks.add(url_glob="*", mode="rewrite", replace=[["a", "b"]],
                          registry=self.registry)
        self.assertIsNone(added["response"]["status"])
        upstream = FakeResponse(404, b"a")
        self.run_flow(self.recorder(), FakeFlow(), upstream)
        self.assertEqual(upstream.status_code, 404)
        self.assertEqual(upstream.decoded(), b"b")

    def test_an_earlier_respond_rule_still_wins(self) -> None:
        mocks.add(url_glob="*", status=418, registry=self.registry)
        mocks.add(url_glob="*", mode="rewrite", status=503, registry=self.registry)
        flow = FakeFlow()
        self.run_flow(self.recorder(), flow, FakeResponse(200))
        self.assertEqual(flow.response.status_code, 418)
        self.assertEqual(self.records()[0]["mock_kind"], "respond")
        self.assertNotIn("rewrite", self.records()[0])

    def test_rewrite_rules_use_request_conditions(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", status=500, query=["page=2"],
                  registry=self.registry)
        page1, page2 = FakeResponse(200), FakeResponse(200)
        self.run_flow(self.recorder(), FakeFlow(FakeRequest("https://h/l?page=1")), page1)
        self.run_flow(self.recorder(), FakeFlow(FakeRequest("https://h/l?page=2")), page2)
        self.assertEqual((page1.status_code, page2.status_code), (200, 500))

    def test_a_reason_phrase_never_raises(self) -> None:
        response = FakeResponse(200)
        mitm_addon.rewrite_status_headers(response, {"status": 503})
        self.assertEqual(response.status_code, 503)


# --- MR-07 -----------------------------------------------------------------------


class BodyEditTests(Base):
    def test_mr07_s01_json_edit_on_a_gzip_response(self) -> None:
        mocks.add(url_glob="*", mode="rewrite",
                  json_set=[mocks.parse_path_value('user.plan="pro"', "--json-set")],
                  json_remove=["user.trial_ends"], registry=self.registry)
        upstream = FakeResponse(200, b'{"user":{"plan":"free","trial_ends":"x"}}',
                                [("Content-Type", "application/json")], encoding="gzip")
        self.run_flow(self.recorder(), FakeFlow(), upstream)
        self.assertEqual(upstream.headers["Content-Encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(upstream.raw_content)),
                         {"user": {"plan": "pro"}})
        self.assertEqual(int(upstream.headers["Content-Length"]), len(upstream.raw_content))
        self.assertEqual(self.records()[0]["rewrite"],
                         {"applied": ["json_set", "json_remove"], "error": None})

    def test_mr07_s02_text_replace(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", replace=[("Free", "Pro")],
                  registry=self.registry)
        upstream = FakeResponse(200, b"Free plan. Free!")
        self.run_flow(self.recorder(), FakeFlow(), upstream)
        self.assertEqual(upstream.decoded(), b"Pro plan. Pro!")
        self.assertEqual(upstream.headers["Content-Length"], "14")

    def test_json_edits_run_before_text_replacements(self) -> None:
        plan = {"json_set": [("a", "Free")], "json_remove": [], "replace": [("Free", "Pro")]}
        self.assertEqual(json.loads(mitm_addon.rewrite_body(b'{"a":1}', plan)), {"a": "Pro"})

    def test_json_set_and_remove_semantics(self) -> None:
        document = b'{"a":{"b":1},"items":[{"id":1},{"id":2}]}'
        ok = {"json_set": [("a.c", [1]), ("items.1.id", 9), ("a.b", None)],
              "json_remove": ["items.0"], "replace": []}
        self.assertEqual(json.loads(mitm_addon.rewrite_body(document, ok)),
                         {"a": {"b": None, "c": [1]}, "items": [{"id": 9}]})
        for plan, reason in (
                ({"json_set": [("items.5", 1)]}, "path_not_found: items.5"),
                ({"json_set": [("x.y", 1)]}, "path_not_found: x.y"),
                ({"json_set": [("a.b.c", 1)]}, "path_not_found: a.b.c"),
                ({"json_remove": ["a.zz"]}, "path_not_found: a.zz"),
                ({"json_remove": ["items.x"]}, "path_not_found: items.x")):
            with self.subTest(plan=plan), self.assertRaises(mitm_addon.RewriteError) as caught:
                mitm_addon.rewrite_body(document, plan)
            self.assertEqual(caught.exception.reason, reason)

    def test_a_set_value_is_copied_so_later_edits_never_touch_the_rule(self) -> None:
        value = {"k": [1]}
        plan = {"json_set": [("a", value), ("b", value)], "json_remove": ["a.k"],
                "replace": []}
        self.assertEqual(json.loads(mitm_addon.rewrite_body(b"{}", plan)),
                         {"a": {}, "b": {"k": [1]}})
        self.assertEqual(value, {"k": [1]})

    def test_whole_body_replacement(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", body_text='{"fake":true}',
                  registry=self.registry)
        upstream = FakeResponse(200, b'{"real":1}', [("Content-Type", "text/plain")],
                                encoding="gzip")
        self.run_flow(self.recorder(), FakeFlow(), upstream)
        self.assertEqual(upstream.decoded(), b'{"fake":true}')
        self.assertEqual(upstream.headers["Content-Type"], "application/json")
        self.assertEqual(self.records()[0]["rewrite"]["applied"], ["headers", "body"])

    def test_whole_body_and_edits_are_refused_together(self) -> None:
        self.assert_refused(mocks.add, url_glob="*", mode="rewrite", body_text="{}",
                            json_set=["a=1"], registry=self.registry, flag="--json-set")
        # A whole body with header removal is fine.
        mocks.add(url_glob="*", mode="rewrite", body_text="{}", remove_headers=["ETag"],
                  registry=self.registry)


# --- MR-08 ------------------------------------------------------------------------


class FailedEditTests(Base):
    def outcome(self, body: bytes, encoding: str | None = None, **kwargs):
        mocks.add(url_glob="*", mode="rewrite", registry=self.registry, **kwargs)
        upstream = FakeResponse(200, body, [("ETag", "e")], encoding=encoding)
        raw = upstream.raw_content
        self.run_flow(self.recorder(), FakeFlow(), upstream)
        return upstream, raw, self.records()[-1]

    def test_mr08_s01_missing_path(self) -> None:
        upstream, raw, record = self.outcome(b'{"x":1}', status=299, json_set=["a.b=1"])
        self.assertEqual(upstream.status_code, 299)
        self.assertEqual(upstream.raw_content, raw)
        self.assertEqual(upstream.raw_content, b'{"x":1}')
        self.assertEqual(record["rewrite"], {"applied": ["status"],
                                             "error": "path_not_found: a.b"})

    def test_non_json_body_is_left_byte_for_byte(self) -> None:
        upstream, raw, record = self.outcome(b"<html>Free</html>", json_set=["a=1"],
                                             replace=[["Free", "Pro"]],
                                             remove_headers=["ETag"])
        self.assertEqual(upstream.raw_content, raw)
        self.assertNotIn("ETag", upstream.headers)
        self.assertEqual(record["rewrite"], {"applied": ["remove_headers"],
                                             "error": "not_json"})

    def test_body_over_eight_mib_passes_through(self) -> None:
        body = b"F" * (mitm_addon.REWRITE_BODY_LIMIT + 1)
        upstream, raw, record = self.outcome(body, replace=[["F", "P"]])
        self.assertIs(upstream.raw_content, raw)
        self.assertEqual(record["rewrite"]["error"], "body_too_large")

    def test_body_decoding_over_eight_mib_passes_through(self) -> None:
        body = b"F" * (mitm_addon.REWRITE_BODY_LIMIT + 1)
        upstream, raw, record = self.outcome(body, encoding="gzip", replace=[["F", "P"]])
        self.assertEqual(upstream.raw_content, raw)
        self.assertEqual(record["rewrite"]["error"], "body_too_large")

    def test_undecodable_encoding_passes_through(self) -> None:
        upstream, raw, record = self.outcome(b"Free", encoding="x-custom", status=201,
                                             replace=[["Free", "Pro"]])
        self.assertEqual(upstream.raw_content, raw)
        self.assertEqual(upstream.status_code, 201)
        self.assertEqual(record["rewrite"]["error"], "undecodable_encoding")

    def test_broken_gzip_is_undecodable(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", replace=[["x", "y"]], registry=self.registry)
        upstream = FakeResponse(200, b"x", encoding="gzip")
        upstream.raw_content = b"not gzip at all"
        self.run_flow(self.recorder(), FakeFlow(), upstream)
        self.assertEqual(upstream.raw_content, b"not gzip at all")
        self.assertEqual(self.records()[0]["rewrite"]["error"], "undecodable_encoding")

    def test_non_utf8_body_with_text_replace(self) -> None:
        upstream, raw, record = self.outcome(b"\xff\xfeFree", replace=[["Free", "Pro"]])
        self.assertEqual(upstream.raw_content, raw)
        self.assertEqual(record["rewrite"]["error"], "not_text")

    def test_unreadable_whole_body_file(self) -> None:
        rule = mocks.add(url_glob="*", mode="rewrite", body_text="new", status=202,
                         registry=self.registry)
        Path(rule["response"]["body_path"]).unlink()
        upstream = FakeResponse(200, b"old")
        self.run_flow(self.recorder(), FakeFlow(), upstream)
        self.assertEqual((upstream.status_code, upstream.raw_content), (202, b"old"))
        self.assertEqual(self.records()[0]["rewrite"]["error"], "body_unreadable")

    def test_malformed_hand_edited_edits_are_invalid_rule(self) -> None:
        malformed = [
            {"rewrite": {"json_set": "a=1"}},
            {"rewrite": {"json_set": [{"path": "a..b", "value": 1}]}},
            {"rewrite": {"json_set": [{"path": "a"}]}},
            {"rewrite": {"json_remove": [""]}},
            {"rewrite": {"replace": [{"find": "", "with": "x"}]}},
            {"rewrite": {"replace": [["a", "b"]]}},
            {"rewrite": {"remove_headers": ["bad name"]}},
            {"rewrite": "nope"},
            {"response": {"status": 700}},
            {"response": {"status": "503"}},
            {"response": {"headers": {"bad name": "x"}}},
            {"response": {"headers": ["x"]}},
            {"response": {"body_path": "/x"}, "rewrite": {"replace": [{"find": "a",
                                                                         "with": "b"}]}},
        ]
        for index, extra in enumerate(malformed):
            with self.subTest(extra=extra):
                rule = {"id": f"bad{index}", "enabled": True, "mode": "rewrite",
                        "match": {"url_glob": "*"},
                        "response": {"status": None, "headers": {}, "body_path": None}}
                for key, value in extra.items():
                    rule[key] = ({**rule[key], **value} if key == "response" else value)
                self.registry.mkdir(parents=True, exist_ok=True)
                mocks.registry_file(self.registry).write_text(json.dumps({"mocks": [rule]}))
                self.touch_registry()
                upstream = FakeResponse(200, b"abc", [("ETag", "e")])
                self.run_flow(self.recorder(), FakeFlow(), upstream)
                self.assertEqual((upstream.status_code, upstream.raw_content), (200, b"abc"))
                self.assertIn("ETag", upstream.headers)
                self.assertEqual(self.records()[-1]["rewrite"],
                                 {"applied": [], "error": "invalid_rule"})

    def test_rule_gone_between_request_and_response(self) -> None:
        for change in ("remove", "disable", "respond"):
            with self.subTest(change=change):
                rule = mocks.add(url_glob="*", mode="rewrite", status=503,
                                 registry=self.registry)
                reader = self.recorder()
                flow = FakeFlow()
                reader.request(flow)
                if change == "remove":
                    mocks.remove(rule["id"], self.registry)
                elif change == "disable":
                    mocks.set_enabled(rule["id"], False, registry=self.registry)
                else:
                    mocks.update(rule["id"], mode="respond", registry=self.registry)
                self.touch_registry()
                flow.response = FakeResponse(200, b"real")
                await_hook(reader.response(flow))
                self.assertEqual((flow.response.status_code, flow.response.raw_content),
                                 (200, b"real"))
                record = self.records()[-1]
                self.assertEqual(record["rewrite"], {"applied": [], "error": "rule_gone"})
                self.assertEqual(record["upstream_status"], 200)
                mocks.clear(self.registry)

    def test_upstream_failure_is_an_incomplete_flow_with_the_rule(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", status=503, registry=self.registry)
        reader = self.recorder()
        flow = FakeFlow()
        reader.request(flow)

        class Error:
            msg = "Server connection to api.test failed: refused"
            timestamp = 1_700_000_001.0

        flow.error = Error()
        reader.error(flow)
        record = self.records()[0]
        self.assertEqual((record["completed"], record["error_kind"]), (False, "upstream_error"))
        self.assertEqual((record["mock_id"], record["mock_kind"]), ("m_1", "rewrite"))
        self.assertNotIn("rewrite", record)

    def test_apply_rewrite_never_raises(self) -> None:
        class Broken:
            status_code = 200
            headers = None  # every header operation raises

        flow = FakeFlow()
        flow.response = Broken()
        outcome = mitm_addon.apply_rewrite(flow, {"mode": "rewrite", "response": {
            "headers": {"X-A": "1"}}})
        self.assertEqual(outcome["error"], "invalid_rule")


# --- MR-09 -------------------------------------------------------------------------


class DelayTests(Base):
    def test_mr09_s01_delay_after_the_real_response(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", status=503, delay_s=1.5,
                  registry=self.registry)
        clock = FakeClock()
        reader = self.recorder(clock)
        reader._wall = lambda: 1_700_000_000.5 + clock.now  # noqa: SLF001
        flow = FakeFlow()
        self.assertIsNone(reader.request(flow))  # no wait before going upstream
        self.assertEqual(clock.sleeps, [])
        flow.response = FakeResponse(200, b"x")
        pending = reader.response(flow)
        self.assertTrue(inspect.isawaitable(pending))
        self.assertEqual(flow.response.status_code, 503)  # edited before the hold
        self.assertEqual(self.records(), [])               # recorded after it
        asyncio.run(pending)
        self.assertAlmostEqual(sum(clock.sleeps), 1.5)
        self.assertLessEqual(max(clock.sleeps), mitm_addon.HANG_POLL_S)
        self.assertEqual(len(self.records()), 1)
        # 0.5 s upstream plus the 1.5 s hold.
        self.assertEqual(self.records()[0]["duration_ms"], 2000)
        self.assertFalse(flow.killed)

    def test_a_client_that_hangs_up_during_the_delay(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", status=503, delay_s=5, registry=self.registry)
        clock = FakeClock()
        reader = self.recorder(clock)
        flow = FakeFlow()
        reader.request(flow)
        flow.response = FakeResponse(200, b"x")

        def hang_up(fake: FakeClock) -> None:
            if fake.now >= 1.0:
                flow.client_conn.close(at=1_700_000_001.25)
                fake.on_sleep = None

        clock.on_sleep = hang_up
        await_hook(reader.response(flow))
        self.assertTrue(flow.killed)
        self.assertLess(clock.now, 1.0 + mitm_addon.HANG_POLL_S + 1e-9)

        class Error:
            msg = "Client disconnected."
            timestamp = None

        flow.error = Error()
        reader.error(flow)  # mitmproxy's error hook after the kill adds nothing
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record["completed"], record["error_kind"]), (False, "client_abort"))
        self.assertEqual((record["mock_id"], record["mock_kind"]), ("m_1", "rewrite"))
        self.assertEqual(record["upstream_status"], 200)
        self.assertEqual(record["duration_ms"], 1250)

    def test_two_parallel_flows_the_unmatched_one_is_not_delayed(self) -> None:
        mocks.add(url_glob="*/slow", mode="rewrite", delay_s=0.05, replace=[["a", "b"]],
                  registry=self.registry)
        reader = self.recorder()
        order: list[str] = []

        async def run(name: str, url: str) -> None:
            flow = FakeFlow(FakeRequest(url))
            pending = reader.request(flow)
            if inspect.isawaitable(pending):
                await pending
            flow.response = FakeResponse(200, b"a")
            pending = reader.response(flow)
            if inspect.isawaitable(pending):
                await pending
            order.append(name)

        async def both() -> None:
            await asyncio.gather(run("slow", "https://h/slow"), run("fast", "https://h/fast"))

        asyncio.run(both())
        self.assertEqual(order, ["fast", "slow"])
        kinds = {record["url"]: record.get("mock_kind") for record in self.records()}
        self.assertEqual(kinds, {"https://h/fast": None, "https://h/slow": "rewrite"})

    def test_hang_with_rewrite_is_refused(self) -> None:
        self.assert_refused(mocks.add, url_glob="*", mode="rewrite", hang=True,
                            registry=self.registry, flag="--hang")
        rule = mocks.add(url_glob="*", mode="rewrite", registry=self.registry)
        self.assert_refused(mocks.update, rule["id"], hang=True, registry=self.registry,
                            flag="--hang")
        held = mocks.add(url_glob="*", hang=True, registry=self.registry)
        self.assert_refused(mocks.update, held["id"], mode="rewrite", registry=self.registry,
                            flag="--hang")
        switched = mocks.update(held["id"], mode="rewrite", delay_s=2, registry=self.registry)
        self.assertEqual((switched["mode"], switched["delay_s"]), ("rewrite", 2.0))
        self.assertNotIn("hang", switched)

    def test_a_held_respond_rule_switched_to_rewrite_goes_upstream(self) -> None:
        rule = mocks.add(url_glob="*", hang=True, registry=self.registry)
        clock = FakeClock()
        reader = self.recorder(clock)
        flow = FakeFlow()

        def switch(fake: FakeClock) -> None:
            if fake.now >= 0.5:
                mocks.update(rule["id"], mode="rewrite", hang=False, status=503,
                             registry=self.registry)
                self.touch_registry()
                fake.on_sleep = None

        clock.on_sleep = switch
        await_hook(reader.request(flow))
        self.assertIsNone(flow.response)
        self.assertEqual(flow.metadata["autonom_mock_kind"], "rewrite")
        flow.response = FakeResponse(200)
        await_hook(reader.response(flow))
        self.assertEqual(flow.response.status_code, 503)


# --- MR-10 and the registry ---------------------------------------------------------


class RecordTests(Base):
    def test_mr10_s01_filter_and_records_carry_no_edit_values(self) -> None:
        mocks.add(url_glob="*/fake", status=201, registry=self.registry)
        mocks.add(url_glob="*/edit", mode="rewrite", json_set=["token=SECRET-VALUE-1"],
                  headers={"X-Secret": "SECRET-VALUE-2"}, replace=[["a", "SECRET-VALUE-3"]],
                  registry=self.registry)
        reader = self.recorder()
        for path in ("fake", "edit", "plain"):
            self.run_flow(reader, FakeFlow(FakeRequest(f"https://h/{path}")),
                          FakeResponse(200, b'{"token":"x","a":1}'))
        listed = store.listing({"artifacts_dir": str(self.flows_dir.parent)}, mocked=True)
        kinds = [(flow["path"], flow["mock_kind"]) for flow in listed["requests"]]
        self.assertEqual(sorted(kinds), [("/edit", "rewrite"), ("/fake", "respond")])
        plain = [flow for flow in self.records() if flow["path"] == "/plain"][0]
        self.assertEqual((plain["mocked"], plain["mock_kind"]), (False, None))
        self.assertNotIn("rewrite", plain)
        edited = [flow for flow in self.records() if flow["path"] == "/edit"][0]
        self.assertEqual(edited["rewrite"], {"applied": ["headers", "json_set", "replace"],
                                             "error": None})
        flows_text = (self.flows_dir / "flows.jsonl").read_text("utf-8")
        self.assertNotIn("SECRET-VALUE-1", flows_text)  # also scrubbed as a token field
        self.assertNotIn("SECRET-VALUE-3", json.dumps(edited["rewrite"]))
        self.assertNotIn("SECRET-VALUE-2", json.dumps(edited["rewrite"]))

    def test_har_keeps_the_edited_response_and_the_kind(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", status=503, replace=[["Free", "Pro"]],
                  registry=self.registry)
        self.run_flow(self.recorder(), FakeFlow(), FakeResponse(200, b"Free"))
        entry = har.entry_for(self.records()[0])
        self.assertEqual(entry["response"]["status"], 503)
        self.assertEqual(entry["response"]["content"]["text"], "Pro")
        self.assertEqual(entry["_autonom"]["mock_kind"], "rewrite")
        self.assertEqual(har.entry_for({"mocked": True, "mock_kind": None})["_autonom"]
                         ["mock_kind"], "respond")
        self.assertIsNone(har.entry_for({"mocked": False, "mock_kind": None})["_autonom"]
                          ["mock_kind"])
        # A record from before rewrite rules exports exactly as it did.
        self.assertEqual(set(har.entry_for({"mocked": True})["_autonom"]),
                         {"id", "mocked", "mock_id"})

    def test_respond_flows_record_their_kind(self) -> None:
        mocks.add(url_glob="*", status=201, delay_s=0.5, registry=self.registry)
        self.run_flow(self.recorder(FakeClock()), FakeFlow(), None)
        record = self.records()[0]
        self.assertEqual((record["mock_kind"], record["status"]), ("respond", 201))
        self.assertNotIn("upstream_status", record)

    def test_rewrite_applies_without_a_store_directory(self) -> None:
        mocks.add(url_glob="*", mode="rewrite", status=503, registry=self.registry)
        reader = self.recorder()
        reader.directory = ""
        upstream = FakeResponse(200)
        self.run_flow(reader, FakeFlow(), upstream)
        self.assertEqual(upstream.status_code, 503)
        self.assertFalse((self.flows_dir / "flows.jsonl").exists())


class RegistryRewriteTests(Base):
    def test_mr11_s02_edit_flags_need_rewrite(self) -> None:
        for kwargs, flag in (({"json_set": ["a=1"]}, "--json-set"),
                             ({"json_remove": ["a"]}, "--json-remove"),
                             ({"replace": [["a", "b"]]}, "--replace"),
                             ({"remove_headers": ["ETag"]}, "--remove-header")):
            with self.subTest(flag=flag):
                error = self.assert_refused(mocks.add, url_glob="*", registry=self.registry,
                                            flag=flag, **kwargs)
                self.assertIn("--rewrite", error.message + error.hint)
        self.assertEqual(mocks.load(self.registry), [])

    def test_edit_validation(self) -> None:
        long = "x" * (mocks.MAX_CONDITION_VALUE + 1)
        cases = [
            ({"mode": "fake"}, "rewrite"),
            ({"remove_headers": ["bad name"]}, "--remove-header"),
            ({"remove_headers": [""]}, "--remove-header"),
            ({"remove_headers": ["X"] * 21}, "--remove-header"),
            ({"json_set": ["a"]}, "--json-set"),
            ({"json_set": ["a..b=1"]}, "--json-set"),
            ({"json_remove": [""]}, "--json-remove"),
            ({"json_remove": ["a."]}, "--json-remove"),
            ({"replace": [["", "x"]]}, "--replace"),
            ({"replace": [["a"]]}, "--replace"),
            ({"replace": [["a", long]]}, "--replace"),
            ({"replace": [["a", 1]]}, "--replace"),
            ({"headers": {"bad name": "x"}}, "--header"),
            ({"status": 700}, "--status"),
        ]
        for kwargs, flag in cases:
            with self.subTest(kwargs=kwargs):
                self.assert_refused(mocks.add, url_glob="*", registry=self.registry,
                                    flag=flag, **{"mode": "rewrite", **kwargs})
        self.assertEqual(mocks.load(self.registry), [])

    def test_stored_shape(self) -> None:
        rule = mocks.add(url_glob="*/p", mode="rewrite", status=503, headers={"X-A": "1"},
                         remove_headers=["ETag"], json_set=['user.plan="pro"'],
                         json_remove=["user.trial_ends"], replace=[["Free", "Pro"]],
                         delay_s=1.5, query=["page=2"], registry=self.registry)
        self.assertEqual(rule["mode"], "rewrite")
        self.assertEqual(rule["response"], {"status": 503, "headers": {"X-A": "1"},
                                            "body_path": None})
        self.assertEqual(rule["rewrite"], {
            "remove_headers": ["ETag"], "json_set": [{"path": "user.plan", "value": "pro"}],
            "json_remove": ["user.trial_ends"], "replace": [{"find": "Free", "with": "Pro"}]})
        self.assertEqual(rule["delay_s"], 1.5)
        self.assertEqual(mocks.load(self.registry)[0], rule)
        described = mocks.describe(rule)
        self.assertEqual(described["mode"], "rewrite")
        self.assertEqual(described["edits"], 4)
        self.assertEqual(described["edit_summary"],
                         "status 503 · -1 header · +1 header · json_set×1 · json_remove×1 · "
                         "replace×1")

    def test_update_edits_and_modes(self) -> None:
        rule = mocks.add(url_glob="*", mode="rewrite", json_set=["a=1"], replace=[["x", "y"]],
                         registry=self.registry)
        rid = rule["id"]
        kept = mocks.update(rid, status=500, registry=self.registry)
        self.assertEqual(kept["rewrite"]["json_set"], [{"path": "a", "value": 1}])
        replaced = mocks.update(rid, json_set=["b=2"], registry=self.registry)
        self.assertEqual(replaced["rewrite"], {"json_set": [{"path": "b", "value": 2}],
                                               "replace": [{"find": "x", "with": "y"}]})
        # Back to a fake response needs the edits gone first.
        self.assert_refused(mocks.update, rid, mode="respond", registry=self.registry,
                            flag="--rewrite")
        cleared = mocks.update(rid, clear_edits=True, registry=self.registry)
        self.assertNotIn("rewrite", cleared)
        kept_status = mocks.update(rid, clear_status=True, registry=self.registry)
        self.assertIsNone(kept_status["response"]["status"])
        fake = mocks.update(rid, mode="respond", registry=self.registry)
        self.assertNotIn("mode", fake)
        self.assertEqual(fake["response"]["status"], 200)  # a fake response needs one
        self.assert_refused(mocks.update, rid, clear_status=True, registry=self.registry)
        self.assert_refused(mocks.update, rid, clear_status=True, status=201, mode="rewrite",
                            registry=self.registry)

    def test_update_body_choice_is_one_or_the_other(self) -> None:
        rule = mocks.add(url_glob="*", mode="rewrite", body_text='{"a":1}',
                         registry=self.registry)
        rid = rule["id"]
        body_path = Path(rule["response"]["body_path"])
        self.assert_refused(mocks.update, rid, body_text="{}", json_set=["a=1"],
                            registry=self.registry)
        edits = mocks.update(rid, json_set=["a=2"], registry=self.registry)
        self.assertIsNone(edits["response"]["body_path"])
        self.assertFalse(body_path.exists())
        self.assertNotIn("Content-Type", edits["response"]["headers"])
        whole = mocks.update(rid, body_text="plain", registry=self.registry)
        self.assertNotIn("rewrite", whole)
        self.assertTrue(Path(whole["response"]["body_path"]).exists())
        dropped = mocks.update(rid, clear_body=True, registry=self.registry)
        self.assertIsNone(dropped["response"]["body_path"])
        self.assert_refused(mocks.update, rid, clear_body=True, body_text="x",
                            registry=self.registry)

    def test_remove_headers_is_kept_by_a_body_change(self) -> None:
        rule = mocks.add(url_glob="*", mode="rewrite", remove_headers=["ETag"],
                         json_set=["a=1"], registry=self.registry)
        whole = mocks.update(rule["id"], body_text="{}", registry=self.registry)
        self.assertEqual(whole["rewrite"], {"remove_headers": ["ETag"]})

    def test_round_trip_of_every_field_through_update(self) -> None:
        """MR-14-S02 at the library level: sending every stored value back changes nothing."""
        rule = mocks.add(url_glob="*/p", method="POST", mode="rewrite", status=503,
                         headers={"X-A": "1"}, remove_headers=["ETag"],
                         json_set=["user.plan=\"pro\"", "n=2", "auth.token=tk-secret"],
                         json_remove=["user.t"],
                         replace=[["Free", "Pro"]], delay_s=1.5,
                         query=["page=2", "token=abc"],
                         request_headers=["Authorization: Bearer abc", "X-Client"],
                         body_contains=["needle"], body_json=["a.b=true"],
                         note="n", registry=self.registry)
        masked = mocks.mask_rule(rule)
        self.assertNotIn("tk-secret", json.dumps(masked))
        self.assertNotIn("Bearer abc", json.dumps(masked))
        mocks.update(rule["id"], mode=masked.get("mode"), status=masked["response"]["status"],
                     headers=masked["response"]["headers"],
                     query=masked["match"]["query"], request_headers=masked["match"]["headers"],
                     body_contains=masked["match"]["body"]["contains"],
                     body_json=masked["match"]["body"]["json"],
                     **{key: masked["rewrite"][key] for key in mocks.EDIT_KEYS},
                     delay_s=masked["delay_s"], note=masked["note"], registry=self.registry)
        stored = mocks.load(self.registry)[0]
        stored.pop("updated_at")
        self.assertEqual(stored, rule)


if __name__ == "__main__":
    unittest.main()
