"""iOS (and a few Android) device-control defects found on a live Flutter app.

Each class reproduces one defect with the fake tools and asserts what the
fakes received and what the verb answered:

- a Flutter checkbox reads its state from AXValue "1"/"0", so `checked` was
  false for every control and `assertChecked` could never pass on iOS;
- the log predicate filtered on the bundle id's last component while every
  Flutter app's process is `Runner`, so an app's logs were empty;
- `crash list` answered `count: 0` when idb had lost its companion;
- `permissions grant camera` passed validation and failed inside simctl,
  which on Xcode 27 has no such service;
- status-bar pins, the iOS battery and biometric enrollment never read the
  device back although simctl can list them;
- `push send` ignored the session's app, `clipboard get` did not exist,
  and an unsupported Android clipboard answered `ok: true`;
- `battery set` under a live Android pin was undone by the next capture;
- `capabilities` had no `simulator.keyboard`;
- a SIGTERM in the middle of a log rotation lost the previous rotation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
SERIAL = "emulator-5554"
BUNDLE = "com.example.knit"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import (  # noqa: E402
    device_state, errors, ios_simctl, providers, simulator, ui_ios,
)
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _fake_simctl_module():
    """The fake's own module, for its Xcode 27 help text."""
    spec = importlib.util.spec_from_file_location("autonom_fake_simctl", FAKE_SIMCTL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reset_probes() -> None:
    reset = getattr(ios_simctl, "reset_caches", None)
    if reset is not None:
        reset()


def _element(kind: str, *, value=None, label=None, ident=None, role=None,
             frame=(16, 700, 24, 24)) -> dict:
    """One element in the shape `idb ui describe-all --json` returns."""
    x, y, width, height = frame
    return {
        "AXFrame": f"{{{{{x}, {y}}}, {{{width}, {height}}}}}",
        "AXUniqueId": ident, "frame": {"x": x, "y": y, "width": width, "height": height},
        "role_description": kind.lower(), "AXLabel": label, "content_required": False,
        "type": kind, "title": None, "help": None, "custom_actions": [],
        "AXValue": value, "enabled": True, "role": role or f"AX{kind}", "subrole": None,
    }


# A consent screen from a Flutter app, trimmed and relabelled: two
# checkboxes (the first ticked), a switch, and a text whose value is "1".
CONSENT_TREE = [
    _element("Application", label="Knit", frame=(0, 0, 440, 956)),
    _element("StaticText", label="Before you start", frame=(16, 120, 408, 40)),
    _element("CheckBox", value="1", ident="terms_checkbox", frame=(36, 766, 24, 24)),
    _element("StaticText", label="I accept the terms", frame=(72, 766, 300, 24)),
    _element("CheckBox", value="0", ident="news_checkbox", frame=(36, 808, 24, 24)),
    _element("StaticText", label="Send me the newsletter", frame=(72, 808, 300, 24)),
    _element("Switch", value="1", ident="dark_switch", frame=(360, 850, 51, 31)),
    _element("StaticText", value="1", label="Step", frame=(16, 900, 60, 20)),
]


class Base(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        self.home = self.root / "home"
        self.devices_dir = self.root / "Devices"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(self.home),
            AUTONOM_CORESIMULATOR_DEVICES=str(self.devices_dir),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
            AUTONOM_IDB_COMPANION=None, AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state"),
            AUTONOM_IOS_HID=None, AUTONOM_AXE=None,
        )
        self.env = dict(os.environ)
        _reset_probes()
        self.addCleanup(_reset_probes)

    # --- fakes ------------------------------------------------------------------

    def set_state(self, **kwargs) -> None:
        self.state.write_text(json.dumps(kwargs), encoding="utf-8")

    def update_state(self, **kwargs) -> None:
        state = self.fake_state()
        state.update(kwargs)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def fake_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def argv_log(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["tool"] == tool]

    def clear_log(self) -> None:
        if self.log.exists():
            self.log.unlink()

    def write_tree(self, elements: list) -> str:
        path = self.root / "describe-all.json"
        path.write_text(json.dumps(elements), encoding="utf-8")
        return str(path)

    # --- entry points -------------------------------------------------------------

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], capture_output=True, text=True,
            env=self.env, timeout=120, cwd=self.tmp.name,
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def ios(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
                            "--udid", UDID, *argv)

    def android(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv)

    def ios_target(self) -> Target:
        return Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    def android_target(self) -> Target:
        return Target(ANDROID, SERIAL, str(FAKE_ADB), {"serial": SERIAL})


# --- 1. checkbox / switch state ------------------------------------------------------


class ToggleStateTests(unittest.TestCase):
    def nodes(self, elements: list) -> dict:
        return {node["resource_id"] or node["desc"]: node for node in ui_ios.parse_all(elements)}

    def test_checkbox_state_comes_from_axvalue(self) -> None:
        nodes = self.nodes(CONSENT_TREE)
        self.assertTrue(nodes["terms_checkbox"]["checked"])
        self.assertFalse(nodes["news_checkbox"]["checked"])
        self.assertEqual(nodes["terms_checkbox"]["role"], "checkbox")

    def test_checkboxes_and_switches_are_clickable(self) -> None:
        nodes = self.nodes(CONSENT_TREE)
        for ident in ("terms_checkbox", "news_checkbox", "dark_switch"):
            self.assertTrue(nodes[ident]["clickable"], ident)
        self.assertTrue(nodes["dark_switch"]["checked"])

    def test_on_off_words_and_other_toggle_kinds(self) -> None:
        cases = [
            (_element("Switch", value="on", ident="a"), True),
            (_element("Switch", value="off", ident="a"), False),
            (_element("ToggleButton", value="1", ident="a"), True),
            (_element("RadioButton", value="1", ident="a"), True),
            (_element("RadioButton", value="0", ident="a"), False),
            (_element("CheckBox", value="2", ident="a"), False),  # mixed
            (_element("CheckBox", value=None, ident="a"), False),
            # Typed generically, but the AX role says it is a switch.
            (_element("Other", value="1", ident="a", role="AXSwitch"), True),
        ]
        for element, expected in cases:
            with self.subTest(element=(element["type"], element["AXValue"])):
                self.assertIs(ui_ios.parse_all([element])[0]["checked"], expected)

    def test_a_text_whose_value_is_one_is_not_checked(self) -> None:
        self.assertFalse(self.nodes(CONSENT_TREE)["Step"]["checked"])

    def test_an_explicit_checked_flag_still_wins(self) -> None:
        element = dict(_element("CheckBox", value="1", ident="a"), AXChecked=False)
        self.assertFalse(ui_ios.parse_all([element])[0]["checked"])


class AssertCheckedFlowTests(Base):
    """`assertChecked` end to end on fake iOS: it passes on the ticked box
    and times out on the unticked one."""

    def setUp(self) -> None:
        super().setUp()
        self.set_state(idb_describe_all=self.write_tree(CONSENT_TREE), installed=[BUNDLE])
        code, payload = self.ios("session", "start", "--app-id", BUNDLE)
        self.assertEqual(code, 0, payload)
        self.addCleanup(self.ios, "session", "stop")

    def flow(self, ident: str) -> Path:
        path = self.root / f"{ident}.yaml"
        path.write_text(
            "schema: autonom.dev/flow/v1\n"
            f"appId: {BUNDLE}\n"
            "name: consent\n"
            "---\n"
            "- assertChecked:\n"
            "    selector:\n"
            f"      id: {ident}\n"
            "    timeoutMs: 600\n",
            encoding="utf-8")
        return path

    def test_ticked_checkbox_passes(self) -> None:
        code, summary = self.ios("flow", "run", str(self.flow("terms_checkbox")))
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "passed")

    def test_unticked_checkbox_fails(self) -> None:
        code, summary = self.ios("flow", "run", str(self.flow("news_checkbox")))
        self.assertEqual(code, 1, summary)
        self.assertEqual(summary["status"], "failed")


# --- 2. the app executable and the log predicate --------------------------------------


class AppExecutableTests(Base):
    def app_bundle(self, *, identifier: str | None = BUNDLE, executable: str = "Runner",
                   binary: bool = True, where: str = "build") -> Path:
        app = self.root / where / "Runner.app"
        app.mkdir(parents=True, exist_ok=True)
        info = {"CFBundleExecutable": executable}
        if identifier is not None:
            info["CFBundleIdentifier"] = identifier
        with (app / "Info.plist").open("wb") as handle:
            plistlib.dump(info, handle,
                          fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML)
        return app

    def installed(self, name: str) -> Path:
        """An installed bundle directory, as the simulator's container has."""
        app = self.root / "Devices" / UDID / "data/Containers/Bundle/Application" / name / "Runner.app"
        app.mkdir(parents=True, exist_ok=True)
        return app

    def simctl_verbs(self) -> list[str]:
        return [argv[1] for argv in self.argv_log("simctl") if argv[:1] == ["simctl"]]

    def test_appinfo_names_the_executable_and_bundle_and_is_cached(self) -> None:
        app = self.installed("A1")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner", "Path": str(app)}})
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE), "Runner")
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE), "Runner")
        self.assertEqual(ios_simctl.app_bundle_path(str(FAKE_SIMCTL), UDID, BUNDLE), str(app))
        self.assertEqual(self.simctl_verbs(), ["appinfo"], "one lookup per process")

    def test_the_recorded_install_bundle_is_read_without_the_device(self) -> None:
        app = self.app_bundle()
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE,
                                                   app_path=app), "Runner")
        self.assertEqual(self.simctl_verbs(), [])

    def test_a_recorded_bundle_of_another_app_is_not_trusted(self) -> None:
        app = self.app_bundle(identifier="com.example.other", executable="Other")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner"}})
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE,
                                                   app_path=app), "Runner")

    def test_a_plist_that_names_no_identifier_is_not_trusted(self) -> None:
        app = self.app_bundle(identifier=None, executable="Other")
        self.assertIsNone(ios_simctl.bundle_executable(app, BUNDLE))
        self.assertEqual(ios_simctl.bundle_executable(app), "Other", "no id asked, no check")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner"}})
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE,
                                                   app_path=app), "Runner")

    def test_a_recorded_bundle_answer_is_never_cached(self) -> None:
        app = self.app_bundle(executable="Alpha")
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE,
                                                   app_path=app), "Alpha")
        # Nothing on the device: the app_path answer did not leak into the cache.
        self.assertIsNone(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE))
        self.app_bundle(executable="Beta")
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE,
                                                   app_path=app), "Beta")

    def test_the_installed_bundle_is_the_fallback(self) -> None:
        app = self.app_bundle(binary=False, where="installed")
        self.set_state(app_bundle=str(app))  # appinfo fails: no app_info entry
        self.assertEqual(ios_simctl.app_image(str(FAKE_SIMCTL), UDID, BUNDLE),
                         ("Runner", str(app)))
        self.assertEqual(self.simctl_verbs(), ["appinfo", "get_app_container"])

    def test_a_miss_is_not_cached(self) -> None:
        self.assertIsNone(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE))
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner"}})
        self.assertEqual(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE), "Runner")

    def test_a_reinstalled_app_is_looked_up_again(self) -> None:
        first = self.installed("A1")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner", "Path": str(first)}})
        self.assertEqual(ios_simctl.app_bundle_path(str(FAKE_SIMCTL), UDID, BUNDLE), str(first))
        # A reinstall moves the app to a new container and deletes the old one.
        first.rmdir()
        second = self.installed("A2")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner", "Path": str(second)}})
        self.assertEqual(ios_simctl.app_bundle_path(str(FAKE_SIMCTL), UDID, BUNDLE), str(second))

    def test_appinfo_parsing(self) -> None:
        text = ('{\n    ApplicationType = User;\n    CFBundleExecutable = "My App";\n'
                f'    CFBundleIdentifier = "{BUNDLE}";\n}}\n')
        self.assertEqual(ios_simctl.parse_appinfo_executable(text, BUNDLE), "My App")
        self.assertIsNone(ios_simctl.parse_appinfo_executable(text, "com.example.other"))
        self.assertIsNone(ios_simctl.parse_appinfo_executable(
            "{\n    CFBundleExecutable = \"../x\";\n}\n"))

    def stream_predicate(self, wait: float = 15.0) -> str:
        # The bounded writer starts `log stream` itself, after the verb that
        # spawned it has returned: wait for the argv to show up.
        deadline = time.monotonic() + wait
        while True:
            streams = [argv for argv in self.argv_log("simctl") if "stream" in argv]
            if streams or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        self.assertTrue(streams, "the writer never started `log stream`")
        return streams[0][streams[0].index("--predicate") + 1]

    def test_the_session_stream_filters_on_the_installed_executable(self) -> None:
        app = self.installed("A1")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner", "Path": str(app)}})
        destination = self.root / "logs" / "stream.ndjson"
        pid = ios_simctl.start_log_stream(str(FAKE_SIMCTL), UDID, destination,
                                          bundle_id=BUNDLE)
        self.assertIsNotNone(pid)
        os.waitpid(pid, 0)
        predicate = self.stream_predicate()
        # Before: `processImagePath CONTAINS "knit"`, which no Runner line has;
        # then `CONTAINS "Runner"`, which every Flutter app's line has. The
        # container path is not used: the log reports the first one a
        # binary was seen at (tests/test_fix_ios_logs_uuid.py).
        self.assertIn('processImagePath ENDSWITH "/Runner"', predicate)
        self.assertIn('senderImagePath ENDSWITH "/Runner"', predicate)
        self.assertIn(f'subsystem == "{BUNDLE}"', predicate)
        self.assertNotIn(str(app), predicate)
        self.assertNotIn("BEGINSWITH", predicate)

    def test_with_only_the_executable_the_stream_matches_its_name_exactly(self) -> None:
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner"}})
        destination = self.root / "logs" / "stream.ndjson"
        pid = ios_simctl.start_log_stream(str(FAKE_SIMCTL), UDID, destination,
                                          bundle_id=BUNDLE)
        os.waitpid(pid, 0)
        predicate = self.stream_predicate()
        self.assertIn('processImagePath ENDSWITH "/Runner"', predicate)
        self.assertNotIn("CONTAINS", predicate)

    def test_session_start_log_stream_uses_the_installed_executable(self) -> None:
        app = self.installed("A1")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner", "Path": str(app)}},
                       installed=[BUNDLE])
        code, payload = self.ios("session", "start", "--app-id", BUNDLE, "--log-stream")
        self.assertEqual(code, 0, payload)
        self.addCleanup(self.ios, "session", "stop")
        predicate = self.stream_predicate()
        self.assertIn('processImagePath ENDSWITH "/Runner"', predicate)
        self.assertNotIn(str(app), predicate)

    def test_a_name_with_quotes_is_escaped_in_the_predicate(self) -> None:
        predicate = ios_simctl.log_predicate(BUNDLE, executable='My "App"\\x',
                                             bundle_path='/x/"a\\b"/Runner.app')
        self.assertIn('processImagePath ENDSWITH "/My \\"App\\"\\\\x"', predicate)
        self.assertNotIn("Runner.app", predicate)

    def test_log_noise_is_recognised(self) -> None:
        self.assertTrue(ios_simctl.is_log_noise('{"count":0,"finished":1}'))
        self.assertTrue(ios_simctl.is_log_noise(
            'Filtering the log data using "subsystem == \\"x\\""'))
        self.assertFalse(ios_simctl.is_log_noise(json.dumps({"eventMessage": "hi",
                                                               "count": 1})))
        self.assertFalse(ios_simctl.is_log_noise("plain text"))

    def test_client_side_filter_with_only_the_executable(self) -> None:
        mine = json.dumps({"processImagePath": "/x/Runner.app/Runner", "eventMessage": "m"})
        other = json.dumps({"processImagePath": "/x/Other.app/Other", "eventMessage": "m"})
        near = json.dumps({"processImagePath": "/x/TestRunner.app/TestRunner",
                           "eventMessage": "m"})
        match = ios_simctl.log_line_matches
        self.assertTrue(match(mine, BUNDLE, executable="Runner"))
        self.assertFalse(match(other, BUNDLE, executable="Runner"))
        self.assertFalse(match(near, BUNDLE, executable="Runner"))
        self.assertFalse(match('{"count":0,"finished":1}', BUNDLE, executable="Runner"))

    def test_two_flutter_apps_only_the_session_apps_records_pass(self) -> None:
        mine = self.installed("A1")
        theirs = self.installed("B2")
        self.set_state(app_info={BUNDLE: {"CFBundleExecutable": "Runner", "Path": str(mine)},
                                 "com.example.other": {"CFBundleExecutable": "Runner",
                                                       "Path": str(theirs)}})
        records = {
            "mine": {"processImagePath": f"{mine}/Runner", "eventMessage": "flutter: mine"},
            "my engine": {"processImagePath": f"{mine}/Runner",
                          "senderImagePath": f"{mine}/Frameworks/Flutter.framework/Flutter",
                          "eventMessage": "engine"},
            "my subsystem": {"processImagePath": "/usr/libexec/y", "subsystem": BUNDLE,
                             "eventMessage": "s"},
            "theirs": {"processImagePath": f"{theirs}/Runner", "eventMessage": "flutter: theirs"},
            "their engine": {"processImagePath": f"{theirs}/Runner",
                             "senderImagePath": f"{theirs}/Frameworks/Flutter.framework/Flutter",
                             "eventMessage": "engine"},
            "a sibling": {"processImagePath": f"{mine}2/Runner", "eventMessage": "x"},
        }
        keep = ios_simctl.app_log_filter(str(FAKE_SIMCTL), UDID, BUNDLE)
        kept = sorted(name for name, record in records.items() if keep(json.dumps(record)))
        self.assertEqual(kept, ["mine", "my engine", "my subsystem"])
        # The predicate names the executable, never a container (the log
        # keeps a reinstalled binary's first path): the client side narrows.
        predicate = ios_simctl.app_log_predicate(str(FAKE_SIMCTL), UDID, BUNDLE)
        self.assertIn('processImagePath ENDSWITH "/Runner"', predicate)
        self.assertNotIn(str(mine), predicate)
        self.assertNotIn(str(theirs), predicate)
        # macOS temp dirs sit behind the /var -> /private/var link; both forms match.
        real = os.path.realpath(mine)
        if real != str(mine):
            self.assertTrue(keep(json.dumps({"processImagePath": f"{real}/Runner"})))

    def test_the_bounded_writer_drops_the_banner_and_the_trailer(self) -> None:
        record = json.dumps({"subsystem": BUNDLE, "eventMessage": "real"})
        self.set_state(ios_log=[f'Filtering the log data using "subsystem == \\"{BUNDLE}\\""',
                                record, '{"count":1,"finished":1}'])
        destination = self.root / "stream.ndjson"
        pid = ios_simctl.start_log_stream(str(FAKE_SIMCTL), UDID, destination,
                                          bundle_id=BUNDLE, executable="Runner")
        os.waitpid(pid, 0)
        self.assertEqual(destination.read_text(encoding="utf-8").splitlines(), [record])


class NeverRaisesTests(Base):
    """The lookups behind a log filter or a read-back degrade, never raise:
    a malformed Info.plist raised ExpatError through `session start
    --log-stream` (rolling the session back), and a wedged simctl raised
    TimeoutExpired from every probe."""

    def malformed_bundle(self) -> Path:
        app = self.root / "installed" / "Runner.app"
        app.mkdir(parents=True)
        (app / "Info.plist").write_text("<?xml version='1.0'?><plist><dict><key>CFBundle",
                                        encoding="utf-8")
        return app

    def test_a_malformed_plist_is_no_executable(self) -> None:
        app = self.malformed_bundle()
        self.assertIsNone(ios_simctl.bundle_executable(app, BUNDLE))
        self.assertIsNone(ios_simctl.app_executable(str(FAKE_SIMCTL), UDID, BUNDLE,
                                                    app_path=app))
        self.set_state(app_bundle=str(app))
        self.assertEqual(ios_simctl.app_image(str(FAKE_SIMCTL), UDID, BUNDLE), (None, str(app)))
        pid = ios_simctl.start_log_stream(str(FAKE_SIMCTL), UDID, self.root / "s.ndjson",
                                          bundle_id=BUNDLE)
        self.assertIsNotNone(pid)
        os.waitpid(pid, 0)

    def test_session_start_survives_a_malformed_plist(self) -> None:
        self.set_state(app_bundle=str(self.malformed_bundle()), installed=[BUNDLE])
        code, payload = self.ios("session", "start", "--app-id", BUNDLE, "--log-stream")
        self.assertEqual(code, 0, payload)
        self.addCleanup(self.ios, "session", "stop")
        self.assertNotIn("session_rolled_back", payload)
        self.assertIsInstance(payload["session"]["background"]["log_stream_pid"], int)

    def hang(self, prefix: str) -> None:
        self.update_state(simctl_hang={prefix: 10})
        patcher = unittest.mock.patch.object(ios_simctl, "PROBE_TIMEOUT", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_wedged_appinfo_is_waited_on_once(self) -> None:
        self.hang("simctl appinfo")
        started = time.monotonic()
        self.assertEqual(ios_simctl.app_image(str(FAKE_SIMCTL), UDID, BUNDLE), (None, None))
        self.assertLess(time.monotonic() - started, 5)
        verbs = [argv[1] for argv in self.argv_log("simctl")]
        self.assertEqual(verbs, ["appinfo"], "no second 30 s probe after a timeout")
        pid = ios_simctl.start_log_stream(str(FAKE_SIMCTL), UDID, self.root / "s.ndjson",
                                          bundle_id=BUNDLE)
        self.assertIsNotNone(pid)
        os.waitpid(pid, 0)

    def test_a_wedged_help_falls_back_to_the_static_list(self) -> None:
        self.hang("simctl help")
        services, source = ios_simctl.privacy_services(str(FAKE_SIMCTL))
        self.assertEqual(source, "static")
        self.assertIn("photos", services)

    def test_a_wedged_status_bar_list_is_unavailable_with_a_reason(self) -> None:
        self.hang(f"simctl status_bar {UDID} list")
        result = simulator.apply(self.ios_target(), "status-bar", "pin", {})
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "unavailable")
        self.assertIn("timed out", result["verification_detail"])

    def test_a_wedged_notifyutil_read_is_unavailable_with_a_reason(self) -> None:
        self.hang(f"simctl spawn {UDID} notifyutil -g")
        result = simulator.apply(self.ios_target(), "biometric", "enroll", {})
        self.assertEqual(result["verification"], "unavailable")
        self.assertIn("timed out", result["verification_detail"])

    def test_a_wedged_pasteboard(self) -> None:
        self.hang("simctl pbpaste")
        result = simulator.apply(self.ios_target(), "clipboard", "set", {"text": "x"})
        self.assertEqual(result["verification"], "unavailable")
        self.assertIn("timed out", result["verification_detail"])
        with self.assertRaises(errors.AutonomError) as caught:
            simulator.apply(self.ios_target(), "clipboard", "get", {})
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertIn("timed out", caught.exception.message)


# --- 3. crash list with a lost companion ----------------------------------------------


class CrashListTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.set_env(AUTONOM_IDB=str(FAKE_IDB))

    def test_a_lost_companion_is_an_error_not_an_empty_store(self) -> None:
        self.set_state(idb_fail={"crash list": [
            1, "Failed to connect to companion at localhost:10882: Connection reset"]})
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.crash_list(self.ios_target())
        self.assertEqual(caught.exception.code, errors.IDB_COMPANION_UNAVAILABLE)

    def test_any_other_failure_is_backend_failed(self) -> None:
        self.set_state(idb_fail={"crash list": [1, "unexpected internal error"]})
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.crash_list(self.ios_target())
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)

    def test_exit_zero_with_a_companion_complaint_is_the_same_failure(self) -> None:
        self.set_state(idb_crash_list_stderr="grpc: failed to connect to companion")
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.crash_list(self.ios_target())
        self.assertEqual(caught.exception.code, errors.IDB_COMPANION_UNAVAILABLE)

    def test_a_benign_companion_mention_on_stderr_is_not_a_failure(self) -> None:
        # fb-idb's own INFO lines when it spawns a companion for the call.
        for line in ("No existing companion at /tmp/idb/x.sock, spawning one...",
                     f"Companion at /tmp/idb/x.sock spawned for {UDID}",
                     "Got existing companion for udid; connecting"):
            with self.subTest(line=line):
                self.set_state(idb_crash_list_stderr=line)
                self.assertEqual(device_state.crash_list(self.ios_target()), [])

    def test_a_listing_still_parses_and_an_empty_one_is_empty(self) -> None:
        self.set_state(idb_crash_list="Runner-2026-09-26-120000.ips com.example.knit "
                                      "2026-09-26 12:00:00\n")
        entries = device_state.crash_list(self.ios_target())
        self.assertEqual([entry["name"] for entry in entries],
                         ["Runner-2026-09-26-120000.ips"])
        self.set_state()
        self.assertEqual(device_state.crash_list(self.ios_target()), [])

    def test_the_cli_exits_2(self) -> None:
        self.set_state(idb_fail={"crash list": [1, "Failed to connect to companion"]})
        code, payload = self.ios("crash", "list")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.IDB_COMPANION_UNAVAILABLE)


# --- 4. privacy services from the running simctl --------------------------------------


class PrivacyServiceTests(Base):
    def privacy_calls(self) -> list[list[str]]:
        return [argv for argv in self.argv_log("simctl") if argv[1:2] == ["privacy"]]

    def test_services_this_simctl_lacks_are_invalid_values(self) -> None:
        for service in ("camera", "userTracking"):
            with self.subTest(service=service):
                with self.assertRaises(errors.AutonomError) as caught:
                    device_state.permissions(self.ios_target(), "grant", service, BUNDLE)
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
                supported = caught.exception.extra["supported_services"]
                self.assertIn("photos", supported)
                self.assertNotIn("camera", supported)
                self.assertIn("photos", caught.exception.hint)
        self.assertEqual(self.privacy_calls(), [], "refused before the device is touched")

    def test_a_supported_service_is_sent(self) -> None:
        device_state.permissions(self.ios_target(), "grant", "photos", BUNDLE)
        self.assertEqual(self.privacy_calls(),
                         [["simctl", "privacy", UDID, "grant", "photos", BUNDLE]])

    def test_a_name_nobody_knows_keeps_its_code(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.permissions(self.ios_target(), "grant", "telepathy", BUNDLE)
        self.assertEqual(caught.exception.code, errors.UNKNOWN_PRIVACY_SERVICE)
        self.assertIn("photos", caught.exception.hint)

    def test_the_help_is_read_once_per_process(self) -> None:
        device_state.permissions(self.ios_target(), "grant", "photos", BUNDLE)
        device_state.permissions(self.ios_target(), "revoke", "microphone", BUNDLE)
        helps = [argv for argv in self.argv_log("simctl") if argv[1:2] == ["help"]]
        self.assertEqual(helps, [["simctl", "help", "privacy"]])

    def test_a_service_only_the_live_simctl_knows_is_accepted(self) -> None:
        fake_simctl = _fake_simctl_module()
        self.set_state(privacy_help=fake_simctl.PRIVACY_HELP.replace(
            "\t         siri -", "\t         bluetooth - Allow Bluetooth.\n\t         siri -"))
        device_state.permissions(self.ios_target(), "grant", "bluetooth", BUNDLE)
        self.assertEqual(self.privacy_calls()[-1][4], "bluetooth")

    def test_an_unreadable_help_falls_back_to_the_static_list(self) -> None:
        self.set_state(simctl_fail={"simctl help": [1, "boom"]})
        device_state.permissions(self.ios_target(), "grant", "camera", BUNDLE)
        self.assertEqual(self.privacy_calls()[-1][4], "camera")

    def test_the_parser_reads_only_the_service_section(self) -> None:
        fake_simctl = _fake_simctl_module()
        services = ios_simctl.parse_privacy_services(fake_simctl.PRIVACY_HELP)
        self.assertEqual(services[0], "all")
        self.assertIn("contacts-limited", services)
        self.assertNotIn("grant", services)
        self.assertNotIn("camera", services)
        self.assertEqual(len(services), 13)


# --- 5. read-backs: status bar, iOS battery, biometric enrollment ---------------------


class StatusBarListParseTests(unittest.TestCase):
    LISTING = ("Current Status Bar Overrides:\n=============================\n"
               "Time: 9:41\nDataNetworkType: 11\nWiFi Mode: 3, WiFi Bars: 3\n"
               "Cell Mode: 3, Cell Bars: 4\nOperator Name: Carrier, Inc\n"
               "Battery State: 2, Battery Level: 100, Not Charging: 0\n")

    def test_every_field_is_parsed(self) -> None:
        self.assertEqual(ios_simctl.parse_status_bar_list(self.LISTING), {
            "time": "9:41", "dataNetwork": "11", "wifiMode": "3", "wifiBars": "3",
            "cellularMode": "3", "cellularBars": "4", "operatorName": "Carrier, Inc",
            "batteryState": "2", "batteryLevel": "100", "notCharging": "0",
        })

    def test_raw_values_and_names_both_match(self) -> None:
        matches = ios_simctl.status_bar_value_matches
        self.assertTrue(matches("dataNetwork", "5g", "11"))
        self.assertFalse(matches("dataNetwork", "lte", "11"))
        self.assertTrue(matches("batteryState", "charged", "2"))
        self.assertTrue(matches("batteryState", "discharging", "0"))
        self.assertTrue(matches("wifiMode", "active", "Active"))
        self.assertTrue(matches("batteryLevel", "100", "100"))
        self.assertFalse(matches("batteryLevel", "10", "100"))
        self.assertFalse(matches("time", "9:41", None))

    def test_no_header_is_not_a_listing(self) -> None:
        self.assertIsNone(ios_simctl.parse_status_bar_list("usage: simctl status_bar"))
        self.assertEqual(ios_simctl.parse_status_bar_list(
            "Current Status Bar Overrides:\n=============================\n"), {})


class StatusBarReadBackTests(Base):
    def test_pin_is_read_back(self) -> None:
        code, payload = self.ios("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"], payload)
        self.assertEqual(payload["verification"], "read_back")
        self.assertEqual(sorted(payload["verified_keys"]),
                         sorted(simulator.IOS_STATUS_BAR_PIN))
        self.assertIn(["simctl", "status_bar", UDID, "list"], self.argv_log("simctl"))

    def test_free_text_keys_are_read_back_too(self) -> None:
        code, payload = self.ios("simulator", "status-bar", "pin", "--value", "time=9:41",
                                 "--value", "operatorName=Knit Mobile")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"], payload)
        self.assertIn("time", payload["verified_keys"])

    def test_a_listing_that_prints_names_is_accepted(self) -> None:
        self.set_state(status_bar_names=True)
        code, payload = self.ios("simulator", "status-bar", "pin")
        self.assertTrue(payload["verified"], payload)

    def test_an_override_the_device_dropped_is_a_mismatch(self) -> None:
        self.set_state(status_bar_ignored=True)
        code, payload = self.ios("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["verification"], "mismatch")
        self.assertIn("batteryLevel", payload["mismatched_keys"])

    def test_an_unlistable_bar_stays_unavailable(self) -> None:
        self.set_state(simctl_fail={f"simctl status_bar {UDID} list": [1, "Unknown argument"]})
        code, payload = self.ios("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["verification"], "unavailable")

    def test_clear_is_read_back(self) -> None:
        self.ios("simulator", "status-bar", "pin")
        code, payload = self.ios("simulator", "status-bar", "clear")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"], payload)
        self.assertEqual(self.fake_state()["status_bar"], {})

    def test_a_clear_that_left_overrides_is_a_mismatch(self) -> None:
        self.set_state(status_bar={"batteryLevel": "5"}, status_bar_ignored=True)
        code, payload = self.ios("simulator", "status-bar", "clear")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["verification"], "mismatch")

    def test_ios_battery_set_and_reset_are_read_back(self) -> None:
        code, payload = self.ios("simulator", "battery", "set", "--value", "level=42",
                                 "--value", "state=charging")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"], payload)
        self.assertEqual(payload["observed_overrides"]["batteryLevel"], "42")
        code, payload = self.ios("simulator", "battery", "reset")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"], payload)

    def test_ios_battery_the_device_ignored_is_a_mismatch(self) -> None:
        self.set_state(status_bar_ignored=True)
        code, payload = self.ios("simulator", "battery", "set", "--value", "level=42")
        self.assertEqual(payload["verification"], "mismatch")


class BiometricReadBackTests(Base):
    NAME = "com.apple.BiometricKit.enrollmentChanged"

    def test_enroll_and_unenroll_are_read_back(self) -> None:
        for action, expected in (("enroll", 1), ("unenroll", 0)):
            with self.subTest(action=action):
                code, payload = self.ios("simulator", "biometric", action)
                self.assertEqual(code, 0, payload)
                self.assertTrue(payload["verified"], payload)
                self.assertEqual(payload["verification"], "read_back")
                self.assertEqual(payload["observed_state"], expected)
                self.assertEqual(self.argv_log("simctl")[-1],
                                 ["simctl", "spawn", UDID, "notifyutil", "-g", self.NAME])

    def test_an_enrollment_that_did_not_take_is_a_mismatch(self) -> None:
        self.set_state(notify_ignored=True)
        code, payload = self.ios("simulator", "biometric", "enroll")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["verification"], "mismatch")

    def test_a_match_still_has_no_read_back(self) -> None:
        code, payload = self.ios("simulator", "biometric", "match")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["verification"], "unavailable")
        self.assertFalse(any("-g" in argv for argv in self.argv_log("simctl")))


# --- 6. push send defaults ---------------------------------------------------------------


class PushTests(Base):
    PAYLOAD = json.dumps({"payload": {"aps": {"alert": "New pattern"}}})

    def pushes(self) -> list[list[str]]:
        return [argv for argv in self.argv_log("simctl") if argv[1:2] == ["push"]]

    def test_the_session_app_is_the_default(self) -> None:
        self.set_state(installed=[BUNDLE])
        code, payload = self.ios("session", "start", "--app-id", BUNDLE)
        self.assertEqual(code, 0, payload)
        self.addCleanup(self.ios, "session", "stop")
        code, payload = self.ios("simulator", "push", "send", "--json", self.PAYLOAD)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["app_id"], BUNDLE)
        self.assertEqual(payload["app_id_source"], "session")
        self.assertEqual(self.pushes()[-1][:4], ["simctl", "push", UDID, BUNDLE])

    def test_an_explicit_app_id_wins(self) -> None:
        values = {"app_id": "com.example.other", "payload": {"aps": {}}}
        result = simulator.apply(self.ios_target(), "push", "send", values,
                                 session={"target_id": UDID, "app_id": BUNDLE})
        self.assertEqual(result["app_id"], "com.example.other")
        self.assertEqual(result["app_id_source"], "value")

    def test_no_app_anywhere_is_a_usage_error_with_the_json_shape(self) -> None:
        code, payload = self.ios("simulator", "push", "send", "--json", self.PAYLOAD)
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual(payload["missing"], ["app_id"])
        self.assertIn("--json", payload["hint"])
        self.assertIn('"payload"', payload["hint"])
        self.assertEqual(self.pushes(), [])

    def test_a_missing_payload_is_named(self) -> None:
        code, payload = self.ios("simulator", "push", "send", "--value", f"app_id={BUNDLE}")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)
        self.assertEqual(payload["missing"], ["payload"])

    def test_a_payload_given_as_text_is_parsed(self) -> None:
        code, payload = self.ios("simulator", "push", "send", "--value", f"app_id={BUNDLE}",
                                 "--value", 'payload={"aps": {"alert": "x"}}')
        self.assertEqual(code, 0, payload)

    def test_a_session_on_another_target_lends_nothing(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            simulator.apply(self.ios_target(), "push", "send", {"payload": {}},
                            session={"target_id": "OTHER", "app_id": BUNDLE})
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)


# --- 7/8. clipboard -----------------------------------------------------------------------


class ClipboardTests(Base):
    def test_ios_clipboard_get_reads_the_pasteboard(self) -> None:
        self.set_state(pasteboard="gauge swatch 22x30")
        code, payload = self.ios("simulator", "clipboard", "get")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["text"], "gauge swatch 22x30")
        self.assertEqual(payload["length"], 18)
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["verification"], "read_back")
        self.assertIn(["simctl", "pbpaste", UDID], self.argv_log("simctl"))

    def test_ios_set_then_get_round_trips(self) -> None:
        self.ios("simulator", "clipboard", "set", "--value", "text=naïve")
        code, payload = self.ios("simulator", "clipboard", "get")
        self.assertEqual(payload["text"], "naïve")

    def test_android_clipboard_get_is_unsupported(self) -> None:
        code, payload = self.android("simulator", "clipboard", "get")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(self.argv_log("adb"), [], "refused before any device command")

    def test_android_set_without_the_shell_command_exits_2(self) -> None:
        self.set_state(clipboard_unsupported=True)
        code, payload = self.android("simulator", "clipboard", "set", "--value", "text=hi")
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error_code"], errors.UNSUPPORTED_CAPABILITY)
        self.assertEqual(payload["capability"], "simulator.clipboard")
        self.assertFalse(payload["supported"])

    def test_android_set_that_works_is_still_ok(self) -> None:
        code, payload = self.android("simulator", "clipboard", "set", "--value", "text=hi")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["supported"])


# --- 9. Android battery under a live pin -------------------------------------------------


class BatteryUnderLivePinTests(Base):
    def last_level_sent(self) -> str:
        levels = [argv[-1] for argv in self.argv_log("adb")
                  if argv[2:6] == ["shell", "dumpsys", "battery", "set"]
                  and argv[6:7] == ["level"]]
        return levels[-1]

    def test_battery_set_updates_the_pin_the_next_capture_reasserts(self) -> None:
        code, payload = self.android("simulator", "status-bar", "pin")
        self.assertEqual(code, 0, payload)
        code, payload = self.android("simulator", "battery", "set", "--value", "level=42")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["pin_updated"])
        pinned = simulator.read_snapshot(self.android_target())["status_bar"]["pinned"]
        self.assertEqual(pinned["battery"], 42)
        # What `autonom screenshot` runs first: the re-pin must keep 42.
        self.clear_log()
        result = simulator.reassert_pins(self.android_target())
        self.assertTrue(result["repinned"])
        self.assertEqual(self.last_level_sent(), "42")

    def test_without_a_pin_nothing_is_recorded(self) -> None:
        code, payload = self.android("simulator", "battery", "set", "--value", "level=42")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["pin_updated"])
        self.assertEqual(simulator.read_snapshot(self.android_target()), {})

    def test_observed_is_whole_lines(self) -> None:
        code, payload = self.android("simulator", "battery", "set", "--value", "level=42")
        self.assertEqual(code, 0, payload)
        first = payload["observed"].splitlines()[0]
        self.assertTrue(first == "Current Battery Service state:" or first.startswith("  "),
                        first)

    def test_tail_lines_never_cuts_a_line(self) -> None:
        dumpsys = "Current Battery Service state:\n" + "".join(
            f"  line {index}: {'x' * 30}\n" for index in range(40))
        tail = simulator.tail_lines(dumpsys, 500)
        self.assertLessEqual(len(tail), 500)
        for line in tail.splitlines():
            self.assertIn(line + "\n", dumpsys)
        self.assertTrue(tail.endswith("line 39: " + "x" * 30))
        self.assertEqual(simulator.tail_lines("y" * 900, 500), "y" * 900)


# --- 10. capabilities: simulator.keyboard -------------------------------------------------


class KeyboardCapabilityTests(Base):
    def caps(self, target: Target) -> dict:
        record = {"tooling": {"idb": {"state": "ready"}}}
        snapshot = providers.open_session(target, record).capabilities()
        return {item.name: item for item in snapshot.capabilities}

    def test_ios_with_a_preference_store_is_available(self) -> None:
        (self.devices_dir / UDID / "data/Library/Preferences").mkdir(parents=True)
        keyboard = self.caps(self.ios_target())["simulator.keyboard"]
        self.assertEqual(keyboard.state, "available")
        self.assertIn("reboot=true", keyboard.reason)

    def test_ios_without_one_is_unavailable(self) -> None:
        keyboard = self.caps(self.ios_target())["simulator.keyboard"]
        self.assertEqual(keyboard.state, "unavailable")

    def test_android_is_unavailable(self) -> None:
        for serial in (SERIAL, "R58M123ABC"):
            with self.subTest(serial=serial):
                target = Target(ANDROID, serial, str(FAKE_ADB), {"serial": serial})
                self.assertEqual(self.caps(target)["simulator.keyboard"].state, "unavailable")

    def test_flows_can_require_it(self) -> None:
        self.assertIn("simulator.keyboard", providers.SEMANTIC_CAPABILITIES)
        (self.devices_dir / UDID / "data/Library/Preferences").mkdir(parents=True)
        providers.preflight(self.ios_target(), {"tooling": {"idb": {"state": "ready"}}},
                            ["simulator.keyboard"])
        with self.assertRaises(errors.AutonomError) as caught:
            providers.preflight(self.android_target(), {}, ["simulator.keyboard"])
        self.assertEqual(caught.exception.code, errors.FLOW_REQUIREMENTS_UNMET)

    def test_the_cli_lists_it(self) -> None:
        code, payload = self.ios("capabilities")
        self.assertEqual(code, 0, payload)
        self.assertIn("simulator.keyboard", [item["name"] for item in payload["capabilities"]])


# --- 11. log rotation survives a SIGTERM at any step ------------------------------------

# Prepended to the real writer: deliver SIGTERM just before the N-th
# filesystem call it makes, and note whether a rotation already existed then.
_ROTATION_FAULT = r'''
import json, os, signal, sys
_at, _report, _calls, _rotated = int(sys.argv.pop(1)), sys.argv.pop(1), [0], [False]
def _hook(name):
    real = getattr(os, name)
    def hooked(*args, **kwargs):
        _calls[0] += 1
        _rotated[0] = _rotated[0] or os.path.lexists(sys.argv[1] + ".1")
        if _calls[0] == _at:
            with open(_report, "w") as handle:
                json.dump({"op": name, "rotated_before": _rotated[0]}, handle)
            signal.raise_signal(signal.SIGTERM)
            for _ in range(1000):
                pass
        return real(*args, **kwargs)
    setattr(os, name, hooked)
for _name in ("remove", "unlink", "link", "replace"):
    _hook(_name)
'''
_ROTATION_PRODUCER = ("import sys, time\nsys.stdout.write(('y' * 99 + '\\n') * 349 + 'z' * 99 + '\\n')\n"
                      "sys.stdout.flush()\ntime.sleep(60)\n")
_ROTATION_LAST = b"z" * 99 + b"\n"


def _writer_idle(dest: Path) -> bool:
    """The writer has consumed the producer's last line: no rotation is left."""
    try:
        return dest.read_bytes().endswith(_ROTATION_LAST)
    except FileNotFoundError:
        return False


class RotationSurvivesSigtermTests(unittest.TestCase):
    """`session stop` SIGTERMs the bounded writer whenever it likes. The
    rotation used to remove `.1` and then link `dest` to it, so a stop
    between the two lost the previous rotation (5-12% of runs under load)."""

    def test_a_sigterm_at_any_rotation_step_keeps_the_previous_rotation(self) -> None:
        hit_a_later_rotation = False
        for fault_at in range(1, 12):
            with self.subTest(fault_at=fault_at), tempfile.TemporaryDirectory() as tmp:
                dest, report = Path(tmp) / "s.ndjson", Path(tmp) / "fault.json"
                writer = subprocess.Popen(
                    [sys.executable, "-c", _ROTATION_FAULT + ios_simctl._BOUNDED_WRITER,  # noqa: SLF001
                     str(fault_at), str(report), str(dest), "10000",
                     sys.executable, "-c", _ROTATION_PRODUCER],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 30
                while (writer.poll() is None and not report.exists() and not _writer_idle(dest)
                       and time.monotonic() < deadline):
                    time.sleep(0.02)
                if not report.exists():  # fewer calls than fault_at: stop it when idle
                    writer.send_signal(signal.SIGTERM)
                writer.wait(timeout=10)
                self.assertFalse(dest.with_name("s.ndjson.1.tmp").exists(),
                                 "a stop mid-rotation left a stray temp link behind")
                if not report.exists():
                    continue
                fault = json.loads(report.read_text())
                if fault["rotated_before"]:
                    hit_a_later_rotation = True
                    self.assertTrue(dest.with_name("s.ndjson.1").exists(),
                                    f"SIGTERM before {fault['op']} lost the previous rotation")
                self.assertTrue(dest.exists())
        self.assertTrue(hit_a_later_rotation, "no fault landed after a first rotation")


if __name__ == "__main__":
    unittest.main()
