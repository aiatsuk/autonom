"""App profile, warm-up, accessibility and analytics checks (tp-023..tp-026).

The library modules (``autonom_lib.pretest.profile``, ``warmup``, ``a11y``,
``events``) are covered directly with fixtures from ``tests/fixtures/pretest/``
and a temporary ``AUTONOM_HOME``; the verbs ``pretest warmup``, ``pretest
a11y`` and ``pretest events`` run end to end through scripts/autonom.py on
the fake adb (the warm-up runs ``main()`` in this process so a tap can move
the fake screen to the next dump).

Every refusal is checked for its ``error_code``, a ``hint``, and that it
wrote nothing: no finding, no saved tree, no device input. The analytics
check is checked to never copy a request body value (an e-mail address, a
phone number, a product code, an unexpected event's name) into its output,
its findings or the journal.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
sys.path.insert(0, str(ROOT / "scripts"))
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FIXTURES = ROOT / "tests/fixtures/pretest"
SERIAL = "emulator-5554"
APP = "com.example.app"

from autonom_lib import errors, selector, ui, ui_android, ui_ios  # noqa: E402
from autonom_lib.atlas import fingerprint as fingerprint_mod  # noqa: E402
from autonom_lib.pretest import a11y, context as context_mod  # noqa: E402
from autonom_lib.pretest import events as events_mod  # noqa: E402
from autonom_lib.pretest import profile as profile_mod  # noqa: E402
from autonom_lib.pretest import warmup as warmup_mod  # noqa: E402

TICKET = "# WB-9 Cart badge\n\nThe cart shows the item count.\n"
PII = ("anna.k@example.com", "+31 6 1234 5678", "secret-sku-991", "promo_click", "abc123")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def nodes_of(name: str) -> list[dict]:
    text = fixture(name)
    parsed = ui_ios.parse_all(text) if name.endswith(".json") else ui_android.parse_all(text)
    return selector.mark_visibility(ui.annotate_parents(parsed))


def screen_id(name: str) -> str:
    return fingerprint_mod.fingerprint(nodes_of(name))["structure"]


def load_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("autonom_cli_profile", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HomeCase(unittest.TestCase):
    """A temporary root with a private AUTONOM_HOME for library tests."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()

    def put(self, where: str, text: str, name: str = "profile.yaml") -> Path:
        base = (self.workspace / ".autonom/apps" if where == "workspace"
                else self.home / "apps") / APP
        base.mkdir(parents=True, exist_ok=True)
        path = base / name
        path.write_text(text, encoding="utf-8")
        return path

    def invalid(self, text: str, where: str = "workspace") -> errors.AutonomError:
        self.put(where, text)
        with self.assertRaises(errors.AutonomError) as caught:
            profile_mod.load(self.workspace, APP)
        self.assertEqual(caught.exception.code, errors.PROFILE_INVALID)
        self.assertTrue(caught.exception.hint)
        return caught.exception


# --- tp-023: app profile ----------------------------------------------------------------


class ProfileLookupTests(HomeCase):
    def test_workspace_wins_then_home_then_none(self) -> None:
        self.assertIsNone(profile_mod.load(self.workspace, APP))
        home = self.put("home", "flaky_screens: [Home splash]\n")
        found = profile_mod.load(self.workspace, APP)
        self.assertEqual((found["source"], found["path"]), ("home", str(home)))
        workspace = self.put("workspace", "flaky_screens: [Workspace splash]\n")
        found = profile_mod.load(self.workspace, APP)
        self.assertEqual((found["source"], found["path"]), ("workspace", str(workspace)))
        self.assertEqual(found["flaky_screens"], ["Workspace splash"])
        # the home profile is never read once the workspace has one, even a bad one
        self.put("home", "password: hunter2\n")
        self.assertEqual(profile_mod.load(self.workspace, APP)["source"], "workspace")

    def test_candidates_order_and_unsafe_app_ids(self) -> None:
        listed = profile_mod.candidates(self.workspace, APP)
        self.assertEqual([source for source, _ in listed], ["workspace", "home"])
        self.assertEqual(listed[1][1], self.home / "apps" / APP / "profile.yaml")
        self.assertEqual([source for source, _ in profile_mod.candidates(None, APP)], ["home"])
        for bad in ("", None, "../etc", "a/b", "a..b"):
            self.assertEqual(profile_mod.candidates(self.workspace, bad), [], bad)

    def test_full_profile_is_normalized(self) -> None:
        path = self.put("workspace", fixture("profile_events.yaml"))
        (path.parent / "login.yaml").write_text(fixture("login.yaml"), encoding="utf-8")
        self.put("workspace", fixture("profile_events.yaml")
                 + "login_flow: login.yaml\nwarmup_flow: flows/warm.yaml\n"
                   "fixtures:\n  product: Blue mug\n  sizes: [S, M]\n")
        found = profile_mod.load(self.workspace, APP)
        self.assertEqual(found["schema"], "autonom.pretest-profile/v1")
        self.assertEqual(found["app_id"], APP)
        self.assertEqual(found["login_flow"], str((path.parent / "login.yaml").resolve()))
        self.assertEqual(found["warmup_flow"], str((path.parent / "flows/warm.yaml").resolve()))
        self.assertEqual(found["accounts"], {"qa": {"user": "PRETEST_QA_USER",
                                                    "password": "PRETEST_QA_PASSWORD"}})
        self.assertEqual(profile_mod.account_variables(found), ["PRETEST_QA_USER", "PRETEST_QA_PASSWORD"])
        self.assertEqual(found["fixtures"], {"product": "Blue mug", "sizes": ["S", "M"]})
        self.assertEqual([rule["name"] for rule in found["events"]],
                         ["screen_view", "add_to_cart"])
        self.assertEqual(found["events"][1]["method"], "POST")
        self.assertEqual(found["events"][1]["host"], "*.example.com")

    def test_context_reports_the_profile_used_or_none(self) -> None:
        repo = self.workspace
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        for argv in (["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", *argv], cwd=repo, check=True)
        (repo / "a.txt").write_text("1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
        context = context_mod.build(repo, "main", None, None, None, APP)
        self.assertIsNone(context["profile"])
        home = self.put("home", "flaky_screens: [Splash]\n")
        context = context_mod.build(repo, "main", None, None, None, APP)
        self.assertEqual(context["profile"]["path"], str(home))
        self.assertEqual(context["profile"]["source"], "home")
        workspace = self.put("workspace", "flaky_screens: [Splash]\n")
        context = context_mod.build(repo, "main", None, None, None, APP)
        self.assertEqual(context["profile"]["path"], str(workspace))
        self.put("workspace", fixture("profile_secret.yaml"))
        with self.assertRaises(errors.AutonomError) as caught:
            context_mod.build(repo, "main", None, None, None, APP)
        self.assertEqual(caught.exception.code, errors.PROFILE_INVALID)

    def test_schema_file_names_every_key(self) -> None:
        schema = json.loads((ROOT / "schemas/pretest-profile-v1.schema.json").read_text("utf-8"))
        self.assertEqual(set(schema["properties"]), set(profile_mod.KEYS))
        self.assertFalse(schema["additionalProperties"])
        rule = schema["$defs"]["event_rule"]
        self.assertEqual(set(rule["properties"]), set(profile_mod.EVENT_KEYS))
        self.assertEqual(schema["properties"]["schema"]["const"], profile_mod.SCHEMA)


class ProfileValidationTests(HomeCase):
    def test_inline_password_is_refused_without_echoing_it(self) -> None:
        exc = self.invalid(fixture("profile_secret.yaml"))
        self.assertIn("password", exc.message)
        payload = json.dumps(exc.as_dict())
        self.assertNotIn("hunter2", payload)
        self.assertEqual(exc.extra["failures"][0]["path"], "password")

    def test_credential_shapes_everywhere(self) -> None:
        cases = {
            "fixtures:\n  note: 'Authorization: Bearer abcdefghijklmnop0123456789'\n":
                "fixtures.note",
            "fixtures:\n  jwt: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl\n":
                "fixtures.jwt",
            "fixtures:\n  - api_key=abc123secret\n": "fixtures[0]",
            "fixtures:\n  url: https://user:pa55word@example.com/x\n": "fixtures.url",
            "accounts:\n  qa:\n    token: abc123\n": "accounts.qa.token",
            "fixtures:\n  pin: '1234'\n": "fixtures.pin",
            "fixtures:\n  note: 'пароль: секрет123'\n": "fixtures.note",
        }
        for text, where in cases.items():
            with self.subTest(where=where):
                exc = self.invalid(text)
                self.assertIn(where, [item["path"] for item in exc.extra["failures"]])
                self.assertNotIn("abc123", json.dumps(exc.as_dict(), ensure_ascii=False))

    def test_variable_names_are_not_secrets(self) -> None:
        self.put("workspace", "accounts:\n  qa:\n    password: PRETEST_QA_PASSWORD\n    pin: PRETEST_QA_PIN\n"
                              "  admin: PRETEST_ADMIN_USER\nfixtures:\n  token_env: API_TOKEN\n")
        found = profile_mod.load(self.workspace, APP)
        self.assertEqual(found["accounts"]["admin"], "PRETEST_ADMIN_USER")

    def test_accounts_hold_variable_names_only(self) -> None:
        for text in ("accounts:\n  qa: anna@example.com\n",
                     "accounts:\n  qa:\n    user: anna\n",
                     "accounts: [PRETEST_QA_USER]\n",
                     "accounts:\n  qa: [PRETEST_QA_USER]\n"):
            with self.subTest(text=text):
                exc = self.invalid(text)
                self.assertTrue(exc.extra["failures"])
                self.assertNotIn("anna", exc.message)

    def test_accounts_name_only_autonom_or_pretest_variables(self) -> None:
        """Fix round 5, item 4: any other variable name makes the profile invalid."""
        for name in ("HOME", "AWS_SECRET_ACCESS_KEY", "QA_USER", "AUTONOM_", "PRETEST_",
                     "PRETESTX_USER", "autonom_user", "XAUTONOM_USER", "_AUTONOM_USER"):
            for text, where in ((f"accounts:\n  qa: {name}\n", "accounts.qa"),
                                (f"accounts:\n  qa:\n    user: {name}\n",
                                 "accounts.qa.user")):
                with self.subTest(text=text):
                    exc = self.invalid(text)
                    failures = exc.extra["failures"]
                    self.assertIn(where, [item["path"] for item in failures])
                    reason = next(item["reason"] for item in failures if item["path"] == where)
                    self.assertIn("AUTONOM_ or PRETEST_", reason)
                    self.assertIn("AUTONOM_ or PRETEST_", exc.hint)
                    json.dumps(exc.as_dict())
        self.put("workspace", "accounts:\n  qa: AUTONOM_QA_USER\n"
                              "  buyer:\n    user: PRETEST_B_1\n    password: AUTONOM_PW\n")
        found = profile_mod.load(self.workspace, APP)
        self.assertEqual(profile_mod.account_variables(found),
                         ["AUTONOM_QA_USER", "PRETEST_B_1", "AUTONOM_PW"])

    def test_warmup_never_reads_other_variables(self) -> None:
        from autonom_lib.flow import executor as flow_executor
        from autonom_lib.pretest import warmup as warmup_mod

        # a hand-built profile dict that skipped validation still only reads
        # AUTONOM_/PRETEST_ variables
        profile = {"accounts": {"qa": "PRETEST_QA_USER", "evil": "HOME",
                                "aws": {"key": "AWS_SECRET_ACCESS_KEY",
                                        "user": "AUTONOM_AWS_USER"}}}
        env = {"PRETEST_QA_USER": "qa", "HOME": "/home/x",
               "AWS_SECRET_ACCESS_KEY": "nope", "AUTONOM_AWS_USER": "aws"}
        with mock.patch.dict(os.environ, env):
            config = warmup_mod._secrets(profile, flow_executor.RunConfig())  # noqa: SLF001
        self.assertEqual(dict(config.secrets),
                         {"PRETEST_QA_USER": "qa", "AUTONOM_AWS_USER": "aws"})

    def test_schema_account_pattern_matches_the_rule(self) -> None:
        schema = json.loads((ROOT / "schemas/pretest-profile-v1.schema.json").read_text("utf-8"))
        self.assertEqual(schema["$defs"]["env_name"]["pattern"],
                         profile_mod.ACCOUNT_VAR.pattern)

    def test_structure_refusals(self) -> None:
        cases = {
            "colour: blue\n": "colour",
            "schema: autonom.other/v9\n": "schema",
            "login_flow: ''\n": "login_flow",
            "login_flow: [a, b]\n": "login_flow",
            "fixtures: plain\n": "fixtures",
            "flaky_screens: Splash\n": "flaky_screens",
            "flaky_screens:\n  - a: b\n": "flaky_screens",
            "events: rule\n": "events",
            "events:\n  - just-a-name\n": "events[0]",
            "events:\n  - name: a\n    method: POST\n    colour: x\n": "events[0]",
            "events:\n  - name: a\n    method: [POST]\n": "events[0]",
            "events:\n  - method: POST\n": "events[0]",
            "events:\n  - name: a\n    path: /x\n  - name: a\n    path: /y\n": "events[1].name",
            "events:\n  - name: a\n    body_key: e\n": "events[0]",
            "events:\n  - name: a\n    method: 'PO ST'\n": "events[0].method",
            "events:\n  - name: a\n    path: /x\n    body_value: v\n": "events[0].body_value",
            "events:\n  - name: a\n    path: /x\n    body_key: 'a..b'\n": "events[0].body_key",
        }
        for text, where in cases.items():
            with self.subTest(where=where, text=text):
                exc = self.invalid(text)
                self.assertIn(where, [item["path"] for item in exc.extra["failures"]])
        many = "events:\n" + "".join(f"  - name: e{i}\n    path: /x\n"
                                     for i in range(profile_mod.MAX_EVENTS + 1))
        self.invalid(many)
        flaky = "flaky_screens:\n" + "".join(f"  - s{i}\n"
                                             for i in range(profile_mod.MAX_FLAKY + 1))
        self.invalid(flaky)

    def test_file_level_refusals(self) -> None:
        exc = self.invalid("events:\n  - name: a\n\tpath: /x\n")
        self.assertIn("line", exc.extra)
        self.invalid("- a\n- b\n")
        self.invalid("a: 1\na: 2\n")
        path = self.put("workspace", "x")
        path.write_bytes(b"flaky_screens: [\xff\xfe]\n")
        with self.assertRaises(errors.AutonomError) as caught:
            profile_mod.load(self.workspace, APP)
        self.assertEqual(caught.exception.code, errors.PROFILE_INVALID)
        path.write_bytes(b"# " + b"x" * profile_mod.MAX_BYTES + b"\n")
        with self.assertRaises(errors.AutonomError) as caught:
            profile_mod.load(self.workspace, APP)
        self.assertEqual(caught.exception.code, errors.PROFILE_INVALID)
        path.unlink()
        path.mkdir()
        with self.assertRaises(errors.AutonomError) as caught:
            profile_mod.load(self.workspace, APP)
        self.assertEqual(caught.exception.code, errors.PROFILE_INVALID)
        self.assertTrue(caught.exception.hint)

    def test_unreadable_profile(self) -> None:
        path = self.put("workspace", "flaky_screens: [a]\n")
        with mock.patch.object(Path, "open", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(errors.AutonomError) as caught:
                profile_mod.read(path)
        self.assertEqual(caught.exception.code, errors.PROFILE_INVALID)
        self.assertTrue(caught.exception.hint)

    def test_deep_nesting_is_refused(self) -> None:
        deep = "fixtures:\n" + "".join("  " * (level + 1) + f"k{level}:\n"
                                       for level in range(profile_mod.MAX_DEPTH + 2))
        deep += "  " * (profile_mod.MAX_DEPTH + 3) + "leaf: x\n"
        exc = self.invalid(deep)
        self.assertIn("deep", exc.message)

    def test_empty_profile_is_valid(self) -> None:
        self.put("workspace", "# nothing yet\n")
        found = profile_mod.load(self.workspace, APP)
        self.assertEqual((found["events"], found["accounts"], found["login_flow"]), ([], {}, None))


# --- tp-025: accessibility rules ----------------------------------------------------------


class A11yRuleTests(unittest.TestCase):
    def test_android_fixture_gives_the_three_kinds(self) -> None:
        result = a11y.evaluate(nodes_of("a11y_android.xml"), "android", density=320, app_id=APP)
        rules = [issue["rule"] for issue in result["issues"]]
        self.assertEqual(sorted(rules), ["duplicate_label", "small_target", "unlabeled"])
        by_rule = {issue["rule"]: issue for issue in result["issues"]}
        self.assertEqual(by_rule["unlabeled"]["bounds"], [[40, 200, 240, 400]])
        self.assertEqual(by_rule["small_target"]["bounds"], [[900, 200, 960, 260]])
        self.assertEqual(by_rule["small_target"]["size"],
                         {"width": 30.0, "height": 30.0, "unit": "dp", "minimum": 48})
        self.assertEqual(by_rule["duplicate_label"]["bounds"],
                         [[40, 600, 520, 760], [560, 600, 1040, 760]])
        self.assertEqual(by_rule["duplicate_label"]["label"], "OK")
        # the system UI node and the merged "Continue" row are not issues
        self.assertEqual(result["controls"], 6)
        self.assertEqual(result["warnings"], [])

    def test_density_changes_the_size_rule(self) -> None:
        nodes = nodes_of("a11y_android.xml")
        loose = a11y.evaluate(nodes, "android", density=160, app_id=APP)
        self.assertNotIn("small_target", [issue["rule"] for issue in loose["issues"]])
        unknown = a11y.evaluate(nodes, "android", density=None, app_id=APP)
        self.assertNotIn("small_target", [issue["rule"] for issue in unknown["issues"]])
        self.assertEqual([item["code"] for item in unknown["warnings"]], ["density_unknown"])

    def test_ios_fixture_uses_points(self) -> None:
        result = a11y.evaluate(nodes_of("a11y_ios.json"), "ios")
        by_rule = {issue["rule"]: issue for issue in result["issues"]}
        self.assertEqual(set(by_rule), set(a11y.RULES))
        self.assertEqual(by_rule["small_target"]["size"]["unit"], "pt")
        self.assertEqual(by_rule["small_target"]["size"]["minimum"], 44)
        self.assertEqual(by_rule["unlabeled"]["bounds"], [[20, 200, 170, 250]])

    def test_nested_controls_are_not_duplicates(self) -> None:
        nodes = [
            {"ref": "n0", "parent": None, "bounds": [0, 0, 400, 400], "clickable": True,
             "desc": "Pay", "package": APP},
            {"ref": "n1", "parent": "n0", "bounds": [10, 10, 300, 300], "clickable": True,
             "text": "Pay", "package": APP},
            {"ref": "n2", "parent": None, "bounds": [0, 0, 0, 0], "clickable": True,
             "package": APP},
            {"ref": "n3", "parent": None, "bounds": [0, 500, 400, 700], "clickable": True,
             "text": "Hidden", "visible": False, "package": APP},
            {"ref": "n4", "parent": None, "bounds": [0, 800, 400, 900], "clickable": True,
             "package": "com.other.app"},
        ]
        result = a11y.evaluate(nodes, "android", density=160, app_id=APP)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["controls"], 2)

    def test_small_target_rounding_tolerance(self) -> None:
        """Fix round 3, item 14: 47.6 dp is a 48 dp target after rounding."""

        def control(ref: str, width: int, height: int, label: str) -> dict:
            top = int(ref[1:]) * 300
            return {"ref": ref, "parent": None, "bounds": [0, top, width, top + height],
                    "clickable": True, "text": label, "package": APP}

        # density 420: 125 px = 47.6 dp, 124 px = 47.2 dp, 126 px = 48.0 dp
        nodes = [control("n1", 125, 125, "Edge"), control("n2", 124, 300, "Short"),
                 control("n3", 126, 126, "Exact"), control("n4", 300, 124, "Flat")]
        result = a11y.evaluate(nodes, "android", density=420, app_id=APP)
        flagged = {issue["label"]: issue["size"] for issue in result["issues"]
                   if issue["rule"] == "small_target"}
        self.assertEqual(sorted(flagged), ["Flat", "Short"])
        self.assertEqual(flagged["Short"]["width"], 47.2)
        ios = [control("n1", 44, 44, "A"), control("n2", 43, 44, "B")]
        result = a11y.evaluate(ios, "ios")
        flagged = [issue["label"] for issue in result["issues"]
                   if issue["rule"] == "small_target"]
        self.assertEqual(flagged, ["B"])
        self.assertEqual(a11y.SIZE_TOLERANCE, 0.5)

    def test_parse_density(self) -> None:
        self.assertEqual(a11y.parse_density("Physical density: 420\n"), 420)
        self.assertEqual(a11y.parse_density("Physical density: 420\nOverride density: 320\n"),
                         320)
        self.assertIsNone(a11y.parse_density(""))
        self.assertIsNone(a11y.parse_density("Physical density: 0"))


# --- tp-026: event matching -----------------------------------------------------------------


class EventMatchTests(unittest.TestCase):
    def test_body_forms_and_values(self) -> None:
        parse = events_mod.parse_body
        self.assertEqual(parse('{"a": 1}'), {"a": 1})
        self.assertEqual(parse("a=1&b=two"), {"a": "1", "b": "two"})
        self.assertIsNone(parse("{broken"))
        self.assertIsNone(parse("plain text"))
        self.assertIsNone(parse(""))
        rule = {"name": "r", "body_key": "events.name", "body_value": "add"}
        body = {"events": [{"name": "view"}, {"name": "add"}]}
        self.assertTrue(events_mod.body_matches(rule, body))
        self.assertFalse(events_mod.body_matches({**rule, "body_value": "pay"}, body))
        self.assertTrue(events_mod.body_matches({"name": "r", "body_key": "events.name"}, body))
        self.assertFalse(events_mod.body_matches({"name": "r", "body_key": "missing"}, body))
        self.assertTrue(events_mod.body_matches(
            {"name": "r", "body_key": "ok", "body_value": "true"}, {"ok": True}))
        self.assertFalse(events_mod.body_matches({"name": "r", "body_key": "a"}, None))
        self.assertTrue(events_mod.body_matches({"name": "r"}, None))
        self.assertEqual(events_mod.body_keys([{"a": 1}, {"b": 2, "a": 3}]), ["a", "b"])

    def test_endpoint_rules(self) -> None:
        row = {"method": "post", "host": "Analytics.Example.com", "path": "/v1/events?x=1"}
        match = events_mod.endpoint_matches
        self.assertTrue(match({"method": "POST", "host": "analytics.example.com",
                               "path": "/v1/events"}, row))
        self.assertTrue(match({"host": "*.example.com"}, row))
        self.assertFalse(match({"method": "GET"}, row))
        self.assertFalse(match({"path": "/v2/*"}, row))
        self.assertFalse(match({"host": "example.org"}, row))

    def test_match_reports_ids_and_keys_never_values(self) -> None:
        rows = [json.loads(line) for line in fixture("events_flows.jsonl").splitlines()]
        rules = [{"name": "screen_view", "method": "POST", "host": "analytics.example.com",
                  "path": "/v1/events*", "body_key": "event", "body_value": "screen_view"},
                 {"name": "add_to_cart", "method": "POST", "host": "*.example.com",
                  "path": "/v1/events", "body_key": "event", "body_value": "add_to_cart"}]
        session = {"artifacts_dir": "/nonexistent"}
        result = events_mod.match(session, rules, rows[:3])
        self.assertEqual(result["matched"], [{"name": "screen_view", "count": 1,
                                              "request_ids": ["f_0001"]}])
        self.assertEqual([item["name"] for item in result["missing"]], ["add_to_cart"])
        self.assertEqual([item["request_id"] for item in result["unexpected"]], ["f_0002"])
        self.assertEqual(result["unexpected"][0]["body_keys"], ["event", "phone"])
        text = json.dumps(result, ensure_ascii=False)
        for value in PII:
            self.assertNotIn(value, text)


# --- tp-024: walk helpers ---------------------------------------------------------------------


class MapHelperTests(HomeCase):
    def test_a_map_that_is_not_a_map_is_never_rewritten(self) -> None:
        atlas = self.home / "apps" / APP / "atlas"
        atlas.mkdir(parents=True)
        graph = atlas / "graph.json"
        for text in ("[]", '{"screens": [], "transitions": {}}', "{broken"):
            with self.subTest(text=text):
                graph.write_text(text, encoding="utf-8")
                session = {"artifacts_dir": str(self.root / "artifacts"), "session_id": "s_x"}
                result = warmup_mod.update_map(session, APP)
                self.assertEqual(result["state"], "unavailable")
                self.assertEqual(graph.read_text("utf-8"), text)
                self.assertEqual(result["_graph"], {"screens": {}, "transitions": {}})
        graph.write_text('{"screens": ["a"], "transitions": {}}', encoding="utf-8")
        self.assertEqual(context_mod.runtime_map_hints(APP, ["lib/cart/a.dart"], []), [])


class WalkHelperTests(unittest.TestCase):
    def test_selector_of(self) -> None:
        detail = {"command": "tapOn", "detail": json.dumps(
            {"text": "Cart", "mode": "contains", "case_sensitive": False, "index": None})}
        self.assertEqual(warmup_mod.selector_of(detail),
                         {"fields": {"text": "Cart"}, "mode": "contains",
                          "case_sensitive": False, "index": None})
        flow = {"command": "tapOn", "detail": json.dumps({"id": "open_cart", "match": "exact"})}
        self.assertEqual(warmup_mod.selector_of(flow)["fields"], {"resource_id": "open_cart"})
        for edge in ({"command": "swipe", "detail": None},
                     {"command": "tapOn", "detail": None},
                     {"command": "tapOn", "detail": "{bad"},
                     {"command": "tapOn", "detail": json.dumps(
                         {"text": "A", "match": "exact", "below": {"text": "B"}})},
                     {"command": "tapOn", "detail": json.dumps({"mode": "contains"})},
                     {"command": "tapOn", "detail": json.dumps({"text": "A", "mode": "fuzzy"})},
                     {"command": "tapOn", "detail": json.dumps({"text": "A", "index": "1"})}):
            self.assertIsNone(warmup_mod.selector_of(edge), edge)

    def test_shortest_path_and_statuses(self) -> None:
        tap = json.dumps({"text": "Next", "mode": "contains"})
        graph = {"screens": {"a": {}, "b": {}, "c": {}, "d": {}},
                 "transitions": {
                     "a->b": {"from": "a", "to": "b", "command": "tapOn", "detail": tap,
                              "success": 2},
                     "b->c": {"from": "b", "to": "c", "command": "swipe", "detail": None,
                              "success": 1},
                     "a->d": {"from": "a", "to": "d", "command": "tapOn", "detail": tap,
                              "success": 0, "failure": 3}}}
        self.assertEqual(warmup_mod.shortest_path(graph, "a", "a"), [])
        self.assertEqual([edge["to"] for edge in warmup_mod.shortest_path(graph, "a", "c")],
                         ["b", "c"])
        self.assertIsNone(warmup_mod.shortest_path(graph, "a", "d"))  # never succeeded
        hints = [{"screen_id": sid, "labels": [], "matched_area": "lib", "hint": True}
                 for sid in ("a", "c", "d", "zzz")]
        with mock.patch.object(warmup_mod, "_screen", return_value=("a", [])):
            result = warmup_mod.walk({}, object(), graph, hints)
        statuses = {item["screen_id"]: item["status"] for item in result["hints"]}
        self.assertEqual(statuses, {"a": "reached", "c": "unreplayable", "d": "known",
                                    "zzz": "unknown"})
        self.assertEqual(result["counts"], {"known": 1, "reached": 1, "unreplayable": 1,
                                            "unknown": 1})
        skipped = warmup_mod.walk({}, object(), graph, hints, skip_reason="login failed")
        self.assertEqual({item["status"] for item in skipped["hints"]}, {"known", "unknown"})
        with mock.patch.object(warmup_mod, "_screen", side_effect=errors.AutonomError(
                errors.BACKEND_FAILED, "dump failed")):
            unread = warmup_mod.walk({}, object(), graph, hints[:1])
        self.assertEqual(unread["hints"][0]["status"], "known")
        self.assertIn("backend_failed", unread["hints"][0]["reason"])

    def test_a_damaged_pretest_refusal_stops_the_walk_like_a_spent_budget(self) -> None:
        tap = json.dumps({"text": "Next", "mode": "contains"})
        graph = {"screens": {"a": {}, "b": {}, "c": {}},
                 "transitions": {
                     "a->b": {"from": "a", "to": "b", "command": "tapOn", "detail": tap,
                              "success": 1},
                     "a->c": {"from": "a", "to": "c", "command": "tapOn", "detail": tap,
                              "success": 1}}}
        hints = [{"screen_id": sid, "labels": [], "matched_area": "lib", "hint": True}
                 for sid in ("b", "c")]
        for code in (errors.BUDGET_EXHAUSTED, errors.PRETEST_DAMAGED):
            with self.subTest(code=code):
                refusal = errors.AutonomError(code, f"{code} refused the tap")
                with mock.patch.object(warmup_mod, "_screen", return_value=("a", [])), \
                        mock.patch.object(warmup_mod, "_replay_tap",
                                          side_effect=refusal) as replay:
                    result = warmup_mod.walk({}, object(), graph, hints)
                self.assertEqual(replay.call_count, 1)  # the walk stopped at the refusal
                self.assertEqual(result["refusals"],
                                 [{"error_code": code, "error": f"{code} refused the tap",
                                   "screen_id": "b"}])
                first, second = result["hints"]
                self.assertEqual((first["status"], first["reason"], first["replayed"]),
                                 ("known", f"replay stopped: {code}", 0))
                self.assertEqual((second["status"], second["reason"]),
                                 ("known", f"walk stopped by {code}"))


# --- CLI ------------------------------------------------------------------------------------------


class CliCase(unittest.TestCase):
    """A private home, fake adb state and log, and a git repo whose working
    tree changes `lib/cart/cart_page.dart` and `lib/settings/page.dart`."""

    dump_name = "a11y_android.xml"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.log = self.root / "fake.log"
        self.state = self.root / "state.json"
        self.dumps = self.root / "dumps"
        self.dumps.mkdir()
        for name in ("a11y_android.xml", "warmup_login.xml", "warmup_home.xml",
                     "warmup_cart.xml"):
            shutil.copy(FIXTURES / name, self.dumps / name)
        self.write_state(self.dump_name)
        self.env = {**os.environ, "AUTONOM_HOME": str(self.home),
                    "AUTONOM_FAKE_STATE": str(self.state),
                    "AUTONOM_FAKE_LOG": str(self.log)}
        for key in ("AUTONOM_ADB", "AUTONOM_SIMCTL", "AUTONOM_IDB", "AUTONOM_SESSION_ID"):
            self.env.pop(key, None)
        self.repo = self.root / "repo"
        self.make_repo()
        self.ticket = self.root / "ticket.md"
        self.ticket.write_text(TICKET, encoding="utf-8")

    def write_state(self, dump: str, **extra) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(self.dumps / dump),
                 "getprop": {"ro.sf.lcd_density": "320"}}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def make_repo(self) -> None:
        for folder in ("lib/cart", "lib/settings"):
            (self.repo / folder).mkdir(parents=True)

        def git(*argv: str) -> None:
            subprocess.run(["git", *argv], cwd=self.repo, check=True,
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.repo / "lib/cart/cart_page.dart").write_text("v1\n", encoding="utf-8")
        (self.repo / "lib/settings/page.dart").write_text("v1\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        (self.repo / "lib/cart/cart_page.dart").write_text("v2\n", encoding="utf-8")
        (self.repo / "lib/settings/page.dart").write_text("v2\n", encoding="utf-8")

    def profile_dir(self) -> Path:
        path = self.repo / ".autonom/apps" / APP
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put_profile(self, name: str, *extra: str) -> Path:
        directory = self.profile_dir()
        path = directory / "profile.yaml"
        path.write_text(fixture(name), encoding="utf-8")
        for item in extra:
            shutil.copy(FIXTURES / item, directory / item)
        return path

    def cli(self, *argv: str) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--adb", str(FAKE_ADB), "--serial", SERIAL, *argv],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=180,
            stdin=subprocess.DEVNULL)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        return completed

    def ok(self, *argv: str) -> dict:
        completed = self.cli(*argv)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        return payload

    def refused(self, code: str, *argv: str) -> dict:
        completed = self.cli(*argv)
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], code, payload)
        self.assertTrue(payload.get("hint"), f"{code} has no hint")
        return payload

    def in_process(self, argv: list[str], *patches) -> tuple[int, dict]:
        cli = load_cli()
        env = {key: self.env[key] for key in
               ("AUTONOM_HOME", "AUTONOM_FAKE_STATE", "AUTONOM_FAKE_LOG")}
        out, err = StringIO(), StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(contextlib.chdir(self.root))
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = cli.main(["--adb", str(FAKE_ADB), "--serial", SERIAL, *argv])
        text = out.getvalue() if code == 0 else err.getvalue()
        return code, json.loads(text)

    def start_session(self) -> dict:
        self.ok("session", "start", "--app-id", APP)
        found = list((self.home / "sessions").glob("s_*/session.json"))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text(encoding="utf-8"))

    def start_pretest(self, *extra: str) -> dict:
        return self.ok("pretest", "start", "--ticket", str(self.ticket), "--base", "main",
                       "--repo", str(self.repo), *extra)

    def pretest_dir(self, session: dict) -> Path:
        found = sorted((Path(session["artifacts_dir"]) / "pretests").glob("pt_*"))
        self.assertTrue(found)
        return found[-1]

    def findings(self, session: dict) -> list[dict]:
        path = self.pretest_dir(session) / "findings.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]

    def trees(self, session: dict) -> list[Path]:
        return sorted((Path(session["artifacts_dir"]) / "trees").glob("[0-9]*.json"))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def device_inputs(self) -> int:
        return sum(1 for argv in self.calls()
                   if ("input" in argv and "tap" in argv) or ("am" in argv and "start" in argv))

    def budget(self, session: dict) -> dict:
        return json.loads((self.pretest_dir(session) / "budget.json").read_text("utf-8"))


class ProfileCliTests(CliCase):
    def test_context_reports_the_workspace_profile(self) -> None:
        self.start_session()
        home = self.home / "apps" / APP
        home.mkdir(parents=True)
        (home / "profile.yaml").write_text("flaky_screens: [Home]\n", encoding="utf-8")
        workspace = self.put_profile("profile_events.yaml")
        self.start_pretest()
        context = self.ok("pretest", "context")
        self.assertEqual(context["profile"]["path"], str(workspace.resolve()))
        self.assertEqual(context["profile"]["source"], "workspace")
        self.assertEqual(context["profile"]["accounts"]["qa"]["password"], "PRETEST_QA_PASSWORD")

    def test_no_profile_is_reported_as_null(self) -> None:
        self.start_session()
        self.start_pretest()
        self.assertIsNone(self.ok("pretest", "context")["profile"])

    def test_secret_profile_refuses_start_and_creates_nothing(self) -> None:
        session = self.start_session()
        self.put_profile("profile_secret.yaml")
        payload = self.refused("profile_invalid", "pretest", "start", "--ticket",
                               str(self.ticket), "--base", "main", "--repo", str(self.repo))
        self.assertNotIn("hunter2", json.dumps(payload))
        root = Path(session["artifacts_dir"]) / "pretests"
        self.assertEqual(sorted(root.glob("pt_*")) if root.is_dir() else [], [])
        journal = (Path(session["artifacts_dir"]) / "journal.ndjson").read_text("utf-8")
        self.assertNotIn("hunter2", journal)


class A11yCliTests(CliCase):
    def test_three_findings_with_bounds_and_tree_evidence(self) -> None:
        session = self.start_session()
        self.start_pretest()
        self.ok("pretest", "check", "add", "Cart screen is accessible", "--kind", "a11y")
        self.ok("pretest", "check", "begin", "chk-1")
        inputs = self.device_inputs()
        result = self.ok("pretest", "a11y")
        self.assertEqual(result["check_id"], "chk-1")
        self.assertEqual(result["density"], 320)
        self.assertEqual(result["counts"], {"unlabeled": 1, "small_target": 1,
                                            "duplicate_label": 1})
        found = self.findings(session)
        self.assertEqual(len(found), 3)
        tree = Path(session["artifacts_dir"]) / result["tree"]
        self.assertTrue(tree.is_file())
        self.assertEqual(stat.S_IMODE(tree.stat().st_mode), 0o600)
        saved = json.loads(tree.read_text("utf-8"))
        self.assertEqual(saved["check_id"], "chk-1")
        self.assertEqual(saved["task"], "chk-1")
        rules = set()
        for event in found:
            self.assertEqual((event["origin"], event["source"], event["severity"],
                              event["confirmed"], event["check_id"]),
                             ("auto", "a11y", "minor", False, "chk-1"))
            self.assertEqual(event["evidence"], [f"tree:{tree.name}"])
            self.assertTrue(event["data"]["bounds"])
            for box in event["data"]["bounds"]:
                self.assertIn(f"[{box[0]},{box[1]}][{box[2]},{box[3]}]", event["actual"])
            rules.add(event["data"]["rule"])
        self.assertEqual(rules, set(a11y.RULES))
        # the same screen in the same check adds nothing
        again = self.ok("pretest", "a11y", "--check", "chk-1")
        self.assertEqual((again["findings"], again["already_reported"]), ([], 3))
        self.assertEqual(len(self.findings(session)), 3)
        listed = self.ok("pretest", "finding", "list")
        self.assertEqual(listed["count"], 3)
        self.assertEqual(self.device_inputs(), inputs)  # read-only: nothing was tapped
        self.assertEqual(self.budget(session)["actions_used"], 0)

    def test_findings_and_tree_survive_the_pack(self) -> None:
        self.start_session()
        self.start_pretest()
        self.ok("pretest", "check", "add", "Screen is accessible", "--kind", "a11y")
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("pretest", "a11y")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "pass")
        pack = Path(self.ok("pretest", "pack")["pack"])
        self.assertTrue(self.ok("report", "verify", str(pack))["ok"])
        summary = json.loads((pack / "summary.json").read_text("utf-8"))
        sources = sorted(item["source"] for item in summary["findings"])
        self.assertEqual(sources, ["a11y", "a11y", "a11y"])
        self.assertTrue(all(item["confirmed"] is False for item in summary["findings"]))
        self.assertEqual(summary["verdict"], "pass")  # automatic findings never flip it
        kinds = {item["kind"] for item in summary["evidence"]}
        self.assertIn("tree", kinds)

    def test_without_density_the_size_rule_is_skipped(self) -> None:
        session = self.start_session()
        self.write_state(self.dump_name, getprop={})
        self.start_pretest()
        result = self.ok("pretest", "a11y")
        self.assertIsNone(result["check_id"])
        self.assertEqual(result["counts"]["small_target"], 0)
        self.assertEqual([item["code"] for item in result["warnings"]], ["density_unknown"])
        self.assertTrue(all(event["check_id"] is None for event in self.findings(session)))

    def test_unwritable_tree_writes_no_finding(self) -> None:
        session = self.start_session()
        self.start_pretest()
        code, payload = self.in_process(
            ["pretest", "a11y"],
            mock.patch.object(a11y, "save_tree", side_effect=PermissionError(13, "denied")))
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "output_not_writable")
        self.assertTrue(payload["hint"])
        self.assertEqual(self.findings(session), [])

    def test_refusals_write_nothing(self) -> None:
        self.refused("no_active_session", "pretest", "a11y")
        session = self.start_session()
        self.refused("no_active_pretest", "pretest", "a11y")
        self.start_pretest()
        self.refused("unknown_check", "pretest", "a11y", "--check", "chk-9")
        self.assertEqual(self.trees(session), [])
        self.assertEqual(self.findings(session), [])
        self.ok("pretest", "stop")
        self.refused("no_active_pretest", "pretest", "a11y")


class EventsCliTests(CliCase):
    def write_flows(self, session: dict, began: str) -> None:
        """Rows marked IN start in the window's first second, OUT ones five
        minutes before it."""
        start = datetime.fromisoformat(began.replace("Z", "+00:00"))
        inside = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        outside = (start - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        network = Path(session["artifacts_dir"]) / "network"
        network.mkdir(parents=True, exist_ok=True)
        lines = []
        for line in fixture("events_flows.jsonl").splitlines():
            row = json.loads(line)
            row["started_at"] = inside if row["started_at"] == "IN" else outside
            lines.append(json.dumps(row))
        (network / "flows.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def begin_check(self) -> str:
        self.ok("pretest", "check", "add", "Cart events", "--kind", "analytics")
        return self.ok("pretest", "check", "begin", "chk-1")["began_at"]

    def test_missing_event_becomes_a_finding_without_body_values(self) -> None:
        session = self.start_session()
        self.put_profile("profile_events.yaml")
        self.start_pretest()
        began = self.begin_check()
        self.write_flows(session, began)
        completed = self.cli("pretest", "events")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["check_id"], "chk-1")
        self.assertEqual(result["requests_in_window"], 3)
        self.assertEqual(result["matched"], [{"name": "screen_view", "count": 1,
                                              "request_ids": ["f_0001"]}])
        self.assertEqual([item["name"] for item in result["missing"]], ["add_to_cart"])
        self.assertEqual([(item["request_id"], item["body_keys"]) for item in
                          result["unexpected"]], [("f_0002", ["event", "phone"])])
        found = self.findings(session)
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual((event["origin"], event["source"], event["severity"],
                          event["confirmed"], event["check_id"]),
                         ("auto", "analytics", "minor", False, "chk-1"))
        self.assertIn("add_to_cart", event["title"])
        self.assertEqual(event["data"]["key"], "analytics:chk-1:add_to_cart")
        # PII: no body value anywhere the run wrote
        journal = (Path(session["artifacts_dir"]) / "journal.ndjson").read_text("utf-8")
        findings_text = (self.pretest_dir(session) / "findings.ndjson").read_text("utf-8")
        for value in PII:
            for name, text in (("stdout", completed.stdout), ("findings", findings_text),
                               ("journal", journal)):
                self.assertNotIn(value, text, f"{value!r} leaked into {name}")
        # idempotent per check and rule
        self.ok("pretest", "events", "--check", "chk-1")
        self.assertEqual(len(self.findings(session)), 1)

    def test_whole_pretest_window_without_a_check(self) -> None:
        session = self.start_session()
        self.put_profile("profile_events.yaml")
        started = self.start_pretest()
        self.write_flows(session, started["created_at"])
        result = self.ok("pretest", "events")
        self.assertIsNone(result["check_id"])
        self.assertEqual(self.findings(session)[0]["data"]["key"], "analytics:-:add_to_cart")
        self.assertIsNone(self.findings(session)[0]["check_id"])

    def test_without_capture_the_check_is_unavailable(self) -> None:
        session = self.start_session()
        self.put_profile("profile_events.yaml")
        self.start_pretest()
        self.begin_check()
        result = self.ok("pretest", "events")
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual([item["code"] for item in result["warnings"]],
                         ["network_not_captured"])
        self.assertEqual(self.findings(session), [])

    def test_unreadable_capture_is_unavailable(self) -> None:
        session = self.start_session()
        self.put_profile("profile_events.yaml")
        started = self.start_pretest()
        self.write_flows(session, started["created_at"])
        code, result = self.in_process(
            ["pretest", "events"],
            mock.patch.object(events_mod.network_store, "read_all",
                              side_effect=PermissionError(13, "denied")))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual([item["code"] for item in result["warnings"]], ["network_unreadable"])
        self.assertEqual(self.findings(session), [])

    def test_refusals_write_nothing(self) -> None:
        session = self.start_session()
        self.start_pretest()
        payload = self.refused("invalid_value", "pretest", "events")
        self.assertIn("profile.yaml", payload["hint"])
        self.put_profile("profile_warmup.yaml")
        self.refused("invalid_value", "pretest", "events")
        self.put_profile("profile_events.yaml")
        self.ok("pretest", "check", "add", "Later", "--kind", "analytics")
        self.refused("invalid_value", "pretest", "events", "--check", "chk-1")
        self.refused("unknown_check", "pretest", "events", "--check", "chk-7")
        self.put_profile("profile_secret.yaml")
        payload = self.refused("profile_invalid", "pretest", "events")
        self.assertNotIn("hunter2", json.dumps(payload))
        self.assertEqual(self.findings(session), [])


class WarmupCliTests(CliCase):
    dump_name = "warmup_login.xml"
    broken_map = False

    def seed_map(self) -> dict[str, str]:
        """A Runtime Map where home -> cart is a recorded selector tap and
        home -> settings a swipe."""
        ids = {name: screen_id(f"warmup_{name}.xml") for name in ("login", "home", "cart")}
        ids["settings"] = "scr_settings"
        tap = json.dumps({"text": "Cart", "mode": "contains", "case_sensitive": False,
                          "index": None})
        seen = "2026-10-01T10:00:00Z"

        def screen(sid: str, labels: list[str]) -> dict:
            return {"screen_id": sid, "labels": labels, "first_seen": seen, "last_seen": seen,
                    "variants": {}, "sessions": []}

        def edge(source: str, to: str, command: str, detail: str | None) -> dict:
            return {"from": source, "to": to, "command": command, "detail": detail,
                    "success": 1, "failure": 0, "first_seen": seen, "last_seen": seen,
                    "evidence": []}

        graph = {
            "schema_version": 1, "app_id": APP, "updated_at": None,
            "screens": {
                ids["home"]: screen(ids["home"], ["Home"]),
                ids["cart"]: screen(ids["cart"], ["Your cart", "Checkout"]),
                ids["settings"]: screen(ids["settings"], ["Settings page"]),
            },
            "transitions": {
                f"{ids['home']}->{ids['cart']}::tapOn":
                    edge(ids["home"], ids["cart"], "tapOn", tap),
                f"{ids['home']}->{ids['settings']}::swipe":
                    edge(ids["home"], ids["settings"], "swipe", None),
            }}
        if self.broken_map:
            graph["screens"][ids["home"]].pop("variants")
        atlas = self.home / "apps" / APP / "atlas"
        atlas.mkdir(parents=True)
        (atlas / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
        return ids

    def tap_moves_to_cart(self):
        original = ui_android.tap
        cart = str(self.dumps / "warmup_cart.xml")
        state_path = self.state

        def tap(adb, serial, x, y):
            original(adb, serial, x, y)
            state = json.loads(state_path.read_text("utf-8"))
            state["ui_dump"] = cart
            state_path.write_text(json.dumps(state), encoding="utf-8")

        return mock.patch.object(ui_android, "tap", side_effect=tap)

    def prepare(self, *start: str) -> tuple[dict, dict[str, str]]:
        self.write_state("warmup_login.xml",
                         ui_dump_after_launch=str(self.dumps / "warmup_home.xml"))
        session = self.start_session()
        ids = self.seed_map()
        self.put_profile("profile_warmup.yaml", "login.yaml")
        self.start_pretest(*start)
        # session start may have launched the app: begin from the login screen
        self.write_state("warmup_login.xml",
                         ui_dump_after_launch=str(self.dumps / "warmup_home.xml"))
        return session, ids

    def test_login_then_walk_reaches_the_hinted_screen(self) -> None:
        session, ids = self.prepare()
        hints = {item["screen_id"] for item in
                 self.ok("pretest", "context")["runtime_map_hints"]}
        self.assertEqual(hints, {ids["cart"], ids["settings"]})
        with mock.patch.dict(os.environ, {"PRETEST_WARMUP_QA_USER": "qa-user-value"}):
            code, result = self.in_process(["pretest", "warmup"], self.tap_moves_to_cart())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["profile"]["source"], "workspace")
        self.assertEqual([(run["role"], run["status"]) for run in result["runs"]],
                         [("login_flow", "passed")])
        self.assertTrue(result["runs"][0]["run_id"])
        statuses = {item["screen_id"]: item["status"] for item in result["hints"]}
        self.assertEqual(statuses[ids["cart"]], "reached")
        self.assertEqual(statuses[ids["settings"]], "unreplayable")
        cart = next(item for item in result["hints"] if item["screen_id"] == ids["cart"])
        self.assertEqual(cart["replayed"], 1)
        self.assertEqual(cart["path"][0]["selector"], {"text": "Cart"})
        self.assertEqual(result["current_screen"], ids["cart"])
        self.assertGreaterEqual(result["runtime_map"]["runs_ingested"], 1)
        # warm-up actions count: the launch and the replayed tap
        self.assertEqual(self.budget(session)["actions_used"], 2)
        # the account variable reached the run as a secret, never a file
        artifacts = Path(session["artifacts_dir"])
        for path in artifacts.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"qa-user-value", path.read_bytes(), str(path))

    def test_budget_refusal_stops_the_walk(self) -> None:
        session, ids = self.prepare("--max-actions", "1")
        code, result = self.in_process(["pretest", "warmup"], self.tap_moves_to_cart())
        self.assertEqual(code, 0, result)
        cart = next(item for item in result["hints"] if item["screen_id"] == ids["cart"])
        self.assertEqual(cart["status"], "known")
        self.assertIn("budget_exhausted", cart["reason"])
        self.assertEqual(result["refusals"][0]["error_code"], "budget_exhausted")
        self.assertEqual(sum(1 for argv in self.calls() if "input" in argv and "tap" in argv), 0)

    def test_failed_login_skips_the_walk(self) -> None:
        session, ids = self.prepare()
        self.write_state("warmup_login.xml")  # the launch never reaches Home
        (self.profile_dir() / "login.yaml").write_text(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: Login\n---\n"
            "- launchApp\n- assertVisible:\n    selector:\n      text: Home\n"
            "    timeoutMs: 300\n", encoding="utf-8")
        (self.profile_dir() / "profile.yaml").write_text(
            "login_flow: login.yaml\nwarmup_flow: login.yaml\n", encoding="utf-8")
        code, result = self.in_process(["pretest", "warmup"])
        self.assertEqual(code, 0, result)
        self.assertEqual([run["status"] for run in result["runs"]], ["failed", "skipped"])
        self.assertTrue(all(item["status"] in ("known", "unknown") for item in result["hints"]))
        self.assertIn("warmup_incomplete", [item["code"] for item in result["warnings"]])

    def test_refusals_touch_nothing(self) -> None:
        session, ids = self.prepare()
        before = self.device_inputs()
        self.ok("pretest", "check", "add", "Open", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        payload = self.refused("check_already_open", "pretest", "warmup")
        self.assertEqual(payload["open_check_id"], "chk-1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "pass")
        (self.profile_dir() / "login.yaml").unlink()
        self.refused("flow_file_not_found", "pretest", "warmup")
        (self.profile_dir() / "profile.yaml").write_text(fixture("profile_secret.yaml"),
                                                         encoding="utf-8")
        payload = self.refused("profile_invalid", "pretest", "warmup")
        self.assertNotIn("hunter2", json.dumps(payload))
        self.assertEqual(self.device_inputs(), before)
        self.assertEqual(self.budget(session)["actions_used"], 0)

    def test_a_damaged_map_is_not_rewritten(self) -> None:
        self.broken_map = True
        session, ids = self.prepare()
        graph = self.home / "apps" / APP / "atlas/graph.json"
        before = graph.read_bytes()
        code, result = self.in_process(["pretest", "warmup"], self.tap_moves_to_cart())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["runtime_map"]["state"], "unavailable")
        self.assertIn("runtime_map_not_updated", [item["code"] for item in result["warnings"]])
        self.assertEqual(graph.read_bytes(), before)
        statuses = {item["screen_id"]: item["status"] for item in result["hints"]}
        self.assertEqual(statuses[ids["cart"]], "reached")

    def test_without_a_profile_only_the_walk_runs(self) -> None:
        self.write_state("warmup_home.xml")
        self.start_session()
        ids = self.seed_map()
        self.start_pretest()
        code, result = self.in_process(["pretest", "warmup"], self.tap_moves_to_cart())
        self.assertEqual(code, 0, result)
        self.assertIsNone(result["profile"])
        self.assertEqual(result["runs"], [])
        self.assertIn("no_app_profile", [item["code"] for item in result["warnings"]])
        statuses = {item["screen_id"]: item["status"] for item in result["hints"]}
        self.assertEqual(statuses[ids["cart"]], "reached")


if __name__ == "__main__":
    unittest.main()
