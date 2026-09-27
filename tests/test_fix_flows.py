"""Flow defects found by live WoolBox testing, each pinned by a regression test.

1. `flow create --from-session` read journal argv by position: `open <url>
   --serial S` compiled to `openLink: S`, a resume `session launch` to the
   (fresh since PR #10) bare `launchApp`, and `session uninstall` vanished
   while the report said `skipped: 0`. `flow check` now refuses an openLink
   URL without a scheme.
2. `flow check` said `platforms: [android, ios]` for a flow using `back`,
   which iOS refuses; inference and the iOS pre-flight now share one table.
3. `waitForSettled` — a tap fired into a sheet that was still animating.
4. Maestro export refused the suite's standard `tapOn.timeoutMs`.
5. `report suite --last N` counted runs, so a re-run pushed a flow out.
6. Repair advice said "text on Android", wrong for Flutter.

Everything runs against fakes and temp directories — never a device.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.flow import compiler  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import maestro  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import repair  # noqa: E402
from autonom_lib.flow import report as flow_report  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.flow import validator  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"


def _build(body: str, head: str = _HEAD) -> flow_schema.Flow:
    return flow_schema.build_flow(flow_parser.parse_document(head + body, "t.yaml"))


def _import(text: str) -> flow_schema.Flow:
    return flow_schema.build_flow(flow_parser.parse_document(
        maestro.import_flow(text, "m.yaml"), "m.yaml"))


class _TempDir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # resolved: validate_tree reports the real path (/private/var on macOS)
        self.root = Path(tmp.name).resolve()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


# --- 1. Session -> Flow compiler --------------------------------------------


class CompilerArgvTests(_TempDir):
    """Journal argv exactly as the WoolBox Android sessions recorded it."""

    SERIAL = "emulator-5556"

    _VERBS = ("session launch", "session uninstall", "session clear", "ui key",
              "location set", "open", "permissions")

    def _verb(self, argv: list[str]) -> str:
        # the journal stores the parser's verb next to the argv
        joined = " ".join(argv)
        return next(verb for verb in self._VERBS
                    if f" {verb} " in f" {joined} ")

    def _session(self, argvs: list[list[str]], *, find: bool = True,
                 platform: str | None = None,
                 entry_platform: str | None = None) -> dict:
        session = {"session_id": "s_test", "artifacts_dir": str(self.root),
                   "app_id": "com.example.app"}
        if platform:
            session["platform"] = platform
        lines = []
        for seq, argv in enumerate(argvs, start=1):
            entry = {"seq": seq, "kind": "action", "verb": self._verb(argv),
                     "argv": argv, "ok": True, "origin": "agent"}
            if entry_platform:  # what every target verb records in `result`
                entry["result"] = {"target_id": "T", "platform": entry_platform}
            lines.append(json.dumps(entry))
        (self.root / "journal.ndjson").write_text("\n".join(lines) + "\n",
                                                  encoding="utf-8")
        if find:  # a verifying `ui find` so the flow has a closing assertion
            actions = self.root / "actions"
            actions.mkdir(exist_ok=True)
            (actions / "0001_find.json").write_text(json.dumps({
                "kind": "find", "count": 1,
                "selector": {"desc": "Catalog", "mode": "exact"}}),
                encoding="utf-8")
        return session

    def _compile(self, argvs: list[list[str]]):
        return compiler.compile_session(self._session(argvs), name="walk")

    def test_open_takes_the_url_not_the_serial_after_it(self) -> None:
        flow, _report = self._compile([
            ["open", "woolbox://catalog", "--serial", self.SERIAL],
            ["open", "--serial", self.SERIAL, "https://woolbox.example/catalog"],
            ["--serial", self.SERIAL, "open", "woolbox://feed"],
            ["open", "woolbox://profile", "--ser", self.SERIAL],
            ["open", "woolbox://settings", f"--serial={self.SERIAL}"],
        ])
        urls = [step.args["url"] for step in flow.steps
                if step.command == "openLink"]
        self.assertEqual(urls, ["woolbox://catalog",
                                "https://woolbox.example/catalog",
                                "woolbox://feed", "woolbox://profile",
                                "woolbox://settings"])

    def test_resume_launch_compiles_to_launch_app_resume(self) -> None:
        flow, _report = self._compile([
            ["session", "launch", "com.example.app", "--serial", self.SERIAL],
            ["session", "launch", "com.example.app", "--fresh",
             "--serial", self.SERIAL],
        ])
        launches = [step.args for step in flow.steps
                    if step.command == "launchApp"]
        self.assertEqual(launches, [{"resume": True}, {}])
        text = compiler.emit_flow(flow)
        self.assertIn("- launchApp:\n    resume: true\n", text)

    def test_uninstall_is_counted_as_skipped_with_a_reason(self) -> None:
        flow, report = self._compile([
            ["session", "launch", "com.example.app", "--serial", self.SERIAL],
            ["session", "uninstall", "com.example.app", "--serial", self.SERIAL],
        ])
        codes = [(w["code"], w.get("seq")) for w in report["warnings"]]
        self.assertIn(("uninstall_not_compilable", 2), codes)
        self.assertEqual(report["quality"]["skipped"], 1)
        self.assertTrue(report["review_required"])
        self.assertNotIn("uninstall", compiler.emit_flow(flow))

    def test_launch_options_without_a_flow_form_are_reported(self) -> None:
        flow, report = self._compile([
            ["session", "launch", "com.example.app", "--setenv", "***",
             "--arg", "-demo", "--serial", self.SERIAL],
            ["session", "launch", "com.example.app", "--activity", ".Main"],
            ["session", "launch", "com.example.app", "--fresh"],
        ])
        self.assertEqual([step.args for step in flow.steps
                          if step.command == "launchApp"],
                         [{"resume": True}, {"resume": True}, {}],
                         "the launches still compile")
        dropped = [(w["seq"], w["options"]) for w in report["warnings"]
                   if w["code"] == "launch_args_not_compilable"]
        self.assertEqual(dropped, [(1, ["--arg", "--setenv"]), (2, ["--activity"])])
        self.assertEqual(report["quality"]["skipped"], 2)
        self.assertNotIn("***", json.dumps(report["warnings"]))

    def test_a_clear_recorded_on_ios_is_skipped_not_compiled(self) -> None:
        argvs = [["--platform", "ios", "--target", "T", "session", "clear",
                  "com.example.app"],
                 ["--platform", "ios", "--target", "T", "session", "launch",
                  "com.example.app", "--fresh"]]
        for where in ({"entry_platform": "ios"}, {"platform": "ios"}):
            with self.subTest(where=where):
                session = self._session(argvs, **where)  # rewrites the journal
                flow, report = compiler.compile_session(session, name="walk")
                self.assertNotIn("clearState", [s.command for s in flow.steps])
                warning = next(w for w in report["warnings"]
                               if w["code"] == "clear_state_not_compilable_on_ios")
                self.assertEqual(warning["seq"], 1)
                self.assertIn("setup", warning["error"])
                self.assertEqual(report["quality"]["skipped"], 1)
                path = self._write("ios.yaml", compiler.emit_flow(flow))
                self.assertEqual(validator.flow_platforms(
                    validator.validate_tree(path)), ["android", "ios"])
        android, _report = self._compile([["session", "clear", "com.example.app",
                                           "--serial", self.SERIAL]])
        self.assertIn("clearState", [s.command for s in android.steps])

    def test_trailing_target_flags_never_become_positionals(self) -> None:
        flow, _report = self._compile([
            ["ui", "key", "KEYCODE_HOME", "--serial", self.SERIAL],
            ["location", "set", "55.751244,37.618423", "--serial", self.SERIAL],
            ["location", "set", "--serial", self.SERIAL, "--", "-33.86,151.2"],
            ["permissions", "--serial", self.SERIAL, "grant",
             "android.permission.POST_NOTIFICATIONS", "com.example.app"],
        ])
        steps = [(step.command, step.args) for step in flow.steps
                 if step.command != "assertVisible"]
        self.assertEqual(steps, [
            ("pressKey", {"key": "KEYCODE_HOME"}),
            ("setLocation", {"latitude": 55.751244, "longitude": 37.618423}),
            ("setLocation", {"latitude": -33.86, "longitude": 151.2}),
            ("setPermissions", {"action": "grant",
                                "service": "android.permission.POST_NOTIFICATIONS",
                                "appId": "com.example.app"}),
        ])

    def test_a_schemeless_open_is_skipped_not_emitted(self) -> None:
        flow, report = self._compile([["open", "example.com/path",
                                       "--serial", self.SERIAL],
                                      ["open", "woolbox://ok"]])
        self.assertEqual([step.args["url"] for step in flow.steps
                          if step.command == "openLink"], ["woolbox://ok"])
        self.assertIn("open_url_not_compilable",
                      {w["code"] for w in report["warnings"]})
        self.assertEqual(report["quality"]["skipped"], 1)

    def test_the_recorded_walk_passes_flow_check(self) -> None:
        text, _report = compiler.compile_to_text(self._session([
            ["session", "launch", "com.example.app", "--serial", self.SERIAL],
            ["open", "woolbox://catalog", "--serial", self.SERIAL],
            ["session", "uninstall", "com.example.app", "--serial", self.SERIAL],
        ]), name="walk")
        path = self._write("recorded.yaml", text)
        flow = validator.validate_tree(path)
        self.assertEqual(flow.steps[1].args["url"], "woolbox://catalog")


class CompilerMatchesTheRealParserTests(unittest.TestCase):
    """The compiler's option table against scripts/autonom.py's argparse."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = importlib.import_module("autonom").build_parser()

    def _leaf(self, words: list[str]) -> argparse.ArgumentParser:
        parser = self.parser
        for word in words:
            subs = [action for action in parser._actions  # noqa: SLF001
                    if isinstance(action, argparse._SubParsersAction)]  # noqa: SLF001
            parser = subs[0].choices[word]
        return parser

    @staticmethod
    def _options(parser: argparse.ArgumentParser) -> tuple[set, set]:
        values, switches = set(), set()
        for action in parser._actions:  # noqa: SLF001 - read-only introspection
            for option in action.option_strings:
                if not option.startswith("--") or option == "--help":
                    continue
                (switches if action.nargs == 0 else values).add(option)
        return values, switches

    def test_option_table_matches_every_compiled_verb(self) -> None:
        targets = set(compiler._TARGET_VALUE_FLAGS)  # noqa: SLF001
        global_values, _ = self._options(self.parser)
        self.assertLessEqual(global_values, targets)
        for verb, (values, switches) in compiler._VERB_OPTIONS.items():  # noqa: SLF001
            with self.subTest(verb=verb):
                leaf_values, leaf_switches = self._options(self._leaf(verb.split()))
                self.assertEqual(leaf_values - targets, set(values))
                self.assertEqual(leaf_switches, set(switches))

    def _parse(self, argv: list[str]):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.parser.parse_args(argv)

    def test_positionals_agree_with_argparse(self) -> None:
        cases = [
            (["open", "woolbox://catalog", "--serial", "S"], "open", ["url"]),
            (["--serial", "S", "open", "https://x.test/a"], "open", ["url"]),
            (["open", "--ser", "S", "woolbox://x"], "open", ["url"]),
            (["open", "woolbox://x", "--serial=S"], "open", ["url"]),
            (["session", "launch", "a.b", "--fresh", "--serial", "S"],
             "session launch", ["app_id"]),
            (["session", "launch", "--activity", ".Main", "a.b"],
             "session launch", ["app_id"]),
            (["session", "uninstall", "a.b", "--serial", "S"],
             "session uninstall", ["app_id"]),
            (["ui", "key", "--serial", "S", "KEYCODE_HOME"], "ui key", ["keycode"]),
            (["location", "set", "55.7,37.6", "--serial", "S"],
             "location set", ["coordinates"]),
            (["permissions", "--serial", "S", "grant", "svc", "a.b"],
             "permissions", ["action", "service", "app_id"]),
            (["session", "clear", "a.b", "--strategy", "privacy"],
             "session clear", ["app_id"]),
        ]
        for argv, verb, dests in cases:
            with self.subTest(argv=argv):
                namespace = self._parse(argv)
                positionals, options = compiler._parse_argv(verb, argv)  # noqa: SLF001
                self.assertEqual(positionals,
                                 [getattr(namespace, dest) for dest in dests])
                if verb == "session launch":
                    self.assertEqual("--fresh" in options, namespace.fresh)


# --- flow check: openLink needs a scheme -------------------------------------


class OpenLinkSchemeTests(_TempDir):
    def test_flow_check_refuses_a_url_without_a_scheme(self) -> None:
        path = self._write("f.yaml", _HEAD + "- launchApp\n"
                                             "- openLink: emulator-5556\n")
        with self.assertRaises(errors.AutonomError) as caught:
            validator.validate_tree(path)
        self.assertEqual(caught.exception.code, errors.FLOW_COMMAND_INVALID)
        self.assertEqual(caught.exception.extra["line"], 6)
        self.assertEqual(caught.exception.extra["file"], str(path))
        self.assertIn("scheme", caught.exception.message)

    def test_nested_links_are_checked_too(self) -> None:
        path = self._write("f.yaml", _HEAD + "- retry:\n    commands:\n"
                                             "      - openLink: catalog/filters\n")
        with self.assertRaises(errors.AutonomError) as caught:
            validator.validate_tree(path)
        self.assertEqual(caught.exception.extra["line"], 7)

    def test_absolute_and_runtime_urls_pass(self) -> None:
        path = self._write("f.yaml", _HEAD
                           + "- openLink: woolbox://catalog\n"
                           + "- openLink: https://woolbox.example/catalog/filters\n"
                           + "- openLink: ${DEEPLINK}\n"
                           + "- openLink: mailto:help@example.com\n")
        self.assertEqual(len(validator.validate_tree(path).steps), 4)


# --- 2. Platform inference ----------------------------------------------------


class PlatformInferenceTests(_TempDir):
    def _platforms(self, body: str, head: str = _HEAD) -> list[str]:
        return validator.flow_platforms(
            validator.validate_tree(self._write("f.yaml", head + body)))

    def test_back_excludes_ios(self) -> None:
        self.assertEqual(self._platforms("- launchApp\n- back\n"), ["android"])
        self.assertEqual(self._platforms("- launchApp\n- tapOn: Go\n"),
                         ["android", "ios"])

    def test_every_android_only_step_counts(self) -> None:
        for body in ("- clearState\n", "- setOrientation: landscape\n",
                     "- launchApp:\n    clearState: true\n",
                     "- pressKey: KEYCODE_BACK\n",
                     "- group:\n    label: g\n    commands:\n      - back\n"):
            with self.subTest(body=body):
                self.assertEqual(self._platforms(body), ["android"])
        for body in ("- pressKey: HOME\n", "- pressKey: ${KEY}\n",
                     "- launchApp:\n    resume: true\n"):
            with self.subTest(body=body):
                self.assertEqual(self._platforms(body), ["android", "ios"])

    def test_a_platform_guarded_branch_does_not_count(self) -> None:
        self.assertEqual(self._platforms(
            "- runFlow:\n    when:\n      platform: android\n"
            "    commands:\n      - back\n"), ["android", "ios"])

    def test_runflow_children_count(self) -> None:
        self._write("sub.yaml", "schema: autonom.dev/flow/v1\nname: child\n---\n"
                                "- back\n")
        self.assertEqual(self._platforms("- runFlow: sub.yaml\n"), ["android"])

    def test_load_flow_without_children_still_infers_its_own_steps(self) -> None:
        flow = validator.load_flow(self._write("f.yaml", _HEAD + "- back\n"))
        self.assertEqual(validator.flow_platforms(flow), ["android"])

    def test_declared_ios_with_back_is_refused_at_the_step(self) -> None:
        head = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n"
                "requires:\n  platform: [android, ios]\n---\n")
        path = self._write("f.yaml", head + "- launchApp\n- back\n")
        with self.assertRaises(errors.AutonomError) as caught:
            validator.validate_tree(path)
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(caught.exception.extra["line"], 8)
        self.assertEqual(caught.exception.extra["file"], str(path))
        android = head.replace("[android, ios]", "[android]")
        self.assertEqual(self._platforms("- back\n", head=android), ["android"])


class IosPreflightTests(EnvSandboxMixin, _TempDir):
    def setUp(self) -> None:
        super().setUp()
        self.sandbox_home()

    def _runner(self, **config) -> flow_executor.Executor:
        target = Target("ios", UDID, "xcrun", {"udid": UDID})
        session = {"session_id": "s_test", "artifacts_dir": str(self.root),
                   "tooling": {"idb": {"state": "ready"}}}
        return flow_executor.Executor(target, session,
                                      flow_executor.RunConfig(**config),
                                      clock=lambda: 0.0, sleep=lambda _s: None,
                                      screen=(390, 844))

    def test_back_is_refused_before_any_device_action_with_file_and_line(self) -> None:
        path = self._write("f.yaml", _HEAD + "- tapOn: Go\n- back\n")
        flow = validator.validate_tree(path)
        with mock.patch("autonom_lib.ui.snapshot") as snapshot, \
             mock.patch("autonom_lib.ui.press_key") as press:
            with self.assertRaises(errors.AutonomError) as caught:
                self._runner().run(flow)
        snapshot.assert_not_called()
        press.assert_not_called()
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(caught.exception.extra["line"], 6)
        self.assertEqual(caught.exception.extra["file"], str(path))
        self.assertIn(f"{path}:6:", caught.exception.message)

    def test_android_keycodes_are_refused_up_front(self) -> None:
        flow = validator.validate_tree(
            self._write("f.yaml", _HEAD + "- pressKey: KEYCODE_ENTER\n"))
        with mock.patch("autonom_lib.ui.press_key") as press:
            with self.assertRaises(errors.AutonomError) as caught:
                self._runner().run(flow)
        press.assert_not_called()
        self.assertEqual(caught.exception.code,
                         errors.UNSUPPORTED_KEY_FOR_PLATFORM)

    def test_an_android_guarded_back_does_not_block_ios(self) -> None:
        flow = validator.validate_tree(self._write(
            "f.yaml", _HEAD + "- runFlow:\n    when:\n      platform: android\n"
                              "    commands:\n      - back\n"))
        result = self._runner(dry_run=True).run(flow)
        self.assertEqual(result.status, "passed")


# --- 3. waitForSettled ----------------------------------------------------------


class WaitForSettledSchemaTests(unittest.TestCase):
    def test_bare_and_with_arguments(self) -> None:
        flow = _build("- waitForSettled\n"
                      "- waitForSettled:\n    timeoutMs: 3000\n    quietMs: 300\n")
        self.assertEqual([(s.command, s.args) for s in flow.steps],
                         [("waitForSettled", {}),
                          ("waitForSettled", {"timeoutMs": 3000, "quietMs": 300})])
        self.assertFalse(flow_schema.REGISTRY["waitForSettled"].mutating)

    def test_the_default_quiet_window_fits_a_short_timeout(self) -> None:
        flow = _build("- waitForSettled:\n    timeoutMs: 300\n"
                      "- waitForSettled:\n    timeoutMs: 0\n")
        self.assertEqual([flow_schema.settle_window(s.args) for s in flow.steps],
                         [(300, 300), (0, 0)])
        imported = _import("appId: a.b\n---\n- waitForAnimationToEnd:\n"
                           "    timeout: 300\n")
        self.assertEqual(imported.steps[0].args, {"timeoutMs": 300})
        self.assertEqual(flow_schema.settle_window({}), (5000, 500))

    def test_impossible_windows_are_refused_at_check_time(self) -> None:
        for body in ("- waitForSettled:\n    quietMs: -1\n",
                     "- waitForSettled:\n    timeoutMs: 400\n    quietMs: 800\n",
                     "- waitForSettled:\n    quietMs: 6000\n"):
            with self.subTest(body=body):
                with self.assertRaises(errors.AutonomError) as caught:
                    _build(body)
                self.assertEqual(caught.exception.code,
                                 errors.FLOW_COMMAND_INVALID)
                self.assertIn("line", caught.exception.extra)


class WaitForSettledRunTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.sandbox_home()

    def _runner(self) -> flow_executor.Executor:
        target = Target("android", "emulator-5554", str(FAKE_ADB),
                        {"serial": "emulator-5554"})
        session = {"session_id": "s_test", "artifacts_dir": str(self.tmp)}
        clock = [0.0]
        return flow_executor.Executor(
            target, session, flow_executor.RunConfig(default_timeout_ms=2000),
            clock=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            screen=(1080, 1920))

    @staticmethod
    def _sheet(top: int) -> list[dict]:
        return [{"ref": "n1", "resource_id": "apply", "text": "Apply",
                 "bounds": [100, top, 500, top + 100], "enabled": True}]

    def test_the_tap_waits_for_the_sheet_to_stop_moving(self) -> None:
        # a bottom sheet sliding up: the button's bounds change until it rests
        frames = [self._sheet(top) for top in (1800, 1500, 1200)]
        resting = self._sheet(900)

        def snapshot(_target):
            return frames.pop(0) if frames else resting

        flow = _build("- waitForSettled\n"
                      "- tapOn:\n    selector:\n      id: apply\n")
        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot), \
             mock.patch("autonom_lib.ui.tap") as tap:
            result = self._runner().run(flow)
        self.assertEqual(result.status, "passed")
        tap.assert_called_once()
        self.assertEqual(tap.call_args.args[1:3], (300, 950),
                         "the tap must land on the resting button")
        settle = result.steps[0]
        self.assertEqual((settle.command, settle.settled), ("waitForSettled", True))
        self.assertEqual(result.warnings, [])

    def test_zero_timeout_is_one_honest_snapshot(self) -> None:
        flow = _build("- waitForSettled:\n    timeoutMs: 0\n")
        with mock.patch("autonom_lib.ui.snapshot",
                        return_value=self._sheet(900)) as snapshot:
            result = self._runner().run(flow)
        self.assertEqual(result.status, "passed")
        self.assertLessEqual(snapshot.call_count, 1)
        self.assertIs(result.steps[0].settled, False,
                      "one frame cannot prove the screen is still")

    def test_a_short_timeout_can_settle(self) -> None:
        flow = _build("- waitForSettled:\n    timeoutMs: 300\n")
        with mock.patch("autonom_lib.ui.snapshot", return_value=self._sheet(900)):
            result = self._runner().run(flow)
        self.assertIs(result.steps[0].settled, True)
        self.assertEqual(result.warnings, [])

    def test_a_screen_that_never_settles_is_reported_not_failed(self) -> None:
        counter = [0]

        def snapshot(_target):
            counter[0] += 1
            return self._sheet(counter[0])  # a spinner: every frame differs

        flow = _build("- waitForSettled:\n    timeoutMs: 1000\n")
        with mock.patch("autonom_lib.ui.snapshot", side_effect=snapshot):
            result = self._runner().run(flow)
        self.assertEqual(result.status, "passed")
        self.assertIs(result.steps[0].settled, False)
        self.assertEqual([w["code"] for w in result.warnings],
                         ["screen_not_settled"])
        self.assertEqual(result.warnings[0]["line"], 5)
        events = [json.loads(line) for line in
                  Path(result.events_path).read_text(encoding="utf-8").splitlines()]
        finished = [e for e in events if e["kind"] == "flow.step.finished"]
        self.assertIs(finished[0]["payload"]["settled"], False)


# --- 4. Maestro: tapOn.timeoutMs exports, waitForAnimationToEnd imports ------


class MaestroTimedTapTests(unittest.TestCase):
    def test_tap_timeout_exports_as_a_wait_then_the_tap(self) -> None:
        flow = _build("- tapOn:\n    selector:\n      visibleText: Sign in\n"
                      "    timeoutMs: 15000\n    label: Open auth\n")
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn("- extendedWaitUntil:\n    visible:\n        text: Sign in\n"
                      "    timeout: 15000\n- tapOn:\n    text: Sign in\n"
                      "    label: Open auth\n", out)
        back = _import(out)
        self.assertEqual([step.command for step in back.steps],
                         ["waitUntil", "tapOn"])
        self.assertEqual(back.steps[0].args["timeoutMs"], 15000)

    def test_the_suites_optional_timed_tap_round_trips(self) -> None:
        # the WoolBox pattern: dismiss the push onboarding only if it shows
        flow = _build("- tapOn:\n    selector:\n      visibleText: Later\n"
                      "    timeoutMs: 3000\n    optional: true\n"
                      "    reason: first launch only\n")
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn("- extendedWaitUntil:\n    visible:\n        text: Later\n"
                      "    timeout: 3000\n    optional: true\n- tapOn:\n"
                      "    text: Later\n    optional: true\n", out)
        back = _import(out)
        self.assertEqual(len(back.steps), 1)
        tap = back.steps[0]
        self.assertEqual((tap.command, tap.args["timeoutMs"], tap.args["optional"]),
                         ("tapOn", 3000, True))
        self.assertEqual(tap.selector.fields, {"visible_text": "Later"})

    def test_long_press_and_double_tap_timeouts_export_the_same_way(self) -> None:
        for command in ("longPressOn", "doubleTapOn"):
            with self.subTest(command=command):
                out = maestro.export_flow(_build(
                    f"- {command}:\n    selector:\n      id: row\n"
                    "    timeoutMs: 2000\n"), "t.yaml")
                self.assertIn(f"    timeout: 2000\n- {command}:\n    id: row\n", out)

    def test_an_optional_wait_on_its_own_still_refuses(self) -> None:
        tap = "- tapOn:\n    text: Later\n    optional: true\n"
        for text in (
                "appId: a.b\n---\n- extendedWaitUntil:\n    visible: Later\n"
                "    timeout: 3000\n    optional: true\n",
                "appId: a.b\n---\n- extendedWaitUntil:\n    visible: Later\n"
                "    timeout: 3000\n    optional: true\n- tapOn: Other\n",
                # a second arm must not vanish in the fold (review repro)
                "appId: a.b\n---\n- extendedWaitUntil:\n    visible: Later\n"
                "    notVisible: Spinner\n    timeout: 100\n    optional: true\n"
                + tap,
                "appId: a.b\n---\n- extendedWaitUntil:\n    visible: Later\n"
                "    timeout: 100\n    label: wait\n    optional: true\n" + tap):
            with self.subTest(text=text):
                with self.assertRaises(errors.AutonomError) as caught:
                    maestro.import_flow(text, "m.yaml")
                self.assertEqual(caught.exception.code,
                                 errors.UNSUPPORTED_FLOW_COMMAND)
                self.assertEqual(caught.exception.extra["line"], 3)

    def test_other_refusals_are_kept(self) -> None:
        for body, needle in (
                ("- inputText:\n    value: hi\n    timeoutMs: 500\n",
                 "inputText.timeoutMs"),
                ("- scrollUntilVisible:\n    selector:\n      text: B\n",
                 "scrollUntilVisible"),
                ("- tapOn:\n    selector:\n      text: A\n    timeoutMs: 100\n"
                 "    postcondition:\n      id: done\n", "postcondition")):
            with self.subTest(body=body):
                with self.assertRaises(errors.AutonomError) as caught:
                    maestro.export_flow(_build(body), "t.yaml")
                self.assertIn(needle, caught.exception.message)

    def test_wait_for_animation_to_end_is_wait_for_settled(self) -> None:
        back = _import("appId: a.b\n---\n- waitForAnimationToEnd\n"
                       "- waitForAnimationToEnd:\n    timeout: 3000\n")
        self.assertEqual([(s.command, s.args) for s in back.steps],
                         [("waitForSettled", {}),
                          ("waitForSettled", {"timeoutMs": 3000})])
        out = maestro.export_flow(back, "t.yaml")
        self.assertIn("- waitForAnimationToEnd\n- waitForAnimationToEnd:\n"
                      "    timeout: 3000\n", out)
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.export_flow(_build("- waitForSettled:\n    quietMs: 300\n"),
                                "t.yaml")
        self.assertIn("quietMs", caught.exception.message)


# --- 5. report suite --last ---------------------------------------------------


class SuiteWindowTests(unittest.TestCase):
    @staticmethod
    def _runs(*flow_ids: str) -> list[dict]:
        return [{"run_id": f"fr_{index}", "flow_id": flow_id,
                 "flow_path": f"/flows/{flow_id}.yaml", "status": "passed"}
                for index, flow_id in enumerate(flow_ids)]

    def test_a_rerun_replaces_its_flow_instead_of_evicting_another(self) -> None:
        runs = self._runs("auth", "catalog", "feed", "feed")
        window = flow_report.latest_runs_per_flow(runs, 3)
        self.assertEqual([m["flow_id"] for m in window], ["auth", "catalog", "feed"])
        self.assertEqual(window[-1]["run_id"], "fr_3", "the latest run of feed")

    def test_the_window_counts_flows_and_keeps_run_order(self) -> None:
        runs = self._runs("a", "b", "a", "c")
        self.assertEqual([m["run_id"] for m in
                          flow_report.latest_runs_per_flow(runs, 2)],
                         ["fr_2", "fr_3"])
        self.assertEqual(flow_report.latest_runs_per_flow(runs, None), runs)
        self.assertEqual(flow_report.latest_runs_per_flow(runs, 0), runs)
        self.assertEqual(len(flow_report.latest_runs_per_flow(runs, 99)), 3)

    def test_flows_without_an_id_are_told_apart_by_file(self) -> None:
        runs = [{"run_id": "r1", "flow_path": "/f/a.yaml"},
                {"run_id": "r2", "flow_path": "/f/b.yaml"},
                {"run_id": "r3", "flow_path": "/f/a.yaml"}]
        self.assertEqual([m["run_id"] for m in
                          flow_report.latest_runs_per_flow(runs, 2)], ["r2", "r3"])

    def test_a_negative_window_is_refused(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            flow_report.latest_runs_per_flow(self._runs("a"), -1)
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)


# --- 6. Repair advice ---------------------------------------------------------


class RepairAdviceTests(unittest.TestCase):
    def test_selector_miss_advice_is_platform_neutral(self) -> None:
        failure = {"step_index": 1, "command": "tapOn", "line": 5,
                   "error_code": errors.FLOW_ASSERTION_TIMEOUT,
                   "failure_class": "test_failure", "error": "no node matched"}
        steps = [{"index": 1, "status": "failed",
                  "selector": {"text": "Catalog", "match": "exact"}}]
        for code in (errors.FLOW_ASSERTION_TIMEOUT, errors.NO_MATCHING_NODE):
            with self.subTest(code=code):
                brief = repair.repair_brief("f.yaml", {**failure, "error_code": code},
                                            steps)
                self.assertIn("visibleText", brief["advice"])
                self.assertNotIn("text on Android", brief["advice"])
                self.assertNotIn("description on iOS", brief["advice"])


if __name__ == "__main__":
    unittest.main()
