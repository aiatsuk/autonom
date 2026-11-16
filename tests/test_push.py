"""Push notifications: the `autonom.push/v1` message, `simulator push compose`,
and `simulator push send` on the iOS Simulator and on an Android emulator.

Hermetic: `fake_simctl.py` keeps what each `simctl push` file held, and
`fake_adb.py` answers the push commands the way a live API 37 google_apis
emulator did (`id -u`, `su 0`, `pm path`, `query-receivers`, `am broadcast`,
`dumpsys notification --noredact`, `cmd notification post`). No device,
simulator or emulator is touched.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
SERIAL = "emulator-5554"
PKG = "dev.autonom.pushprobe"
BUNDLE = "com.example.knit"
SECRET = "SECRET-push-5b1e-do-not-log"
TRICKY = "It's $5 off \"today\" `now` \\ ok"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import android_push, errors, push_message, session, simulator  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

FULL = {"title": "Order shipped", "subtitle": "Today", "body": "Your order #42", "badge": 3,
        "sound": "default", "category": "ORDER", "thread": "orders", "channel": "orders",
        "image": "https://example.com/i.png", "data": {"order_id": "42", "count": 2,
                                                      "deep": {"a": [1, 2]}},
        "message_id": "m-1"}
DUMPSYS_PACKAGE = """Packages:
  Package [{pkg}] (4a1b2c):
    userId=10231
    requested permissions:
      android.permission.INTERNET
      android.permission.POST_NOTIFICATIONS
    install permissions:
      android.permission.INTERNET: granted=true
    User 0: ceDataInode=1 installed=true hidden=false
      runtime permissions:
        android.permission.POST_NOTIFICATIONS: granted={granted}, flags=[ USER_SET ]
"""


def invalid(test: unittest.TestCase, call, *args, code: str = errors.INVALID_VALUE,
            **kwargs) -> errors.AutonomError:
    with test.assertRaises(errors.AutonomError) as caught:
        call(*args, **kwargs)
    test.assertEqual(caught.exception.code, code, caught.exception.as_dict())
    test.assertTrue(caught.exception.message)
    test.assertTrue(caught.exception.hint, caught.exception.as_dict())
    return caught.exception


# --- the message ------------------------------------------------------------------------


class MessageTests(unittest.TestCase):
    def test_every_field_maps_on_ios(self) -> None:
        message = push_message.normalize(FULL)
        payload = push_message.render_ios(message, "m-1")
        self.assertEqual(payload["aps"], {
            "alert": {"title": "Order shipped", "subtitle": "Today", "body": "Your order #42"},
            "badge": 3, "sound": "default", "category": "ORDER", "thread-id": "orders",
            "mutable-content": 1})
        self.assertEqual((payload["order_id"], payload["count"], payload["deep"]),
                         ("42", 2, {"a": [1, 2]}))
        self.assertEqual(payload["fcm_options"], {"image": "https://example.com/i.png"})
        self.assertEqual(payload["gcm.message_id"], "m-1")
        self.assertEqual(list(payload)[0], "aps")
        self.assertNotIn("orders", json.dumps({k: v for k, v in payload.items()
                                               if k != "aps"}))  # channel is Android only

    def test_every_field_maps_on_android(self) -> None:
        message = push_message.normalize(FULL)
        extras = push_message.render_android(message, "m-1", 1763240000000)
        self.assertEqual(extras, {
            "gcm.n.e": "1", "gcm.notification.title": "Order shipped",
            "gcm.notification.body": "Your order #42",
            "gcm.notification.notification_count": "3", "gcm.notification.sound": "default",
            "gcm.notification.click_action": "ORDER",
            "gcm.notification.android_channel_id": "orders",
            "gcm.notification.image": "https://example.com/i.png",
            "order_id": "42", "count": "2", "deep": '{"a":[1,2]}',
            "google.message_id": "m-1", "google.sent_time": "1763240000000",
            "from": "autonom", "google.ttl": "3600"})
        self.assertTrue(all(isinstance(value, str) for value in extras.values()))
        self.assertEqual(push_message.android_kind(message), "notification")

    def test_notes_name_what_each_platform_leaves_out(self) -> None:
        notes = push_message.notes(push_message.normalize(FULL))
        self.assertIn("ios: channel is not used (Android only)", notes)
        self.assertIn("android: subtitle is not used (iOS only)", notes)
        self.assertIn("android: thread is not used (iOS only)", notes)
        self.assertIn("android: data.count is sent as JSON text (extras are strings)", notes)
        self.assertIn("android: data.deep is sent as JSON text (extras are strings)", notes)
        self.assertNotIn("android: data.order_id is sent as JSON text (extras are strings)", notes)
        self.assertTrue(all(note.startswith("ios: ")
                            for note in push_message.notes(push_message.normalize(FULL), "ios")))

    def test_a_body_alone_is_a_plain_alert_and_no_title_no_body_is_data_only(self) -> None:
        body = push_message.normalize({"body": "Hi"})
        self.assertEqual(push_message.render_ios(body, "x")["aps"], {"alert": "Hi"})
        data = push_message.normalize({"data": {"k": "v"}, "badge": 1})
        self.assertEqual(push_message.android_kind(data), "data")
        extras = push_message.render_android(data, "x")
        self.assertNotIn("gcm.n.e", extras)
        self.assertEqual(extras["k"], "v")
        self.assertIn("android: badge show nothing in a data-only message (no title or body)",
                      push_message.notes(data, "android"))
        self.assertNotIn("alert", push_message.render_ios(data, "x")["aps"])

    def test_silent(self) -> None:
        silent = push_message.normalize({"silent": True, "data": {"sync": "1"}})
        payload = push_message.render_ios(silent, "x")
        self.assertEqual(payload["aps"], {"content-available": 1})
        self.assertEqual(push_message.render_android(silent, "x")["sync"], "1")
        self.assertEqual(push_message.android_kind(silent), "data")
        self.assertEqual(push_message.normalize({"silent": True})["silent"], True)
        for field in ("title", "subtitle", "body"):
            with self.subTest(field=field):
                error = invalid(self, push_message.normalize, {"silent": True, field: "x"})
                self.assertEqual(error.extra["field"], "silent")

    def test_fcm_false_drops_the_firebase_id_on_ios_only(self) -> None:
        message = push_message.normalize({"title": "t", "fcm": False})
        self.assertNotIn("gcm.message_id", push_message.render_ios(message, "x"))
        self.assertEqual(push_message.render_android(message, "x")["google.message_id"], "x")

    def test_escape_hatches_merge_over_the_render(self) -> None:
        message = push_message.normalize({
            "title": "t", "ios": {"aps": {"interruption-level": "time-sensitive"}, "x": 1},
            "android": {"gcm.notification.tag": "one", "from": "other"}})
        payload = push_message.render_ios(message, "x")
        self.assertEqual(payload["aps"]["alert"], {"title": "t"})
        self.assertEqual(payload["aps"]["interruption-level"], "time-sensitive")
        self.assertEqual(payload["x"], 1)
        extras = push_message.render_android(message, "x")
        self.assertEqual((extras["gcm.notification.tag"], extras["from"]), ("one", "other"))
        invalid(self, push_message.normalize, {"title": "t", "android": {"k": 1}})
        invalid(self, push_message.normalize, {"title": "t", "ios": "aps"})

    def test_reserved_data_keys_are_refused_on_both_platforms(self) -> None:
        for key in ("aps", "from", "message_type", "collapse_key", "google.x", "gcm.n.e"):
            with self.subTest(key=key):
                error = invalid(self, push_message.normalize, {"data": {key: "v"}})
                self.assertEqual(error.extra["field"], f"data.{key}")
        self.assertEqual(push_message.normalize({"data": {"googlex": "v"}})["data"],
                         {"googlex": "v"})

    def test_an_unknown_field_is_named_with_the_valid_ones(self) -> None:
        error = invalid(self, push_message.normalize, {"title": "t", "tittle": "x"})
        self.assertEqual(error.extra["field"], "tittle")
        self.assertIn("title", error.hint)
        self.assertEqual(error.extra["valid_fields"], list(push_message.FIELDS))

    def test_a_message_needs_something_to_say(self) -> None:
        for raw in ({}, {"subtitle": "s"}, {"badge": 1}, {"data": {}}, {"silent": False}):
            with self.subTest(raw=raw):
                invalid(self, push_message.normalize, raw)
        for raw, field in (({"title": ""}, "title"), ({"title": "  "}, "title"),
                           ({"title": 5}, "title"), ({"title": "a\x00b"}, "title"),
                           ({"badge": -1}, "badge"), ({"badge": True}, "badge"),
                           ({"badge": 1.5}, "badge"), ({"data": [1]}, "data"),
                           ({"schema": "autonom.push/v2", "title": "t"}, "schema")):
            with self.subTest(raw=raw):
                error = invalid(self, push_message.normalize, raw)
                self.assertEqual(error.extra.get("field"), field)

    def test_the_text_never_appears_in_an_error(self) -> None:
        cases = [{"title": SECRET, "silent": True}, {"title": SECRET, "unknown": 1},
                 {"body": SECRET, "badge": "lots"}, {"data": {"aps": SECRET}}]
        for raw in cases:
            with self.subTest(raw=list(raw)):
                with self.assertRaises(errors.AutonomError) as caught:
                    push_message.from_values(raw)
                self.assertNotIn(SECRET, json.dumps(caught.exception.as_dict()))

    def test_message_ids(self) -> None:
        generated = push_message.new_message_id()
        self.assertRegex(generated, r"^autonom-\d{13}-[0-9a-f]{6}$")
        self.assertNotEqual(generated, push_message.new_message_id())
        message = push_message.normalize({"title": "t"})
        self.assertNotIn("message_id", message)
        self.assertRegex(push_message.message_id_of(message), r"^autonom-")
        self.assertEqual(push_message.message_id_of({**message, "message_id": "own"}), "own")

    def test_ios_byte_count_and_limit(self) -> None:
        message = push_message.normalize({"body": "é" * 10})
        payload = push_message.render_ios(message, "id")
        self.assertEqual(push_message.payload_bytes(payload),
                         len(json.dumps(payload, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8")))
        compose = push_message.compose({"body": "x" * 4100, "platform": "ios"})
        self.assertTrue(compose["ios"]["over_limit"])
        self.assertGreater(compose["ios"]["bytes"], push_message.IOS_PAYLOAD_LIMIT)
        self.assertEqual([item["code"] for item in compose["warnings"]], ["push_payload_large"])
        self.assertEqual(compose["warnings"][0]["platform"], "ios")
        small = push_message.compose({"body": "x"})
        self.assertFalse(small["ios"]["over_limit"])
        self.assertEqual(small["warnings"], [])


class ValuesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, content) -> str:
        path = self.dir / name
        path.write_text(content if isinstance(content, str) else json.dumps(content),
                        encoding="utf-8")
        return str(path)

    def test_value_text_is_parsed(self) -> None:
        push = push_message.from_values({"title": "t", "badge": " 7 ", "silent": "no",
                                         "fcm": "off", "app_id": "a"})
        self.assertEqual((push.message["badge"], push.message["silent"], push.message["fcm"]),
                         (7, False, False))
        self.assertEqual(push.options, {"app_id": "a"})
        for key, text in (("badge", "seven"), ("badge", "-1"), ("silent", "maybe"),
                          ("fcm", "2"), ("data", "[1]"), ("data", "{nope"), ("ios", "1")):
            with self.subTest(key=key, text=text):
                error = invalid(self, push_message.from_values, {"title": "t", key: text})
                self.assertEqual(error.extra["field"], key)

    def test_data_keys_and_the_data_base(self) -> None:
        push = push_message.from_values({"data": '{"a": 1, "b": "x"}', "data.b": "y",
                                         "data.url": "app://o/42"})
        self.assertEqual(push.message["data"], {"a": 1, "b": "y", "url": "app://o/42"})
        typed = push_message.from_values({"data.n": 3, "title": "t"})  # from --json
        self.assertEqual(typed.message["data"], {"n": 3})
        invalid(self, push_message.from_values, {"data.": "x"})
        invalid(self, push_message.from_values, {"data.aps": "x"})

    def test_unknown_values_are_refused_outside_the_raw_payload(self) -> None:
        error = invalid(self, push_message.from_values, {"title": "t", "colour": "red"})
        self.assertEqual(error.extra["field"], "colour")
        # the original payload path ignores them, as it always did
        push = push_message.from_values({"payload": {"aps": {}}, "colour": "red"})
        self.assertTrue(push.raw)

    def test_payload_and_message_conflict(self) -> None:
        for extra in ({"title": "t"}, {"data.k": "v"},
                      {"file": self.write("m.json", {"title": "t"})}):
            with self.subTest(extra=list(extra)):
                error = invalid(self, push_message.from_values,
                                {"payload": {"aps": {"alert": "x"}}, **extra})
                self.assertEqual(error.extra["field"], "payload")

    def test_payload_text_is_parsed_like_before(self) -> None:
        self.assertEqual(push_message.from_values({"payload": '{"aps": {}}'}).payload,
                         {"aps": {}})
        self.assertEqual(push_message.from_values({"payload": "not json"}).payload, "not json")

    def test_a_message_file_with_fields_laid_over_it(self) -> None:
        path = self.write("order.json", {"schema": push_message.SCHEMA, "name": "order",
                                         "title": "Order shipped", "badge": 1,
                                         "data": {"order_id": "1", "kept": "yes"}})
        push = push_message.from_values({"file": path, "badge": "5", "data.order_id": "42",
                                         "data": '{"extra": true}'})
        self.assertEqual(push.message["title"], "Order shipped")
        self.assertEqual(push.message["badge"], 5)
        self.assertEqual(push.message["data"], {"order_id": "42", "kept": "yes", "extra": True})
        self.assertEqual(push.message["name"], "order")
        self.assertEqual(push.file, path)

    def test_a_raw_aps_file_is_a_payload(self) -> None:
        path = self.write("raw.json", {"aps": {"alert": "x"}, "k": 1})
        push = push_message.from_values({"file": path, "app_id": "a"})
        self.assertTrue(push.raw)
        self.assertEqual(push.payload, {"aps": {"alert": "x"}, "k": 1})
        error = invalid(self, push_message.from_values, {"file": path, "title": "t"})
        self.assertEqual(error.extra["path"], path)

    def test_bad_files_name_the_path(self) -> None:
        cases = {"missing": str(self.dir / "nope.json"),
                 "not json": self.write("bad.json", "{nope"),
                 "array": self.write("list.json", [1, 2]),
                 "directory": str(self.dir),
                 "empty object": self.write("empty.json", {}),
                 "bad field": self.write("field.json", {"title": "t", "tittle": "x"})}
        for label, path in cases.items():
            with self.subTest(label=label):
                error = invalid(self, push_message.from_values, {"file": path})
                if label not in ("empty object", "bad field"):
                    self.assertIn(path, error.message)
                    self.assertEqual(error.extra["path"], path)
        for path in ("", "~no-such-user-autonom/x.json", "a\x00b.json", 3):
            with self.subTest(path=path):
                invalid(self, push_message.from_values, {"file": path})
                invalid(self, push_message.compose, {"title": "t", "write": path})


class SchemaTests(unittest.TestCase):
    """`schemas/push-v1.schema.json` and the validator agree."""

    def setUp(self) -> None:
        self.schema = json.loads((ROOT / "schemas/push-v1.schema.json").read_text("utf-8"))
        self.props = self.schema["properties"]

    def test_field_names_and_schema_id(self) -> None:
        self.assertEqual(list(self.props), list(push_message.FIELDS))
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertEqual(self.props["schema"]["const"], push_message.SCHEMA)
        self.assertNotIn("required", self.schema)

    @staticmethod
    def matches(pattern: str, text: str) -> bool:
        """A JSON Schema `pattern` (ECMA-262: unanchored search, `$` only at
        the very end) checked with Python's `re`."""
        return re.search(pattern.replace("$", r"\Z"), text) is not None

    def accepts(self, raw: dict) -> bool:
        try:
            push_message.normalize(raw)
        except errors.AutonomError as exc:
            self.assertEqual(exc.code, errors.INVALID_VALUE)
            return False
        return True

    def test_types_match_the_validator(self) -> None:
        samples = ("a", " a ", "two words", "", "  ", "\t\n", "a\x00b", "\x00", "é", "line\nbreak",
                   "ab\n")
        for name in push_message.TEXT_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(self.props[name]["type"], "string")
                pattern = self.props[name]["pattern"]
                for text in samples:
                    raw = {"title": "t", name: text} if name != "title" else {"title": text}
                    self.assertEqual(self.matches(pattern, text), self.accepts(raw),
                                     f"{name}={text!r}")
        for bad in ("my id", " lead", "tail "):
            invalid(self, push_message.normalize, {"title": "t", "message_id": bad})
        for name in push_message.LABEL_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(self.props[name]["type"], "string")
                for text in samples:
                    self.assertEqual(self.matches(self.props[name]["pattern"], text),
                                     self.accepts({"title": "t", name: text}), f"{name}={text!r}")
        for name in push_message.BOOL_FIELDS:
            self.assertEqual(self.props[name]["type"], "boolean")
        self.assertEqual(self.props["fcm"]["default"], True)
        self.assertEqual((self.props["badge"]["type"], self.props["badge"]["minimum"]),
                         ("integer", 0))
        for name in ("data", "ios", "android"):
            self.assertEqual(self.props[name]["type"], "object")
        self.assertEqual(self.props["android"]["additionalProperties"]["type"], "string")

    def test_json_types_are_not_text_in_a_file_or_json(self) -> None:
        # the schema says integer and boolean; "3" and "true" are refused by both
        for raw, field in (({"title": "t", "badge": "3"}, "badge"),
                           ({"title": "t", "badge": "0"}, "badge"),
                           ({"title": "t", "silent": "false"}, "silent"),
                           ({"silent": "true"}, "silent"),
                           ({"title": "t", "fcm": "false"}, "fcm"),
                           ({"title": "t", "fcm": 0}, "fcm")):
            with self.subTest(raw=raw):
                self.assertEqual(invalid(self, push_message.normalize, raw).extra["field"], field)
        # an integer with a zero fraction is an integer in JSON Schema
        self.assertEqual(push_message.normalize({"title": "t", "badge": 3.0})["badge"], 3)
        invalid(self, push_message.normalize, {"title": "t", "badge": 2.5})

    def test_nul_is_refused_in_every_string_by_both(self) -> None:
        any_text = r"^[^\u0000]*$"
        names = self.props["data"]["propertyNames"]
        self.assertEqual(names["pattern"], any_text)
        android = self.props["android"]
        self.assertEqual(android["propertyNames"]["pattern"], any_text)
        self.assertEqual(android["additionalProperties"]["pattern"], any_text)
        self.assertEqual(self.props["ios"]["propertyNames"]["pattern"], any_text)
        for ref in (self.props["data"]["additionalProperties"],
                    self.props["ios"]["additionalProperties"]):
            self.assertEqual(ref, {"$ref": "#/$defs/value"})
        value = self.schema["$defs"]["value"]["anyOf"]
        self.assertEqual(value[1], {"type": "string", "pattern": any_text})
        self.assertEqual(value[2]["items"], {"$ref": "#/$defs/value"})
        self.assertEqual(value[3]["propertyNames"], {"pattern": any_text})
        self.assertEqual(value[3]["additionalProperties"], {"$ref": "#/$defs/value"})
        for raw, field in (({"title": "t", "name": "a\x00"}, "name"),
                           ({"title": "t", "description": "\x00"}, "description"),
                           ({"data": {"k": "a\x00"}}, "data.k"),
                           ({"data": {"k": {"deep": ["x", "a\x00"]}}}, "data.k"),
                           ({"data": {"k": {"a\x00": 1}}}, "data.k"),
                           ({"data": {"a\x00": "v"}}, "data"),
                           ({"title": "t", "ios": {"aps": {"sound": "a\x00"}}}, "ios"),
                           ({"title": "t", "ios": {"a\x00": 1}}, "ios"),
                           ({"title": "t", "android": {"k": "a\x00"}}, "android"),
                           ({"title": "t", "android": {"a\x00": "v"}}, "android")):
            with self.subTest(raw=repr(raw)):
                error = invalid(self, push_message.normalize, raw)
                self.assertEqual(error.extra["field"], field)
                self.assertNotIn("\x00", json.dumps(error.as_dict(), ensure_ascii=False))

    def test_an_empty_key_in_data_or_android_is_refused_by_both(self) -> None:
        self.assertEqual(self.props["data"]["propertyNames"]["minLength"], 1)
        self.assertEqual(self.props["android"]["propertyNames"]["minLength"], 1)
        for raw, field in (({"data": {"": "v"}}, "data"),
                           ({"title": "t", "android": {"": "v"}}, "android")):
            with self.subTest(raw=raw):
                self.assertEqual(invalid(self, push_message.normalize, raw).extra["field"], field)

    def test_reserved_data_keys_match(self) -> None:
        names = self.props["data"]["propertyNames"]
        self.assertEqual(names["minLength"], 1)
        refused = names["not"]["anyOf"]
        self.assertEqual(refused[0]["enum"], list(push_message.RESERVED_DATA_KEYS))
        prefixes = "|".join(prefix.rstrip(".") for prefix in push_message.RESERVED_DATA_PREFIXES)
        self.assertEqual(refused[1]["pattern"], rf"^({prefixes})\.")

    def test_the_content_rules_match(self) -> None:
        any_of = self.schema["anyOf"]
        self.assertEqual([item["required"] for item in any_of],
                         [["title"], ["body"], ["data"], ["silent"]])
        self.assertEqual(any_of[2]["properties"]["data"]["minProperties"], 1)
        self.assertEqual(any_of[3]["properties"]["silent"]["const"], True)
        rule = self.schema["if"], self.schema["then"]
        self.assertEqual(rule[0]["properties"]["silent"]["const"], True)
        self.assertEqual([item["required"] for item in rule[1]["not"]["anyOf"]],
                         [["title"], ["subtitle"], ["body"]])

    def test_a_full_message_passes_both(self) -> None:
        message = {"schema": push_message.SCHEMA, "name": "n", "description": "d", **FULL,
                   "fcm": True, "ios": {"aps": {}}, "android": {"k": "v"}}
        # silent cannot sit beside a title: it is checked on its own
        self.assertEqual(set(message) | {"silent"}, set(self.props))
        self.assertEqual(push_message.normalize(message), message)
        quiet = {"schema": push_message.SCHEMA, "silent": True, "data": {"k": "v"}}
        self.assertEqual(push_message.normalize(quiet), quiet)


# --- compose ----------------------------------------------------------------------------


class CliBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state), AUTONOM_FAKE_LOG=str(self.log),
                     AUTONOM_HOME=str(self.root / "home"), AUTONOM_ADB=None,
                     AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
                     AUTONOM_SIMDEVICE_TMP_ROOT=str(self.root / "var-tmp"))
        self.env = dict(os.environ)

    def fake(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def patch_fake(self, **kwargs) -> None:
        self.state.write_text(json.dumps({**self.fake(), **kwargs}), encoding="utf-8")

    def calls(self, tool: str = "adb") -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in self.log.read_text("utf-8").splitlines()
                if json.loads(line)["tool"] == tool]

    def run_cli(self, *argv: str, env: dict | None = None) -> tuple[int, dict, str]:
        completed = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True,
                                   text=True, env=env or self.env, timeout=120,
                                   cwd=self.tmp.name)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream), completed.stdout + completed.stderr

    def android_cli(self, *argv: str) -> tuple[int, dict, str]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv)

    def ios_target(self) -> Target:
        return Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    def android_target(self, serial: str = SERIAL) -> Target:
        return Target(ANDROID, serial, str(FAKE_ADB), {"serial": serial})


class ComposeCliTests(CliBase):
    def test_compose_needs_no_device_tool_at_all(self) -> None:
        env = {**self.env, "PATH": str(self.root / "empty-bin")}
        code, payload, _ = self.run_cli(
            "--adb", "/nonexistent/adb", "--simctl", "/nonexistent/xcrun", "--serial", SERIAL,
            "simulator", "push", "compose", "--value", "title=Hello", "--value", "body=There",
            "--value", "data.order_id=42", "--json", '{"badge": 2}', env=env)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["message"], {"schema": push_message.SCHEMA, "title": "Hello",
                                              "body": "There", "badge": 2,
                                              "data": {"order_id": "42"}})
        self.assertEqual(payload["ios"]["payload"]["aps"]["alert"],
                         {"title": "Hello", "body": "There"})
        self.assertFalse(payload["ios"]["over_limit"])
        self.assertEqual(payload["android"]["kind"], "notification")
        self.assertEqual(payload["android"]["extras"]["order_id"], "42")
        self.assertEqual(payload["ios"]["payload"]["gcm.message_id"],
                         payload["android"]["extras"]["google.message_id"])
        self.assertEqual((payload["notes"], payload["warnings"], payload["written"],
                          payload["next"]), ([], [], None, None))
        self.assertNotIn("target_id", payload)
        self.assertEqual(self.calls("adb") + self.calls("simctl"), [])

    def test_platform_limits_the_reply(self) -> None:
        code, payload, _ = self.run_cli("simulator", "push", "compose", "--value", "title=t",
                                        "--value", "channel=c", "--value", "platform=android")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("ios", payload)
        self.assertEqual(payload["notes"], [])
        code, payload, _ = self.run_cli("simulator", "push", "compose", "--value", "title=t",
                                        "--value", "channel=c", "--value", "platform=ios")
        self.assertNotIn("android", payload)
        self.assertEqual(payload["notes"], ["ios: channel is not used (Android only)"])
        code, payload, _ = self.run_cli("simulator", "push", "compose", "--value", "title=t",
                                        "--value", "platform=web")
        self.assertEqual((code, payload["error_code"]), (2, errors.INVALID_VALUE))

    def test_write_saves_the_message_and_next_sends_it(self) -> None:
        out = self.root / ".autonom" / "pushes" / "order shipped.json"
        code, payload, _ = self.run_cli(
            "simulator", "push", "compose", "--json",
            json.dumps({"name": "order", "title": "Order shipped", "data": {"order_id": 42}}),
            "--value", f"write={out}")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["written"], str(out))
        self.assertEqual(payload["next"],
                         f"autonom simulator push send --value 'file={out}'")
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text), {"schema": push_message.SCHEMA, "name": "order",
                                            "title": "Order shipped",
                                            "data": {"order_id": 42}})
        self.assertIn('\n  "title": "Order shipped",\n', text)
        self.assertNotIn("message_id", text)  # generated for the preview only
        # the saved file composes again, with a field laid over it
        code, again, _ = self.run_cli("simulator", "push", "compose", "--value", f"file={out}",
                                      "--value", "body=On its way")
        self.assertEqual(code, 0, again)
        self.assertEqual(again["message"]["body"], "On its way")

    def test_an_existing_file_is_kept_unless_overwrite(self) -> None:
        out = self.root / "p.json"
        out.write_text("keep\n", encoding="utf-8")
        code, payload, _ = self.run_cli("simulator", "push", "compose", "--value", "title=t",
                                        "--value", f"write={out}")
        self.assertEqual((code, payload["error_code"]), (2, errors.INVALID_VALUE))
        self.assertIn("overwrite=true", payload["hint"])
        self.assertEqual(out.read_text(encoding="utf-8"), "keep\n")
        code, payload, _ = self.run_cli("simulator", "push", "compose", "--value", "title=t",
                                        "--value", "message_id=own", "--value", f"write={out}",
                                        "--value", "overwrite=true")
        self.assertEqual(code, 0, payload)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["message_id"], "own")
        code, payload, _ = self.run_cli("simulator", "push", "compose", "--value", "title=t",
                                        "--value", f"write={self.root}")
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)

    def test_compose_refusals(self) -> None:
        for values, field in ((["--value", "app_id=a", "--value", "title=t"], "app_id"),
                              (["--value", "delivery=shade", "--value", "title=t"], "delivery"),
                              (["--json", '{"payload": {"aps": {}}}'], "payload"),
                              ([], None)):
            with self.subTest(values=values):
                code, payload, _ = self.run_cli("simulator", "push", "compose", *values)
                self.assertEqual((code, payload["error_code"]), (2, errors.INVALID_VALUE))
                self.assertTrue(payload["hint"])
                if field:
                    self.assertEqual(payload["field"], field)


# --- iOS send ---------------------------------------------------------------------------


class IosSendTests(CliBase):
    def pushes(self) -> list[dict]:
        return self.fake().get("pushes", [])

    def test_a_raw_payload_is_sent_byte_for_byte_as_before(self) -> None:
        payload = {"aps": {"alert": "Hi é"}, "k": [1, {"x": None}]}
        result = simulator.apply(self.ios_target(), "push", "send",
                                 {"app_id": BUNDLE, "payload": payload}, session={})
        self.assertEqual(result, {"control": "push", "app_id": BUNDLE, "app_id_source": "value",
                                  "verified": False, "verification": "unavailable"})
        (sent,) = self.pushes()
        self.assertEqual(sent["text"], json.dumps(payload, ensure_ascii=False,
                                                  separators=(",", ":")))
        argv = self.calls("simctl")[-1]
        self.assertEqual(argv[:4], ["simctl", "push", UDID, BUNDLE])
        self.assertTrue(argv[4].endswith(".apns"))
        self.assertEqual(len(argv), 5)

    def test_a_raw_aps_file_is_the_payload(self) -> None:
        path = self.root / "raw.json"
        path.write_text(json.dumps({"aps": {"alert": "From a file"}}), encoding="utf-8")
        result = simulator.apply(self.ios_target(), "push", "send",
                                 {"app_id": BUNDLE, "file": str(path)}, session={})
        self.assertNotIn("composed", result)
        self.assertEqual(self.pushes()[0]["payload"], {"aps": {"alert": "From a file"}})

    def test_a_composed_push_is_rendered_into_the_file_simctl_reads(self) -> None:
        values = {"title": TRICKY, "body": "b", "badge": "4", "data.order_id": "42",
                  "message_id": "m-9"}
        result = simulator.apply(self.ios_target(), "push", "send", values,
                                 session={"target_id": UDID, "app_id": BUNDLE})
        self.assertEqual((result["app_id"], result["app_id_source"]), (BUNDLE, "session"))
        self.assertEqual((result["composed"], result["message_id"], result["kind"]),
                         (True, "m-9", "notification"))
        self.assertEqual((result["verified"], result["verification"]), (False, "unavailable"))
        (sent,) = self.pushes()
        self.assertEqual(sent["payload"], {"aps": {"alert": {"title": TRICKY, "body": "b"},
                                                   "badge": 4},
                                           "order_id": "42", "gcm.message_id": "m-9"})
        self.assertEqual(result["payload_bytes"], len(sent["text"].encode("utf-8")))
        self.assertNotIn("warnings", result)
        self.assertEqual(list(result)[:3], ["control", "app_id", "app_id_source"])

    def test_a_large_composed_payload_is_sent_with_a_warning(self) -> None:
        result = simulator.apply(self.ios_target(), "push", "send",
                                 {"app_id": BUNDLE, "body": "x" * 5000}, session={})
        self.assertEqual([item["code"] for item in result["warnings"]], ["push_payload_large"])
        self.assertEqual(len(self.pushes()), 1)  # simctl decides

    def test_the_raw_path_still_ignores_values_it_does_not_use(self) -> None:
        result = simulator.apply(self.ios_target(), "push", "send",
                                 {"app_id": BUNDLE, "payload": {"aps": {}}, "receiver": ".R",
                                  "platform": "web", "colour": "red"}, session={})
        self.assertEqual(result["verification"], "unavailable")
        self.assertEqual(len(self.pushes()), 1)

    def test_ios_refusals_send_nothing(self) -> None:
        cases = [({"app_id": BUNDLE, "title": "t", "delivery": "shade"}, "delivery"),
                 ({"app_id": BUNDLE, "payload": {"aps": {}}, "delivery": "shade"}, "delivery"),
                 ({"app_id": BUNDLE, "payload": {"aps": {}}, "title": "t"}, "payload"),
                 ({"app_id": BUNDLE, "title": "t", "receiver": ".R"}, "receiver"),
                 ({"app_id": BUNDLE, "title": "t", "write": "x.json"}, "write"),
                 ({"title": "t"}, None)]
        for values, field in cases:
            with self.subTest(values=values):
                error = invalid(self, simulator.apply, self.ios_target(), "push", "send", values,
                                session={})
                if field:
                    self.assertEqual(error.extra["field"], field)
                else:
                    self.assertEqual(error.extra["missing"], ["app_id"])
        self.assertEqual(self.pushes(), [])

    def test_nothing_given_is_the_original_error_with_the_message_fields_in_the_hint(self):
        error = invalid(self, simulator.apply, self.ios_target(), "push", "send",
                        {"app_id": BUNDLE}, session={})
        self.assertEqual((error.message, error.extra["missing"]),
                         ("push send needs payload (a JSON object)", ["payload"]))
        self.assertIn("title=", error.hint)

    def test_cli_composed_push(self) -> None:
        code, payload, _ = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                                        "simulator", "push", "send", "--value",
                                        f"app_id={BUNDLE}", "--value", "silent=true",
                                        "--value", "data.sync=1")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["kind"], "data")
        self.assertEqual(self.pushes()[0]["payload"]["aps"], {"content-available": 1})


# --- Android send -----------------------------------------------------------------------


class AndroidBase(CliBase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(android_push, "READ_BACK_SECONDS", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)
        interval = mock.patch.object(android_push, "READ_BACK_INTERVAL", 0.05)
        interval.start()
        self.addCleanup(interval.stop)

    def send(self, values: dict, *, target: Target | None = None,
             session_record: dict | None = None) -> dict:
        return simulator.apply(target or self.android_target(), "push", "send", values,
                               session=session_record if session_record is not None else {})

    def refused(self, values: dict, code: str, **kwargs) -> errors.AutonomError:
        error = invalid(self, self.send, values, code=code, **kwargs)
        self.assertNotIn(SECRET, json.dumps(error.as_dict()))
        return error

    def broadcasts(self) -> list[dict]:
        return self.fake().get("broadcasts", [])

    def shell_lines(self) -> list[list[str]]:
        return [argv[3:] for argv in self.calls("adb") if argv[2:3] == ["shell"]]

    def assert_no_adb_root(self) -> None:
        self.assertFalse(any(argv[2:] == ["root"] for argv in self.calls("adb")))


class AndroidAppDeliveryTests(AndroidBase):
    def test_su_route_delivers_into_the_fcm_receiver(self) -> None:
        self.patch_fake(app_posts_notifications=True)
        result = self.send({"app_id": PKG, "title": TRICKY, "body": "Body", "badge": 3,
                            "data.order_id": "42", "message_id": "m2"})
        self.assertEqual({key: result[key] for key in (
            "control", "app_id", "app_id_source", "composed", "message_id", "kind", "delivery",
            "route", "receiver_result", "verified", "verification")},
            {"control": "push", "app_id": PKG, "app_id_source": "value", "composed": True,
             "message_id": "m2", "kind": "notification", "delivery": "app", "route": "su",
             "receiver_result": -1, "verified": True, "verification": "read_back"})
        (sent,) = self.broadcasts()
        self.assertTrue(sent["via_su"])
        self.assertEqual((sent["action"], sent["package"], sent["include_stopped"]),
                         (android_push.RECEIVE_ACTION, PKG, False))
        self.assertEqual(sent["extras"]["gcm.notification.title"], TRICKY)  # quoted intact
        self.assertEqual(sent["extras"]["gcm.notification.notification_count"], "3")
        self.assertEqual(sent["extras"]["google.message_id"], "m2")
        self.assertEqual(result["extras_count"], len(sent["extras"]))
        line = next(words for words in self.shell_lines() if "broadcast" in words)
        self.assertEqual(line[:4], ["su", "0", "am", "broadcast"])
        self.assertIn("'It'\\''s $5 off \"today\" `now` \\ ok'", line)
        self.assert_no_adb_root()

    def test_adbd_root_route_runs_the_broadcast_directly(self) -> None:
        self.patch_fake(shell_uid="0")
        result = self.send({"app_id": PKG, "data.k": "v"})
        self.assertEqual(result["route"], "root")
        self.assertFalse(self.broadcasts()[0]["via_su"])
        self.assertFalse(any(words[:1] == ["su"] for words in self.shell_lines()))
        self.assertEqual((result["kind"], result["verified"], result["verification"]),
                         ("data", False, "receiver_accepted"))

    def test_no_root_is_refused_before_anything_is_sent(self) -> None:
        self.patch_fake(su_uid=None)  # a Play Store image
        error = self.refused({"app_id": PKG, "title": SECRET}, errors.PUSH_NOT_DELIVERED)
        self.assertEqual((error.extra["reason"], error.extra["capability"]),
                         ("no_root", "simulator.push"))
        self.assertIn("google_apis", error.hint)
        self.assertIn("Play Store", error.hint)
        self.assertIn("delivery=shade", error.hint)
        self.assertEqual(self.broadcasts(), [])
        self.patch_fake(su_uid="2000")
        self.assertEqual(self.refused({"app_id": PKG, "title": "t"},
                                      errors.PUSH_NOT_DELIVERED).extra["reason"], "no_root")
        self.assert_no_adb_root()

    def test_an_app_that_is_not_installed(self) -> None:
        self.patch_fake(packages={})
        error = self.refused({"app_id": PKG, "title": "t"}, errors.APP_NOT_INSTALLED)
        self.assertEqual(error.extra["app_id"], PKG)
        self.assertEqual(self.broadcasts(), [])

    def test_an_app_without_an_fcm_receiver(self) -> None:
        self.patch_fake(receivers={PKG: []})
        error = self.refused({"app_id": PKG, "title": "t"}, errors.PUSH_NOT_DELIVERED)
        self.assertEqual(error.extra["reason"], "no_receiver")
        self.assertIn("receiver=", error.hint)
        self.assertIn("delivery=shade", error.hint)
        self.assertEqual(self.broadcasts(), [])

    def test_an_explicit_receiver_goes_as_the_shell_without_root(self) -> None:
        self.patch_fake(su_uid=None)
        result = self.send({"app_id": PKG, "data.hello": "world",
                            "receiver": ".CustomReceiver", "action": "dev.autonom.pushprobe.PUSH"})
        self.assertEqual((result["route"], result["receiver_result"]), ("shell", 0))
        self.assertEqual(result["receiver"], f"{PKG}/.CustomReceiver")
        self.assertEqual(result["intent_action"], "dev.autonom.pushprobe.PUSH")
        (sent,) = self.broadcasts()
        self.assertEqual((sent["component"], sent["package"], sent["via_su"]),
                         (f"{PKG}/.CustomReceiver", None, False))
        self.assertFalse(any("query-receivers" in words for words in self.shell_lines()))

    def test_an_explicit_action_uses_root_when_there_is_one(self) -> None:
        result = self.send({"app_id": PKG, "data.k": "v", "action": "dev.example.PUSH"})
        self.assertEqual(result["route"], "su")
        (sent,) = self.broadcasts()
        self.assertEqual((sent["action"], sent["package"]), ("dev.example.PUSH", PKG))
        self.assertEqual(result["receiver_result"], -1)

    def test_result_zero_is_not_received_and_include_stopped_reaches_the_app(self) -> None:
        self.patch_fake(stopped_packages=[PKG])
        error = self.refused({"app_id": PKG, "title": SECRET}, errors.PUSH_NOT_DELIVERED)
        self.assertEqual((error.extra["reason"], error.extra["receiver_result"]),
                         ("not_received", 0))
        self.assertIn("include_stopped=true", error.hint)
        result = self.send({"app_id": PKG, "data.k": "v", "include_stopped": "true"})
        self.assertEqual(result["receiver_result"], -1)
        self.assertTrue(self.broadcasts()[-1]["include_stopped"])
        line = [words for words in self.shell_lines() if "broadcast" in words][-1]
        self.assertIn("--include-stopped-packages", line)
        invalid(self, self.send, {"app_id": PKG, "data.k": "v", "include_stopped": "maybe"})

    def test_any_other_result_is_rejected(self) -> None:
        self.patch_fake(broadcast_results={PKG: 500})
        error = self.refused({"app_id": PKG, "data.k": SECRET}, errors.PUSH_NOT_DELIVERED)
        self.assertEqual((error.extra["reason"], error.extra["receiver_result"]),
                         ("rejected", 500))
        self.patch_fake(broadcast_results={PKG: 500})
        self.assertEqual(self.refused({"app_id": PKG, "data.k": "v", "action": "x.Y"},
                                      errors.PUSH_NOT_DELIVERED).extra["reason"], "rejected")

    def test_odd_broadcast_output_is_a_backend_failure_without_the_text(self) -> None:
        self.patch_fake(broadcast_output=f"Security exception: {SECRET}\n")
        error = self.refused({"app_id": PKG, "title": SECRET}, errors.BACKEND_FAILED)
        self.assertIn("no result line", error.message)

    def test_read_back_not_found_and_an_older_identical_record(self) -> None:
        # the app is in front: it handles the message itself and nothing is shown
        result = self.send({"app_id": PKG, "title": "Hello"})
        self.assertEqual((result["verified"], result["verification"]), (False, "not_in_shade"))
        # an older record with the same title is not this push's
        self.patch_fake(notifications=[{"pkg": PKG, "id": 0, "tag": "FCM-Notification:1",
                                        "title": "Hello", "text": "x"}])
        result = self.send({"app_id": PKG, "title": "Hello"})
        self.assertEqual(result["verification"], "not_in_shade")
        self.patch_fake(app_posts_notifications=True)
        self.assertTrue(self.send({"app_id": PKG, "title": "Hello"})["verified"])

    def test_read_back_by_body_when_there_is_no_title_and_after_a_delay(self) -> None:
        self.patch_fake(app_posts_notifications=True, notification_delay_reads=2,
                        notification_format="api29")
        # the real window: a found record ends the poll, and a loaded machine
        # must not run out of time before the third read
        with mock.patch.object(android_push, "READ_BACK_SECONDS", 10.0):
            result = self.send({"app_id": PKG, "body": "Only (a) body"})
        self.assertEqual((result["verified"], result["verification"]), (True, "read_back"))

    def test_permission_warning_on_api_33_and_newer(self) -> None:
        dump = f"-s {SERIAL} shell dumpsys package {PKG}"
        cases = [("37", DUMPSYS_PACKAGE.format(pkg=PKG, granted="false"), True),
                 ("37", DUMPSYS_PACKAGE.format(pkg=PKG, granted="true"), False),
                 ("37", None, False),  # the lookup failed
                 ("32", DUMPSYS_PACKAGE.format(pkg=PKG, granted="false"), False),
                 ("", DUMPSYS_PACKAGE.format(pkg=PKG, granted="false"), False)]
        for api, text, warned in cases:
            with self.subTest(api=api, text=bool(text)):
                self.patch_fake(getprop={"ro.build.version.sdk": api},
                                fail={dump: [0, text]} if text else {dump: [1, "boom"]})
                result = self.send({"app_id": PKG, "title": "t"})
                codes = [item["code"] for item in result.get("warnings", [])]
                self.assertEqual(codes, ["push_notifications_denied"] if warned else [])
                if warned:
                    warning = result["warnings"][0]
                    self.assertEqual(warning["hint"], f"autonom permissions grant notifications {PKG}")
                    self.assertTrue(warning["error"])
        # a data-only message posts nothing, so there is nothing to warn about
        self.patch_fake(getprop={"ro.build.version.sdk": "37"},
                        fail={dump: [0, DUMPSYS_PACKAGE.format(pkg=PKG, granted="false")]})
        self.assertNotIn("warnings", self.send({"app_id": PKG, "data.k": "v"}))

    def test_a_read_back_that_fails_never_fails_a_sent_push(self) -> None:
        failing = errors.AutonomError(errors.BACKEND_FAILED, "dumpsys did not answer")
        with mock.patch.object(android_push, "notification_records", side_effect=failing):
            result = self.send({"app_id": PKG, "title": "t"})
        self.assertEqual((result["verified"], result["verification"]), (False, "not_in_shade"))
        self.assertEqual(len(self.broadcasts()), 1)

    def test_a_replaced_record_counts_when_its_text_changed(self) -> None:
        old = {"pkg": PKG, "user": "UserHandle{0}", "id": "0", "tag": "order", "title": "Old)",
               "text": None}
        new = {**old, "title": "New)"}
        with mock.patch.object(android_push, "notification_records", side_effect=[[old], [new]]):
            self.assertTrue(self.send({"app_id": PKG, "title": "New"})["verified"])
        with mock.patch.object(android_push, "notification_records",
                               side_effect=[[new], [new], [new], [new], [new], [new], [new],
                                            [new], [new], [new]]):
            # the same text again cannot be told from the record already there
            self.assertFalse(self.send({"app_id": PKG, "title": "New"})["verified"])

    def test_device_answers_that_are_odd(self) -> None:
        self.patch_fake(shell_uid="0",
                        fail={f"-s {SERIAL} shell id -u": [0, "* daemon started successfully\n0"]})
        self.assertEqual(self.send({"app_id": PKG, "data.k": "v"})["route"], "root")
        self.patch_fake(shell_uid="2000")
        self.patch_fake(fail={f"-s {SERIAL} shell cmd package query-receivers":
                              [255, "Unknown command: query-receivers"]})
        error = self.refused({"app_id": PKG, "title": "t"}, errors.BACKEND_FAILED)
        self.assertIn("query-receivers", error.message)
        self.patch_fake(fail={f"-s {SERIAL} shell id -u": [1, "error: device offline"]})
        self.refused({"app_id": PKG, "title": "t"}, errors.BACKEND_FAILED)
        self.patch_fake(fail={f"-s {SERIAL} shell pm path": [1, "error: device offline"]})
        self.refused({"app_id": PKG, "title": "t"}, errors.BACKEND_FAILED)
        self.patch_fake(fail={f"-s {SERIAL} shell su 0 id -u":
                              [127, "/system/bin/sh: su: inaccessible or not found"]})
        self.assertEqual(self.refused({"app_id": PKG, "title": "t"},
                                      errors.PUSH_NOT_DELIVERED).extra["reason"], "no_root")
        self.patch_fake(fail={f"-s {SERIAL} shell cmd notification post": [1, SECRET]})
        error = self.refused({"title": "t", "delivery": "shade"}, errors.BACKEND_FAILED)
        self.assertNotIn(SECRET, error.message)

    def test_android_refusals(self) -> None:
        cases = [({"app_id": PKG, "payload": {"aps": {"alert": "x"}}}, errors.INVALID_VALUE),
                 ({"title": "t"}, errors.INVALID_VALUE),  # no app anywhere
                 ({"app_id": "not a package", "title": "t"}, errors.INVALID_VALUE),
                 ({"app_id": PKG}, errors.INVALID_VALUE),  # nothing to send
                 ({"app_id": PKG, "title": "t", "delivery": "air"}, errors.INVALID_VALUE),
                 ({"app_id": PKG, "title": "t", "receiver": "a b"}, errors.INVALID_VALUE),
                 ({"app_id": PKG, "title": "t", "action": "x;y"}, errors.INVALID_VALUE),
                 ({"app_id": PKG, "title": "t", "write": "x.json"}, errors.INVALID_VALUE)]
        for values, code in cases:
            with self.subTest(values=values):
                self.refused(values, code)
        raw = self.refused({"app_id": PKG, "payload": {"aps": {}}}, errors.INVALID_VALUE)
        self.assertIn("message fields", raw.hint)
        self.assertEqual(self.calls("adb"), [])

    def test_a_physical_device_is_refused(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:  # the existing refusal
            self.send({"app_id": PKG, "title": "t"}, target=self.android_target("R58M123ABC"))
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(caught.exception.extra["capability"], "simulator.controls")
        self.assertEqual(self.calls("adb"), [])

    def test_the_app_comes_from_the_session_on_this_emulator(self) -> None:
        result = self.send({"data.k": "v"}, session_record={"target_id": SERIAL, "app_id": PKG})
        self.assertEqual((result["app_id"], result["app_id_source"]), (PKG, "session"))
        self.refused({"data.k": "v"}, errors.INVALID_VALUE,
                     session_record={"target_id": "emulator-5556", "app_id": PKG})


class AndroidShadeTests(AndroidBase):
    def test_shade_post_and_read_back(self) -> None:
        result = self.send({"title": "Order shipped", "body": TRICKY, "message_id": "t1",
                            "delivery": "shade"})
        self.assertEqual({key: result[key] for key in (
            "delivery", "owner", "tag", "kind", "verified", "verification", "app_id")},
            {"delivery": "shade", "owner": "com.android.shell", "tag": "autonom_push_t1",
             "kind": "notification", "verified": True, "verification": "read_back",
             "app_id": None})
        self.assertEqual([item["code"] for item in result["warnings"]], ["push_shade_only"])
        post = next(words for words in self.shell_lines() if "post" in words)
        self.assertEqual(post[:7], ["cmd", "notification", "post", "-S", "bigtext", "-t",
                                    "'Order shipped'"])
        self.assertEqual(post[7:], ["'autonom_push_t1'",
                                    "'It'\\''s $5 off \"today\" `now` \\ ok'"])
        (record,) = self.fake()["notifications"]
        self.assertEqual((record["pkg"], record["tag"], record["title"], record["text"]),
                         ("com.android.shell", "autonom_push_t1", "Order shipped", TRICKY))
        self.assertFalse(any(words[:1] == ["su"] for words in self.shell_lines()))
        self.assertEqual(self.broadcasts(), [])
        self.assert_no_adb_root()

    def test_shade_on_api_29_with_a_late_record_and_a_title_only(self) -> None:
        self.patch_fake(notification_format="api29", notification_delay_reads=2,
                        getprop={"ro.build.version.sdk": "29"})
        with mock.patch.object(android_push, "READ_BACK_SECONDS", 10.0):  # see above
            result = self.send({"title": "Only a title", "delivery": "shade"})
        self.assertTrue(result["verified"])
        post = next(words for words in self.shell_lines() if "post" in words)
        self.assertEqual(post[-1], "'Only a title'")

    def test_shade_record_missing_is_unverified(self) -> None:
        self.patch_fake(shell_uid="0")  # adbd as root: the post is not shown
        result = self.send({"body": "b", "delivery": "shade"})
        self.assertEqual((result["verified"], result["verification"]), (False, "not_in_shade"))
        self.assertIn("root", result["verification_detail"])

    def test_shade_refusals(self) -> None:
        self.patch_fake(getprop={"ro.build.version.sdk": "28"})
        error = self.refused({"title": SECRET, "delivery": "shade"}, errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual((error.extra["capability"], error.extra["api_level"]),
                         ("simulator.push", 28))
        self.patch_fake(getprop={"ro.build.version.sdk": "37"})
        error = self.refused({"data.k": "v", "delivery": "shade"}, errors.INVALID_VALUE)
        self.assertEqual(error.extra["missing"], ["title", "body"])
        self.refused({"title": "t", "delivery": "shade", "receiver": ".R"}, errors.INVALID_VALUE)
        self.assertFalse(any("post" in words for words in self.shell_lines()))


class AndroidCliTests(CliBase):
    def journal_text(self) -> str:
        record = session.load_current()
        path = Path(record["artifacts_dir"]) / "journal.ndjson"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_session_app_and_nothing_of_the_text_in_the_journal(self) -> None:
        self.patch_fake(app_posts_notifications=True)
        code, payload, _ = self.android_cli("session", "start", "--app-id", PKG)
        self.assertEqual(code, 0, payload)
        self.addCleanup(self.android_cli, "session", "stop")
        code, payload, output = self.android_cli(
            "simulator", "push", "send", "--value", f"title={SECRET}",
            "--json", json.dumps({"body": SECRET, "data": {"k": SECRET}}))
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["app_id"], payload["app_id_source"], payload["route"]),
                         (PKG, "session", "su"))
        self.assertTrue(payload["verified"])
        self.assertNotIn(SECRET, json.dumps(payload))
        self.patch_fake(su_uid=None)
        code, payload, output = self.android_cli("simulator", "push", "send", "--value",
                                                 f"title={SECRET}")
        self.assertEqual((code, payload["error_code"], payload["reason"]),
                         (2, errors.PUSH_NOT_DELIVERED, "no_root"))
        self.assertTrue(payload["error"] and payload["hint"])
        self.assertNotIn(SECRET, output)
        journal = self.journal_text()
        entries = [json.loads(line) for line in journal.splitlines()]
        pushes = [entry for entry in entries if entry.get("verb") == "simulator push"]
        self.assertEqual([entry["ok"] for entry in pushes], [True, False])
        self.assertEqual(pushes[1]["error_code"], errors.PUSH_NOT_DELIVERED)
        self.assertNotIn(SECRET, journal)

    def test_a_value_without_equals_is_not_echoed(self) -> None:
        code, payload, output = self.android_cli("simulator", "push", "send", "--value", SECRET)
        self.assertEqual((code, payload["error_code"]), (2, errors.FLOW_COMMAND_INVALID))
        self.assertTrue(payload["hint"])
        self.assertNotIn(SECRET, output)

    def test_capabilities_list_push_on_an_emulator(self) -> None:
        from autonom_lib import providers

        snapshot = providers.LocalDeviceSession(self.android_target(), {}).capabilities()
        states = {item.name: item.state for item in snapshot.capabilities}
        self.assertEqual(states["simulator.push"], "available")
        phone = providers.LocalDeviceSession(self.android_target("R58M123ABC"), {}).capabilities()
        self.assertEqual({item.name: item.state for item in phone.capabilities}["simulator.push"],
                         "unavailable")


# --- what would fail after a send is refused before it ----------------------------------


def nested(levels: int, leaf: object = "x", *, lists: bool = False) -> object:
    """`levels` objects (or arrays) inside one another around `leaf`."""
    value = leaf
    for _ in range(levels):
        value = [value] if lists else {"k": value}
    return value


class MessageLimitTests(unittest.TestCase):
    """Deep nesting, lone surrogates, non-finite numbers and size: refused
    by `normalize`, naming the field, before anything renders or is sent."""

    def test_nesting_is_capped_at_32_levels(self) -> None:
        cap = push_message.MAX_DEPTH
        self.assertEqual(cap, 32)
        # `data` and `ios` are level 1: a data value holds 31 more, ios 32 in all
        for lists in (False, True):
            with self.subTest(lists=lists):
                push_message.normalize({"data": {"deep": nested(cap - 1, lists=lists)}})
                error = invalid(self, push_message.normalize,
                                {"data": {"ok": "1", "deep": nested(cap, lists=lists)}})
                self.assertEqual(error.extra["field"], "data.deep")
                self.assertIn("32 levels", error.message)
                push_message.normalize({"title": "t", "ios": nested(cap, lists=False)})
                ios = {"title": "t", "ios": {"aps": nested(cap, lists=lists)}}
                self.assertEqual(invalid(self, push_message.normalize, ios).extra["field"], "ios")

    def test_nesting_that_would_exhaust_the_stack_is_refused_not_raised(self) -> None:
        # data and ios share a key path: rendering deep-merges them level by level
        deep = nested(1500)
        for label, raw in (("data and ios, 1500 levels", {"data": {"k": deep}, "ios": {"k": deep}}),
                           ("ios, 100000 levels", {"title": "t", "ios": nested(100_000)})):
            with self.subTest(label):
                error = invalid(self, push_message.normalize, raw)
                self.assertIn(error.extra["field"], ("data.k", "ios"))
                invalid(self, push_message.compose, raw)

    def test_text_utf8_cannot_hold_is_refused_naming_the_field(self) -> None:
        lone = "a\ud800b"
        cases = [({"title": lone}, "title"), ({"body": lone}, "body"),
                 ({"title": "t", "subtitle": lone}, "subtitle"),
                 ({"title": "t", "sound": lone}, "sound"),
                 ({"title": "t", "category": lone}, "category"),
                 ({"title": "t", "thread": lone}, "thread"),
                 ({"title": "t", "channel": lone}, "channel"),
                 ({"title": "t", "image": lone}, "image"),
                 ({"title": "t", "message_id": lone}, "message_id"),
                 ({"title": "t", "name": lone}, "name"),
                 ({"title": "t", "description": lone}, "description"),
                 ({"data": {"k": lone}}, "data.k"),
                 ({"data": {"k": {"a": [1, lone]}}}, "data.k"),
                 ({"data": {"k": {lone: 1}}}, "data.k"),
                 ({"data": {lone: "v"}}, "data"),
                 ({"title": "t", "ios": {"aps": {"sound": lone}}}, "ios"),
                 ({"title": "t", "ios": {lone: 1}}, "ios"),
                 ({"title": "t", "android": {"k": lone}}, "android"),
                 ({"title": "t", "android": {lone: "v"}}, "android")]
        for raw, field in cases:
            with self.subTest(field=field, raw=repr(raw)[:40]):
                error = invalid(self, push_message.normalize, raw)
                self.assertEqual(error.extra["field"], field)
                # the error itself can always be written out
                json.dumps(error.as_dict(), ensure_ascii=False).encode("utf-8")
        for key in ("file", "write"):
            with self.subTest(path=key):
                values = {"title": "t", key: "p\udc00.json"}
                call = push_message.compose if key == "write" else push_message.from_values
                self.assertEqual(invalid(self, call, values).extra["field"], key)

    def test_numbers_that_are_not_finite_are_refused(self) -> None:
        for number in (float("nan"), float("inf"), float("-inf")):
            for raw, field in (({"data": {"n": number}}, "data.n"),
                               ({"data": {"n": {"list": [1, number]}}}, "data.n"),
                               ({"title": "t", "ios": {"aps": {"x": number}}}, "ios")):
                with self.subTest(number=number, field=field):
                    error = invalid(self, push_message.normalize, raw)
                    self.assertEqual(error.extra["field"], field)
                    self.assertIn("finite", error.message)
        self.assertEqual(push_message.normalize({"data": {"n": 1.5}})["data"], {"n": 1.5})

    def test_deep_json_text_is_refused_not_raised(self) -> None:
        text = '{"k":' + "[" * 1_000_000 + "]" * 1_000_000 + "}"
        error = invalid(self, push_message.from_values, {"data": text})
        self.assertEqual(error.extra["field"], "data")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deep.json"
            path.write_text('{"title": "t", "data": ' + text + "}", encoding="utf-8")
            error = invalid(self, push_message.from_values, {"file": str(path)})
            self.assertEqual(error.extra["path"], str(path))
            self.assertIn("nested too deeply", error.message)

    def test_android_extras_above_4096_bytes_warn_like_ios(self) -> None:
        # the android escape hatch is not in the APNs payload: only Android is over
        compose = push_message.compose({"title": "t", "android": {"big": "é" * 2100}})
        self.assertFalse(compose["ios"]["over_limit"])
        (warning,) = compose["warnings"]
        extras = compose["android"]["extras"]
        self.assertEqual((warning["code"], warning["platform"], warning["limit"]),
                         ("push_payload_large", "android", 4096))
        self.assertEqual(warning["bytes"], sum(len(key.encode()) + len(value.encode())
                                               for key, value in extras.items()))
        self.assertIn("FCM", warning["error"])
        self.assertTrue(warning["hint"])
        both = push_message.compose({"body": "x" * 4100})
        self.assertEqual([(item["code"], item["platform"]) for item in both["warnings"]],
                         [("push_payload_large", "ios"), ("push_payload_large", "android")])
        only = push_message.compose({"body": "x" * 4100, "platform": "android"})
        self.assertEqual([item["platform"] for item in only["warnings"]], ["android"])
        fits = push_message.compose({"body": "x" * 3800, "platform": "android"})
        self.assertEqual(fits["warnings"], [])

    def test_a_render_over_64_kb_is_refused(self) -> None:
        limit = push_message.MAX_RENDERED_BYTES
        self.assertEqual(limit, 65536)
        for raw, platform in (({"body": "x" * limit}, "ios"),
                              ({"title": "t", "android": {"k": "x" * limit}}, "android"),
                              ({"data": {"k": "é" * (limit // 2)}}, "ios")):
            with self.subTest(platform=platform):
                error = invalid(self, push_message.normalize, raw)
                self.assertEqual((error.extra["platform"], error.extra["limit"]),
                                 (platform, limit))
                self.assertGreater(error.extra["bytes"], limit)
                self.assertNotIn("xxxx", error.message)
        push_message.normalize({"body": "x" * 60000})  # large, but under the cap

    def test_silent_composes_with_an_ios_note(self) -> None:
        notes = push_message.compose({"silent": True, "data": {"k": "v"}})["notes"]
        (note,) = [item for item in notes if "silent" in item]
        self.assertTrue(note.startswith("ios: "))
        self.assertIn("no visible content", note)
        self.assertEqual(push_message.compose({"silent": True, "platform": "android"})["notes"], [])
        self.assertFalse(any("silent" in item
                             for item in push_message.compose({"title": "t"})["notes"]))


class ValueTextTests(CliBase):
    """`--value` is text and parsed; `--json` keeps JSON types, as a file and
    the schema do."""

    def compose(self, *argv: str) -> tuple[int, dict]:
        code, payload, _ = self.run_cli("simulator", "push", "compose", *argv)
        return code, payload

    def test_json_values_keep_their_types(self) -> None:
        for body, field in (({"title": "t", "badge": "3"}, "badge"),
                            ({"title": "t", "silent": "false"}, "silent"),
                            ({"title": "t", "fcm": "no"}, "fcm"),
                            ({"title": "t", "data": '{"a": 1}'}, "data")):
            with self.subTest(body=body):
                code, payload = self.compose("--json", json.dumps(body))
                self.assertEqual((code, payload["error_code"], payload["field"]),
                                 (2, errors.INVALID_VALUE, field))
                self.assertTrue(payload["hint"])
        code, payload = self.compose("--json", '{"title": "t", "badge": 3, "fcm": false}')
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["message"]["badge"], payload["message"]["fcm"]), (3, False))

    def test_value_text_is_parsed(self) -> None:
        code, payload = self.compose("--value", "title=t", "--value", "badge=3",
                                     "--value", "fcm=no", "--value", 'data={"a": 1}')
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["message"]["badge"], payload["message"]["fcm"],
                          payload["message"]["data"]), (3, False, {"a": 1}))
        # --value wins over the same key in --json, and is text
        code, payload = self.compose("--json", '{"title": "t", "badge": "x"}',
                                     "--value", "badge=4")
        self.assertEqual((code, payload["message"]["badge"]), (0, 4))

    def test_a_file_keeps_json_types(self) -> None:
        path = self.root / "p.json"
        path.write_text(json.dumps({"title": "t", "badge": "3"}), encoding="utf-8")
        code, payload = self.compose("--value", f"file={path}")
        self.assertEqual((code, payload["field"]), (2, "badge"))
        code, payload = self.compose("--value", f"file={path}", "--value", "badge=5")
        self.assertEqual((code, payload["message"]["badge"]), (0, 5))

    def test_the_reply_is_always_strict_json(self) -> None:
        def strict(text: str) -> dict:
            def refuse(word: str) -> None:
                raise AssertionError(f"non-strict JSON constant {word}")
            return json.loads(text, parse_constant=refuse)

        for body in ('{"data": {"x": NaN}}', '{"data": {"x": [Infinity]}}',
                     '{"title": "t", "ios": {"aps": {"x": -Infinity}}}'):
            with self.subTest(body=body):
                completed = subprocess.run(
                    [sys.executable, str(CLI), "simulator", "push", "compose", "--json", body],
                    capture_output=True, text=True, env=self.env, timeout=120, cwd=self.tmp.name)
                self.assertEqual(completed.returncode, 2, completed.stdout)
                error = strict(completed.stderr)
                self.assertEqual(error["error_code"], errors.INVALID_VALUE)
                self.assertIn(error["field"], ("data.x", "ios"))
                self.assertEqual(completed.stdout, "")

    def test_a_lone_surrogate_is_named_not_a_codec_error(self) -> None:
        code, payload = self.compose("--json", '{"title": "\\ud800 hi"}')
        self.assertEqual((code, payload["error_code"], payload["field"]),
                         (2, errors.INVALID_VALUE, "title"))
        self.assertNotIn("codec", payload["error"])


class AndroidSendLimitTests(AndroidBase):
    def test_device_output_that_is_not_utf8_never_fails_a_sent_push(self) -> None:
        # the shade prints text in Latin-1: \xe9 and \xff are not UTF-8
        self.patch_fake(app_posts_notifications=True, notification_encoding="latin-1")
        # the record appears after the broadcast: its title cannot be read
        # back, so the push is not confirmed, and the reply is never an error
        result = self.send({"app_id": PKG, "title": "Café open"})
        self.assertEqual((result["verified"], result["verification"]), (False, "not_in_shade"))
        # another app's unreadable record does not spoil the read-back
        self.patch_fake(notifications=[{"pkg": "com.other", "id": 1, "tag": "t",
                                        "title": "café ÿ", "text": None}])
        result = self.send({"app_id": PKG, "title": "Hello"})
        self.assertEqual((result["verified"], result["verification"]), (True, "read_back"))
        self.assertEqual(len(self.broadcasts()), 2)

    def test_device_output_that_is_not_utf8_through_the_cli(self) -> None:
        self.patch_fake(app_posts_notifications=True, notification_encoding="latin-1",
                        notifications=[{"pkg": PKG, "id": 1, "tag": "old",
                                        "title": "ÿþ", "text": None}])
        code, payload, _ = self.android_cli("simulator", "push", "send", "--value",
                                            f"app_id={PKG}", "--value", "title=Hi")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])

    def test_a_failed_api_level_lookup_never_stops_the_push(self) -> None:
        for failure in (errors.AutonomError(errors.BACKEND_FAILED, "adb shell getprop did not "
                                            "answer within 15 s"),
                        ValueError("odd getprop answer")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(android_push, "api_level", side_effect=failure):
                    result = self.send({"app_id": PKG, "title": "t"})
                self.assertEqual(result["receiver_result"], -1)
                self.assertNotIn("warnings", result)
        self.assertEqual(len(self.broadcasts()), 2)

    def test_extras_above_4096_bytes_are_sent_with_a_warning(self) -> None:
        result = self.send({"app_id": PKG, "data.blob": "x" * 4100})
        (warning,) = result["warnings"]
        self.assertEqual((warning["code"], warning["platform"]), ("push_payload_large", "android"))
        self.assertEqual(warning["bytes"], sum(len(key) + len(value) for key, value
                                               in self.broadcasts()[0]["extras"].items()))
        self.assertNotIn("warnings", self.send({"app_id": PKG, "data.blob": "x" * 3000}))

    def test_a_render_over_64_kb_never_reaches_adb(self) -> None:
        for values in ({"app_id": PKG, "body": SECRET + "x" * 70000},
                       {"body": SECRET + "x" * 70000, "delivery": "shade"}):
            with self.subTest(delivery=values.get("delivery", "app")):
                error = self.refused(values, errors.INVALID_VALUE)
                self.assertEqual(error.extra["limit"], push_message.MAX_RENDERED_BYTES)
        self.assertEqual(self.calls("adb"), [])


class IosSendLimitTests(CliBase):
    NO_CONTENT = ("An error was encountered processing the command (domain=UNErrorDomain, "
                  "code=1401):\nSimulator device failed to complete the requested operation.\n"
                  "Underlying error (domain=UNErrorDomain, code=1401):\n"
                  "\tNotification has no user visible content")
    NO_ALERT = ("An error was encountered processing the command (domain=UNErrorDomain, "
                "code=2002):\nUnderlying error (domain=UNErrorDomain, code=2002):\n"
                "\tRepository could not save notification. No content that needs to alert.")

    def fail_push(self, text: str) -> None:
        self.patch_fake(simctl_fail={f"simctl push {UDID}": [1, text]})

    def send(self, values: dict) -> dict:
        return simulator.apply(self.ios_target(), "push", "send", values, session={})

    def test_no_visible_content_is_push_not_delivered(self) -> None:
        for text in (self.NO_CONTENT, self.NO_ALERT):
            for values in ({"app_id": BUNDLE, "silent": True, "data.k": "v"},
                           {"app_id": BUNDLE, "silent": True, "badge": 1},
                           {"app_id": BUNDLE, "payload": {"aps": {"content-available": 1}}}):
                with self.subTest(code=text[60:64], values=list(values)):
                    self.fail_push(text)
                    error = invalid(self, self.send, values, code=errors.PUSH_NOT_DELIVERED)
                    self.assertEqual(error.message,
                                     "the Simulator refused a push with no alert text")
                    self.assertEqual(error.hint,
                                     "simctl on this iOS runtime delivers only pushes that show "
                                     "something: add a title or body. A silent "
                                     "(content-available) push cannot be tested through the "
                                     "Simulator here; on Android it works (a data message).")
                    self.assertEqual((error.extra["reason"], error.extra["capability"],
                                      error.extra["app_id"]),
                                     ("no_visible_content", "simulator.push", BUNDLE))

    def test_other_simctl_failures_are_unchanged(self) -> None:
        for text in ("An error was encountered processing the command (domain=com.apple."
                     "CoreSimulator.SimError, code=405):\nUnable to lookup in current state: "
                     "Shutdown",
                     "Underlying error (domain=UNErrorDomain, code=1):\n\tnot allowed",
                     "Underlying error (domain=UNErrorDomain, code=14010)"):
            with self.subTest(text=text[-20:]):
                self.fail_push(text)
                error = invalid(self, self.send, {"app_id": BUNDLE, "payload": {"aps": {}}},
                                code=errors.BACKEND_FAILED)
                self.assertEqual(error.message, text.strip())
                self.assertEqual(error.hint, "Run 'autonom doctor' to check the Xcode toolchain.")

    def test_no_visible_content_through_the_cli(self) -> None:
        self.fail_push(self.NO_CONTENT)
        code, payload, _ = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                                        "simulator", "push", "send", "--value", f"app_id={BUNDLE}",
                                        "--value", "silent=true")
        self.assertEqual((code, payload["error_code"], payload["reason"]),
                         (2, errors.PUSH_NOT_DELIVERED, "no_visible_content"))

    def test_an_accepted_silent_push_is_unconfirmed(self) -> None:
        result = self.send({"app_id": BUNDLE, "silent": True, "data.k": "v"})
        (warning,) = result["warnings"]
        self.assertEqual(warning["code"], "push_silent_unconfirmed")
        self.assertEqual(warning["error"], "simctl accepted the silent push; whether the app ran "
                                           "its background handler is not known")
        self.assertTrue(warning["hint"])
        self.assertNotIn("warnings", self.send({"app_id": BUNDLE, "title": "t"}))
        # the raw path is untouched: the same reply, no new warning
        raw = self.send({"app_id": BUNDLE, "payload": {"aps": {"content-available": 1}}})
        self.assertEqual(raw, {"control": "push", "app_id": BUNDLE, "app_id_source": "value",
                               "verified": False, "verification": "unavailable"})

    def test_a_render_over_64_kb_never_reaches_simctl(self) -> None:
        error = invalid(self, self.send, {"app_id": BUNDLE, "body": "x" * 70000})
        self.assertEqual(error.extra["platform"], "ios")
        self.assertFalse(any(argv[:2] == ["simctl", "push"] for argv in self.calls("simctl")))


if __name__ == "__main__":
    unittest.main()
