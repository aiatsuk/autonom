"""Mock request conditions: query, request headers, request body (MR-01..MR-05).

The addon helpers run without mitmproxy; the request hook runs on fake flows.
Registries are temporary, never the machine registry.
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors  # noqa: E402
from autonom_lib.network import mitm_addon, mocks, redact  # noqa: E402


class FakeRequest:
    def __init__(self, url: str, method: str = "GET", headers=None, body: bytes = b"",
                 encoding: str | None = None) -> None:
        self.method = method
        self.pretty_url = url
        rest = url.split("://", 1)[1]
        self.host = rest.split("/", 1)[0].split("?", 1)[0]
        self.pretty_host = self.host
        self.path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        self.timestamp_start = 1_700_000_000.0
        self.headers = list(headers or [])
        if encoding:
            self.headers.append(("Content-Encoding", encoding))
        self.encoding = encoding
        self.raw_content = gzip.compress(body) if encoding == "gzip" else body
        self.content_reads = 0

    def get_content(self, strict: bool = True) -> bytes:
        self.content_reads += 1
        if self.encoding in (None, "identity"):
            return self.raw_content
        if self.encoding == "gzip":
            return gzip.decompress(self.raw_content)
        if strict:
            raise ValueError("Invalid Content-Encoding")
        return self.raw_content


class FakeConn:
    peername = ("10.0.2.15", 5555)
    timestamp_end = None
    state = None


class FakeFlow:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request
        self.response = None
        self.error = None
        self.metadata: dict = {}
        self.client_conn = FakeConn()


def facts(url: str, headers=None, body: bytes = b"", encoding: str | None = None):
    return mitm_addon.RequestFacts.from_flow(
        FakeFlow(FakeRequest(url, headers=headers, body=body, encoding=encoding)))


def rule(match: dict, rule_id: str = "m_1", **extra) -> dict:
    return {"id": rule_id, "enabled": True, "match": {"url_glob": "*", **match},
            "response": {"status": 200, "headers": {}, "body_path": None}, **extra}


class RegistryBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "mocks"
        self.flows_dir = Path(self.tmp.name) / "s" / "network"
        self.flows_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def recorder(self) -> mitm_addon.AutonomRecorder:
        reader = mitm_addon.AutonomRecorder()
        reader.directory = str(self.flows_dir)
        reader.mocks_path = str(mocks.registry_file(self.registry))
        reader._make_response = lambda status, body, headers: {  # noqa: SLF001
            "status": status, "body": body, "headers": headers}
        return reader

    def assert_refused(self, fn, *args, flag: str | None = None, **kwargs):
        with self.assertRaises(errors.AutonomError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        if flag:
            self.assertIn(flag, caught.exception.message + " " + caught.exception.hint)
        return caught.exception


# --- MR-01 query ----------------------------------------------------------------


class QueryConditionTests(RegistryBase):
    def test_mr01_s01_different_answer_for_page_2(self) -> None:
        a = mocks.add(url_glob="https://api.test/items", ignore_query=True, status=500,
                      query=[mocks.parse_query_arg("page=2")], registry=self.registry)
        b = mocks.add(url_glob="https://api.test/items", ignore_query=True, status=200,
                      registry=self.registry)
        reader = self.recorder()
        first = FakeFlow(FakeRequest("https://api.test/items?page=2"))
        second = FakeFlow(FakeRequest("https://api.test/items?page=1"))
        reader.request(first)
        reader.request(second)
        self.assertEqual(first.response["status"], 500)
        self.assertEqual(first.metadata["autonom_mock_id"], a["id"])
        self.assertEqual(second.response["status"], 200)
        self.assertEqual(second.metadata["autonom_mock_id"], b["id"])

    def test_mr01_s02_presence_only(self) -> None:
        present = rule({"query": [{"name": "debug", "value": None}]})
        for url, expected in (("https://h/x?debug=", True), ("https://h/x?debug=1", True),
                              ("https://h/x?debug", True), ("https://h/x?other=1", False),
                              ("https://h/x", False)):
            with self.subTest(url=url):
                self.assertIs(mitm_addon.rule_matches(present, "GET", url, "h"), expected)

    def test_values_are_decoded_globs_and_names_exact(self) -> None:
        condition = rule({"query": [{"name": "q", "value": "hello w*"}]})
        self.assertTrue(mitm_addon.rule_matches(condition, "GET", "https://h/?q=hello+world", "h"))
        self.assertTrue(mitm_addon.rule_matches(condition, "GET", "https://h/?q=hello%20w", "h"))
        self.assertFalse(mitm_addon.rule_matches(condition, "GET", "https://h/?Q=hello+w", "h"))
        self.assertFalse(mitm_addon.rule_matches(condition, "GET", "https://h/?q=Hello+w", "h"))
        utf8 = rule({"query": [{"name": "city", "value": "Zürich"}]})
        self.assertTrue(mitm_addon.rule_matches(utf8, "GET", "https://h/?city=Z%C3%BCrich", "h"))

    def test_any_repeated_value_may_match(self) -> None:
        condition = rule({"query": [{"name": "tag", "value": "b"}]})
        self.assertTrue(mitm_addon.rule_matches(condition, "GET", "https://h/?tag=a&tag=b", "h"))

    def test_query_applies_to_match_glob_rules_too(self) -> None:
        condition = rule({"url_glob": "*/items*", "query": [{"name": "page", "value": "2"}]})
        self.assertTrue(mitm_addon.rule_matches(condition, "GET", "https://h/items?page=2", "h"))
        self.assertFalse(mitm_addon.rule_matches(condition, "GET", "https://h/items?page=3", "h"))

    def test_fragment_is_not_query(self) -> None:
        condition = rule({"query": [{"name": "a", "value": None}]})
        self.assertFalse(mitm_addon.rule_matches(condition, "GET", "https://h/x#a=1", "h"))


# --- MR-02 headers ----------------------------------------------------------------


class HeaderConditionTests(RegistryBase):
    def test_mr02_s01_match_by_client_header(self) -> None:
        mocks.add(url_glob="*", status=418,
                  request_headers=[mocks.parse_header_condition("X-Client: ios*")],
                  registry=self.registry)
        reader = self.recorder()
        flows = [FakeFlow(FakeRequest("https://h/a", headers=[("x-client", "ios-27")])),
                 FakeFlow(FakeRequest("https://h/a", headers=[("X-Client", "android")])),
                 FakeFlow(FakeRequest("https://h/a"))]
        for flow in flows:
            reader.request(flow)
        self.assertEqual([bool(flow.response) for flow in flows], [True, False, False])

    def test_presence_and_repeated_fields(self) -> None:
        present = rule({"headers": [{"name": "X-Debug", "value": None}]})
        self.assertTrue(mitm_addon.rule_matches(
            present, "GET", "https://h/", "h", facts("https://h/", [("x-debug", "")])))
        repeated = rule({"headers": [{"name": "Accept", "value": "text/*"}]})
        self.assertTrue(mitm_addon.rule_matches(
            repeated, "GET", "https://h/", "h",
            facts("https://h/", [("Accept", "application/json"), ("Accept", "text/html")])))

    def test_mitmproxy_style_headers_keep_repeated_fields_apart(self) -> None:
        class MultiHeaders:
            def items(self, multi: bool = False):
                pairs = [("Accept", "a/b"), ("Accept", "text/x")]
                return pairs if multi else [("Accept", "a/b, text/x")]

        exact = rule({"headers": [{"name": "accept", "value": "text/x"}]})
        request = mitm_addon.RequestFacts("https://h/", MultiHeaders())
        self.assertTrue(mitm_addon.rule_matches(exact, "GET", "https://h/", "h", request))

    def test_four_argument_call_cannot_see_headers(self) -> None:
        condition = rule({"headers": [{"name": "X-Client", "value": None}]})
        self.assertFalse(mitm_addon.rule_matches(condition, "GET", "https://h/", "h"))


# --- MR-03 body ---------------------------------------------------------------------


class BodyConditionTests(RegistryBase):
    def test_mr03_s01_json_field_types(self) -> None:
        mocks.add(url_glob="*", method="POST", status=201,
                  body_json=[mocks.parse_path_value("user.role=admin"),
                             mocks.parse_path_value("items.0.qty=2")],
                  registry=self.registry)
        reader = self.recorder()
        numeric = FakeFlow(FakeRequest(
            "https://h/order", "POST", body=b'{"user":{"role":"admin"},"items":[{"qty":2}]}'))
        textual = FakeFlow(FakeRequest(
            "https://h/order", "POST", body=b'{"user":{"role":"admin"},"items":[{"qty":"2"}]}'))
        reader.request(numeric)
        reader.request(textual)
        self.assertEqual(numeric.response["status"], 201)
        self.assertIsNone(textual.response)

    def test_mr03_s02_non_json_and_oversized_bodies_do_not_match(self) -> None:
        condition = rule({"body": {"json": [{"path": "a", "value": 1}]}}, rule_id="json")
        fallback = rule({}, rule_id="next")
        big = b'{"a":1,"pad":"' + b"x" * (2 * 1024 * 1024) + b'"}'
        for body in (b"a=1&b=2", big, b"\xff\xfe", b""):
            with self.subTest(size=len(body)):
                chosen = mitm_addon.select_rule(
                    [condition, fallback], "POST", "https://h/", "h",
                    facts("https://h/", body=body))
                self.assertEqual(chosen["id"], "next")
        self.assertEqual(mitm_addon.select_rule(
            [condition], "POST", "https://h/", "h", facts("https://h/", body=b'{"a":1}'))["id"],
            "json")

    def test_mr03_s03_gzip_request_body(self) -> None:
        condition = rule({"body": {"contains": ["needle"]}})
        self.assertTrue(mitm_addon.rule_matches(
            condition, "POST", "https://h/", "h",
            facts("https://h/", body=b"hay needle hay", encoding="gzip")))
        self.assertFalse(mitm_addon.rule_matches(
            condition, "POST", "https://h/", "h",
            facts("https://h/", body=b"hay needle hay", encoding="x-unknown")))

    def test_contains_needs_every_substring(self) -> None:
        condition = rule({"body": {"contains": ["alpha", "beta"]}})
        self.assertTrue(mitm_addon.rule_matches(
            condition, "POST", "https://h/", "h", facts("https://h/", body=b"beta alpha")))
        self.assertFalse(mitm_addon.rule_matches(
            condition, "POST", "https://h/", "h", facts("https://h/", body=b"alpha")))

    def test_json_value_semantics(self) -> None:
        cases = [
            ("a", "x*", {"a": "xyz"}, True),
            ("a", "X*", {"a": "xyz"}, False),          # case-sensitive glob
            ("a", True, {"a": 1}, False),              # a bool never equals a number
            ("a", 1, {"a": True}, False),
            ("a", 2, {"a": 2.0}, True),
            ("a", None, {"a": None}, True),
            ("a", None, {}, False),                    # missing is not null
            ("a.0.b", [1, 2], {"a": [{"b": [1, 2]}]}, True),
            ("a.1", 5, {"a": [5]}, False),             # index out of range
            ("a.x", 5, {"a": [5]}, False),             # not an index
            ("a", {"k": 1}, {"a": {"k": 1}}, True),
            ("0", 7, {"0": 7}, True),                  # an object key may be digits
        ]
        for path, wanted, document, expected in cases:
            with self.subTest(path=path, wanted=wanted, document=document):
                condition = rule({"body": {"json": [{"path": path, "value": wanted}]}})
                self.assertIs(mitm_addon.rule_matches(
                    condition, "POST", "https://h/", "h",
                    facts("https://h/", body=json.dumps(document).encode())), expected)

    def test_body_is_read_once_and_only_for_body_conditions(self) -> None:
        """RISK-001: no read without a body condition, at most one read per flow."""
        rules = [rule({"url_glob": "*/other"}, rule_id="a"), rule({}, rule_id="b")]
        request = FakeRequest("https://h/x", "POST", body=b'{"a":1}')
        chosen = mitm_addon.select_rule(rules, "POST", "https://h/x", "h",
                                        mitm_addon.RequestFacts.from_flow(FakeFlow(request)))
        self.assertEqual(chosen["id"], "b")
        self.assertEqual(request.content_reads, 0)
        body_rules = [rule({"body": {"contains": ["zzz"]}}, rule_id="c"),
                      rule({"body": {"json": [{"path": "a", "value": 2}]}}, rule_id="d"),
                      rule({"body": {"json": [{"path": "a", "value": 1}]}}, rule_id="e")]
        chosen = mitm_addon.select_rule(body_rules, "POST", "https://h/x", "h",
                                        mitm_addon.RequestFacts.from_flow(FakeFlow(request)))
        self.assertEqual(chosen["id"], "e")
        self.assertEqual(request.content_reads, 1)

    def test_a_raw_body_over_the_limit_is_not_decoded(self) -> None:
        request = FakeRequest("https://h/x", "POST", body=b"x" * (mitm_addon.MATCH_BODY_LIMIT + 1))
        condition = rule({"body": {"contains": ["x"]}})
        self.assertFalse(mitm_addon.rule_matches(
            condition, "POST", "https://h/x", "h",
            mitm_addon.RequestFacts.from_flow(FakeFlow(request))))
        self.assertEqual(request.content_reads, 0)


# --- MR-04 combination and compatibility -------------------------------------------


OLD_REGISTRY = {  # the shape Autonom main ccf98bb writes
    "version": 1, "next_id": 4,
    "mocks": [
        {"id": "m_1", "match": {"url_glob": "*/v1/login", "method": "POST", "host": None,
                                "ignore_query": False},
         "response": {"status": 500, "headers": {"Content-Type": "application/json"},
                      "body_path": None, "content_type_implicit": True},
         "enabled": True, "note": None, "created_at": "2026-10-01T00:00:00Z"},
        {"id": "m_2", "match": {"url_glob": "https://h/slow", "method": None, "host": "h",
                                "ignore_query": True},
         "response": {"status": 200, "headers": {}, "body_path": None},
         "enabled": True, "note": "slow", "created_at": "2026-10-01T00:00:00Z",
         "delay_s": 2.5},
        {"id": "m_3", "match": {"url_glob": "*/held", "method": None, "host": None,
                                "ignore_query": False},
         "response": {"status": 200, "headers": {}, "body_path": None},
         "enabled": True, "note": None, "created_at": "2026-10-01T00:00:00Z", "hang": True},
    ],
}


class CombinationTests(RegistryBase):
    def test_all_conditions_must_hold(self) -> None:
        condition = rule({"method": "POST", "query": [{"name": "v", "value": "2"}],
                          "headers": [{"name": "X-A", "value": "1"}],
                          "body": {"contains": ["ok"]}})
        good = facts("https://h/?v=2", [("X-A", "1")], b"ok")
        self.assertTrue(mitm_addon.rule_matches(condition, "POST", "https://h/?v=2", "h", good))
        for url, headers, body, method in (
                ("https://h/?v=3", [("X-A", "1")], b"ok", "POST"),
                ("https://h/?v=2", [("X-A", "2")], b"ok", "POST"),
                ("https://h/?v=2", [("X-A", "1")], b"no", "POST"),
                ("https://h/?v=2", [("X-A", "1")], b"ok", "GET")):
            with self.subTest(url=url, headers=headers, body=body, method=method):
                self.assertFalse(mitm_addon.rule_matches(
                    condition, method, url, "h", facts(url, headers, body)))

    def test_first_enabled_matching_rule_wins(self) -> None:
        rules = [rule({"query": [{"name": "p", "value": "1"}]}, rule_id="a", enabled=False),
                 rule({"query": [{"name": "p", "value": "1"}]}, rule_id="b"),
                 rule({}, rule_id="c")]
        self.assertEqual(mitm_addon.select_rule(rules, "GET", "https://h/?p=1", "h")["id"], "b")
        self.assertEqual(mitm_addon.select_rule(rules, "GET", "https://h/?p=2", "h")["id"], "c")

    def test_mr04_s01_old_registry_loads_and_matches_unchanged(self) -> None:
        self.registry.mkdir(parents=True)
        mocks.registry_file(self.registry).write_text(json.dumps(OLD_REGISTRY))
        reader = self.recorder()
        rules = reader._load_rules()  # noqa: SLF001
        self.assertEqual([item["id"] for item in rules], ["m_1", "m_2", "m_3"])
        self.assertEqual(mitm_addon.select_rule(
            rules, "POST", "https://a/v1/login", "a")["id"], "m_1")
        self.assertEqual(mitm_addon.select_rule(
            rules, "GET", "https://h/slow?ts=1", "h")["id"], "m_2")
        self.assertEqual(mitm_addon.select_rule(rules, "GET", "https://h/held", "h")["id"], "m_3")
        flow = FakeFlow(FakeRequest("https://a/v1/login", "POST", body=b"{}"))
        reader.request(flow)
        self.assertEqual(flow.response["status"], 500)
        self.assertEqual(flow.metadata["autonom_mock_kind"], "respond")
        # Reading through mocks.py gives the same rules back, untouched.
        self.assertEqual(mocks.load(self.registry), OLD_REGISTRY["mocks"])
        for item in mocks.load(self.registry):
            self.assertEqual(mocks.describe(item)["mode"], "respond")
            self.assertEqual(mocks.describe(item)["summary"], "")

    def test_a_rule_without_new_fields_stores_the_old_shape(self) -> None:
        added = mocks.add(url_glob="*", status=404, registry=self.registry)
        self.assertEqual(set(added), {"id", "match", "response", "enabled", "note",
                                      "created_at"})
        self.assertEqual(set(added["match"]), {"url_glob", "method", "host", "ignore_query"})
        self.assertEqual(added["response"]["status"], 404)
        default = mocks.add(url_glob="*", registry=self.registry)
        self.assertEqual(default["response"]["status"], 200)
        updated = mocks.update(added["id"], status=500, registry=self.registry)
        self.assertNotIn("mode", updated)
        self.assertNotIn("rewrite", updated)

    def test_session_fixtures_still_precede_machine_rules(self) -> None:
        directory = self.flows_dir
        (directory / "session-mocks").mkdir()
        (directory / "session-mocks" / "registry.json").write_text(json.dumps({"mocks": [
            rule({"query": [{"name": "page", "value": "2"}]}, rule_id="fixture")]}))
        mocks.add(url_glob="*", registry=self.registry)
        reader = self.recorder()
        flow = FakeFlow(FakeRequest("https://h/list?page=2"))
        reader.request(flow)
        self.assertEqual(flow.metadata["autonom_mock_id"], "fixture")
        other = FakeFlow(FakeRequest("https://h/list?page=1"))
        reader.request(other)
        self.assertEqual(other.metadata["autonom_mock_id"], "m_1")


# --- MR-05 validation and malformed rules ------------------------------------------


class ValidationTests(RegistryBase):
    def test_mr05_s01_bad_header_name_is_refused_and_nothing_stored(self) -> None:
        self.assert_refused(mocks.parse_header_condition, "Bad Name: x",
                            flag="--request-header")
        self.assert_refused(mocks.add, url_glob="*", request_headers=["Bad Name: x"],
                            registry=self.registry, flag="--request-header")
        self.assert_refused(mocks.add, url_glob="*",
                            request_headers=[{"name": "Bad Name", "value": "x"}],
                            registry=self.registry)
        self.assertEqual(mocks.load(self.registry), [])

    def test_limits_and_shapes(self) -> None:
        long = "x" * (mocks.MAX_CONDITION_VALUE + 1)
        cases = [
            ({"query": [{"name": "", "value": "1"}]}, "--query"),
            ({"query": ["=1"]}, "--query"),
            ({"query": [{"name": "a", "value": long}]}, "--query"),
            ({"query": [{"name": "a", "value": 2}]}, "--query"),
            ({"query": [f"q{i}=1" for i in range(21)]}, "--query"),
            ({"request_headers": [{"name": "", "value": None}]}, "--request-header"),
            ({"request_headers": ["X: " + long]}, "--request-header"),
            ({"body_contains": [""]}, "--body-contains"),
            ({"body_contains": [long]}, "--body-contains"),
            ({"body_contains": ["a"] * 21}, "--body-contains"),
            ({"body_json": ["user.role"]}, "--body-json"),
            ({"body_json": ["=1"]}, "--body-json"),
            ({"body_json": ["a..b=1"]}, "--body-json"),
            ({"body_json": ["a.=1"]}, "--body-json"),
            ({"body_json": [{"path": "a", "value": long}]}, "--body-json"),
            ({"body_json": [{"path": "a"}]}, "--body-json"),
            ({"body_json": [{"path": "a", "value": float("nan")}]}, "--body-json"),
            ({"query": "page=2"}, "--query"),
        ]
        for kwargs, flag in cases:
            with self.subTest(kwargs=str(kwargs)[:80]):
                self.assert_refused(mocks.add, url_glob="*", registry=self.registry,
                                    flag=flag, **kwargs)
        self.assertEqual(mocks.load(self.registry), [])

    def test_exactly_twenty_conditions_and_2048_characters_pass(self) -> None:
        value = "v" * mocks.MAX_CONDITION_VALUE
        added = mocks.add(url_glob="*", query=[f"q{i}={value}" for i in range(20)],
                          registry=self.registry)
        self.assertEqual(len(added["match"]["query"]), 20)

    def test_parsers(self) -> None:
        self.assertEqual(mocks.parse_query_arg("page=2"), {"name": "page", "value": "2"})
        self.assertEqual(mocks.parse_query_arg("debug"), {"name": "debug", "value": None})
        self.assertEqual(mocks.parse_query_arg("a=b=c"), {"name": "a", "value": "b=c"})
        self.assertEqual(mocks.parse_header_condition("X-Client: ios*"),
                         {"name": "X-Client", "value": "ios*"})
        self.assertEqual(mocks.parse_header_condition("X-Debug"),
                         {"name": "X-Debug", "value": None})
        self.assertEqual(mocks.parse_path_value("items.0.qty=2"),
                         {"path": "items.0.qty", "value": 2})
        self.assertEqual(mocks.parse_path_value("user.role=admin"),
                         {"path": "user.role", "value": "admin"})
        self.assertEqual(mocks.parse_path_value('a="2"'), {"path": "a", "value": "2"})
        self.assertEqual(mocks.parse_path_value("a=NaN"), {"path": "a", "value": "NaN"})
        self.assertEqual(mocks.parse_path_value("a={\"k\":[1]}"),
                         {"path": "a", "value": {"k": [1]}})
        self.assertEqual(mocks.parse_path_value("a="), {"path": "a", "value": ""})

    def test_malformed_hand_edited_conditions_never_match_and_never_raise(self) -> None:
        malformed = [
            {"query": "page=2"}, {"query": [None]}, {"query": [{"name": 5}]},
            {"query": [{"name": "a", "value": 3}]}, {"headers": {"X": "1"}},
            {"headers": [{"name": "", "value": None}]}, {"body": "needle"},
            {"body": {"contains": "needle"}}, {"body": {"contains": [5]}},
            {"body": {"json": [{"path": "", "value": 1}]}},
            {"body": {"json": [{"path": "a..b", "value": 1}]}},
            {"body": {"json": [{"value": 1}]}}, {"body": {"json": [{"path": "a"}]}},
            {"body": {"json": "a=1"}},
        ]
        request = facts("https://h/?page=2", [("X", "1")], b'{"a":1}')
        for match in malformed:
            with self.subTest(match=match):
                bad = rule(match, rule_id="bad")
                self.assertFalse(mitm_addon.rule_matches(bad, "GET", "https://h/?page=2", "h",
                                                         request))
                self.assertEqual(mitm_addon.select_rule(
                    [bad, rule({}, rule_id="ok")], "GET", "https://h/?page=2", "h",
                    request)["id"], "ok")
        for bad_rule in ({"id": "x", "match": "nope"}, {"id": "x", "mode": "other",
                                                         "match": {"url_glob": "*"}}, None, 5):
            with self.subTest(rule=bad_rule):
                self.assertFalse(mitm_addon.rule_matches(bad_rule, "GET", "https://h/", "h"))

    def test_the_request_hook_survives_a_malformed_registry_rule(self) -> None:
        self.registry.mkdir(parents=True)
        mocks.registry_file(self.registry).write_text(json.dumps({"mocks": [
            rule({"body": {"json": [{"path": None, "value": 1}]}}, rule_id="bad"),
            rule({"query": [{"name": "p"}]}, rule_id="presence")]}))
        flow = FakeFlow(FakeRequest("https://h/?p=1", "POST", body=b"{}"))
        self.recorder().request(flow)
        self.assertEqual(flow.metadata["autonom_mock_id"], "presence")


# --- update semantics, masking and descriptions ------------------------------------


class UpdateAndMaskTests(RegistryBase):
    def test_mr11_s01_add_then_narrow(self) -> None:
        added = mocks.add(url_glob="*/items*", status=500, query=["page=2"],
                          registry=self.registry)
        updated = mocks.update(added["id"], request_headers=["X-Client: ios*"],
                               registry=self.registry)
        shown = mocks.get(added["id"], self.registry, counts={})
        self.assertEqual(shown["match"]["query"], [{"name": "page", "value": "2"}])
        self.assertEqual(shown["match"]["headers"], [{"name": "X-Client", "value": "ios*"}])
        self.assertEqual(shown["response"]["status"], 500)
        self.assertEqual(updated["match"], shown["match"])

    def test_given_list_replaces_empty_list_clears_and_clear_conditions(self) -> None:
        added = mocks.add(url_glob="*", query=["a=1", "b=2"], body_contains=["x"],
                          body_json=["k=1"], registry=self.registry)
        replaced = mocks.update(added["id"], query=["c=3"], registry=self.registry)
        self.assertEqual(replaced["match"]["query"], [{"name": "c", "value": "3"}])
        self.assertEqual(replaced["match"]["body"], {"contains": ["x"],
                                                     "json": [{"path": "k", "value": 1}]})
        cleared = mocks.update(added["id"], body_contains=[], registry=self.registry)
        self.assertEqual(cleared["match"]["body"], {"json": [{"path": "k", "value": 1}]})
        untouched = mocks.update(added["id"], note="n", registry=self.registry)
        self.assertEqual(untouched["match"]["query"], [{"name": "c", "value": "3"}])
        wiped = mocks.update(added["id"], clear_conditions=True, registry=self.registry)
        for key in ("query", "headers", "body"):
            self.assertNotIn(key, wiped["match"])
        rebuilt = mocks.update(added["id"], clear_conditions=True, query=["z"],
                               registry=self.registry)
        self.assertEqual(rebuilt["match"]["query"], [{"name": "z", "value": None}])

    def test_refused_update_changes_nothing(self) -> None:
        added = mocks.add(url_glob="*", query=["a=1"], registry=self.registry)
        before = mocks.load(self.registry)
        self.assert_refused(mocks.update, added["id"], query=["=x"], registry=self.registry)
        self.assert_refused(mocks.update, added["id"], request_headers=["Bad Name"],
                            registry=self.registry)
        self.assertEqual(mocks.load(self.registry), before)

    def test_mr12_s01_secret_condition_values_are_masked(self) -> None:
        added = mocks.add(url_glob="*", status=401,
                          request_headers=["Authorization: Bearer abc123", "X-Client: ios"],
                          query=["access_token=tok999", "page=2"],
                          body_json=["user.password=hunter2", "user.role=admin"],
                          registry=self.registry)
        masked = mocks.mask_rule(added)
        blob = json.dumps(masked) + json.dumps(mocks.describe(added))
        for secret in ("abc123", "tok999", "hunter2"):
            self.assertNotIn(secret, blob)
        self.assertIn("ios", blob)
        self.assertIn("admin", blob)
        self.assertEqual(masked["match"]["headers"][0]["value"], redact.PLACEHOLDER)
        # The stored rule itself is not changed by masking.
        self.assertEqual(mocks.load(self.registry)[0]["match"]["headers"][0]["value"],
                         "Bearer abc123")

    def test_a_masked_value_sent_back_keeps_the_stored_secret(self) -> None:
        added = mocks.add(url_glob="*", request_headers=["Authorization: Bearer abc123"],
                          query=["token=t1", "page=2"], body_json=["a.secret=s"],
                          registry=self.registry)
        masked = mocks.mask_rule(added)
        mocks.update(added["id"], request_headers=masked["match"]["headers"],
                     query=masked["match"]["query"], body_json=masked["match"]["body"]["json"],
                     registry=self.registry)
        stored = mocks.load(self.registry)[0]
        self.assertEqual(stored["match"], added["match"])

    def test_mr12_sensitive_response_header_values_are_masked(self) -> None:
        rewrite = mocks.add(url_glob="*", mode="rewrite",
                            headers={"Set-Cookie": "session=abc123",
                                     "authorization": "Bearer tok999", "X-Debug": "on"},
                            registry=self.registry)
        respond = mocks.add(url_glob="*", status=200,
                            headers={"X-Api-Key": "key777", "Content-Type": "text/plain"},
                            registry=self.registry)
        for rule in (rewrite, respond):
            blob = json.dumps(mocks.mask_rule(rule)) + json.dumps(mocks.describe(rule))
            for secret in ("abc123", "tok999", "key777"):
                self.assertNotIn(secret, blob)
        masked = mocks.mask_rule(rewrite)["response"]["headers"]
        self.assertEqual(masked["Set-Cookie"], redact.PLACEHOLDER)
        self.assertEqual(masked["authorization"], redact.PLACEHOLDER)
        self.assertEqual(masked["X-Debug"], "on")
        self.assertEqual(mocks.mask_rule(respond)["response"]["headers"]["Content-Type"],
                         "text/plain")
        # Masking never changes the stored rule; odd hand-edited headers do not raise.
        self.assertEqual(mocks.load(self.registry)[0]["response"]["headers"]["Set-Cookie"],
                         "session=abc123")
        odd = mocks.mask_rule({"response": {"headers": {"Cookie": None, "X": 1}}})
        self.assertEqual(odd["response"]["headers"], {"Cookie": None, "X": 1})
        self.assertEqual(mocks.mask_rule({"response": {"headers": "junk"}})["response"],
                         {"headers": "junk"})

    def test_masked_response_headers_sent_back_keep_the_stored_values(self) -> None:
        added = mocks.add(url_glob="*", mode="rewrite",
                          headers={"Set-Cookie": "session=abc123", "X-Debug": "on"},
                          registry=self.registry)
        shown = mocks.mask_rule(added)["response"]["headers"]
        mocks.update(added["id"], headers=dict(shown), registry=self.registry)
        self.assertEqual(mocks.load(self.registry)[0]["response"]["headers"],
                         added["response"]["headers"])
        # Names compare without case; a changed value or a new header is saved as given.
        mocks.update(added["id"], headers={"set-cookie": redact.PLACEHOLDER, "X-Debug": "off",
                                           "Authorization": redact.PLACEHOLDER},
                     registry=self.registry)
        self.assertEqual(mocks.load(self.registry)[0]["response"]["headers"],
                         {"set-cookie": "session=abc123", "X-Debug": "off",
                          "Authorization": redact.PLACEHOLDER})

    def test_describe_summaries(self) -> None:
        plain = {"match": {"url_glob": "*"}}
        self.assertEqual(mocks.describe(plain),
                         {"mode": "respond", "conditions": 0, "edits": 0, "summary": "",
                          "edit_summary": ""})
        narrowed = mocks.add(url_glob="*", query=["page=2"], request_headers=["X-A: 1"],
                             registry=self.registry)
        self.assertEqual(mocks.describe(narrowed)["summary"], "page=2 · +1 header")
        many = mocks.add(url_glob="*", query=["a", "b=1", "c", "d"],
                         request_headers=["X-A", "X-B"], body_contains=["n"],
                         body_json=["k=1", "j=2"], registry=self.registry)
        self.assertEqual(mocks.describe(many)["summary"],
                         "a · b=1 · +2 query params · +2 headers · +1 body text · +2 body fields")
        self.assertEqual(mocks.describe(many)["conditions"], 9)
        self.assertEqual(mocks.condition_counts(many),
                         {"query": 4, "request_header": 2, "body_contains": 1, "body_json": 2})
        self.assertEqual(mocks.describe({"match": "junk", "mode": 5, "rewrite": []})["mode"],
                         "respond")


if __name__ == "__main__":
    unittest.main()
