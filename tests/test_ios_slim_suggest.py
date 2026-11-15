"""`simulator slim suggest`: what one built app needs from the simulator.

Hermetic: the `.app` bundle is built in a temporary directory, `otool` and
`codesign` are an injected runner, and the simulator is the fake `simctl`.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_app_scan, ios_slim, simulator_slim  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

ENTITLEMENTS = {
    "application-identifier": "TEAM.com.example.shop",
    "aps-environment": "development",
    "com.apple.developer.associated-domains": ["applinks:example.com",
                                               "webcredentials:example.com"],
    "keychain-access-groups": ["TEAM.com.example.shop"],
}
OTOOL_MAIN = """{path}:
\t/System/Library/Frameworks/StoreKit.framework/StoreKit (compatibility version 1.0.0, current version 1.0.0)
\t/System/Library/Frameworks/Foundation.framework/Foundation (compatibility version 300.0.0)
\t@rpath/image_picker_ios.framework/image_picker_ios (compatibility version 1.0.0)
\t/usr/lib/swift/libswiftCore.dylib (compatibility version 1.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)
"""


def completed(code: int, out: bytes = b"", err: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, err)


def no_tools(argv):
    return completed(127, b"", b"not found")


def build_app(root: Path, *, name: str = "Shop", info: dict | None = None,
              entitlements: dict | None = None, frameworks: tuple[str, ...] = (),
              extensions: dict[str, dict] | None = None, app_clip: bool = False) -> Path:
    app = root / f"{name}.app"
    app.mkdir(parents=True)
    with (app / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "com.example.shop", "CFBundleName": name,
                       "CFBundleExecutable": name, **(info or {})}, handle)
    body = b"\xcf\xfa\xed\xfe" + b"\x00" * 64
    # a simulator build carries its Info.plist and its entitlements as XML
    body += plistlib.dumps({"CFBundleIdentifier": "com.example.shop"}) + b"\x00" * 8
    if entitlements is not None:
        body += plistlib.dumps(entitlements) + b"\x00" * 8
    (app / name).write_bytes(body)
    for framework in frameworks:
        directory = app / "Frameworks" / f"{framework}.framework"
        directory.mkdir(parents=True)
        (directory / framework).write_bytes(b"\xcf\xfa\xed\xfe")
    for extension, extension_info in (extensions or {}).items():
        directory = app / "PlugIns" / f"{extension}.appex"
        directory.mkdir(parents=True)
        with (directory / "Info.plist").open("wb") as handle:
            plistlib.dump(extension_info, handle)
    if app_clip:
        (app / "AppClips" / "Clip.app").mkdir(parents=True)
    return app


def facts(**kwargs) -> dict:
    base = ios_slim.empty_facts()
    for key, value in kwargs.items():
        base[key] = value
    return base


# Per rule condition: facts that make exactly that condition hold.
def facts_for(condition: tuple) -> dict:
    kind = condition[0]
    if kind == "framework":
        return facts(linked_frameworks={condition[1]})
    if kind == "plugin":
        return facts(embedded_frameworks={condition[1] + "_ios"})
    if kind == "info_key":
        key = condition[1]
        return facts(info_keys={key if key.endswith("Description") or not key.startswith("NS")
                                or key == "NSSupportsLiveActivities"
                                else key + "UsageDescription"})
    if kind == "entitlement":
        return facts(entitlements={condition[1]: ["x"]})
    if kind == "entitlement_value":
        return facts(entitlements={condition[1]: [condition[2] + "example.com"]})
    if kind == "background_mode":
        return facts(background_modes={condition[1]})
    if kind == "extension_point":
        return facts(extension_points={condition[1]})
    if kind == "activity_suffix":
        return facts(activity_types={"Order" + condition[1]})
    if kind == "app_clip":
        return facts(app_clip=True)
    raise AssertionError(kind)


def merge(*items: dict) -> dict:
    merged = ios_slim.empty_facts()
    for item in items:
        for key, value in item.items():
            if isinstance(value, set):
                merged[key] |= value
            elif isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = merged[key] or value
    return merged


class RulesTests(unittest.TestCase):
    def test_every_rule_names_a_feature_and_known_condition_kinds(self) -> None:
        kinds = {"framework", "plugin", "info_key", "entitlement", "entitlement_value",
                 "background_mode", "extension_point", "activity_suffix", "app_clip"}
        for rule in ios_slim.SUGGEST_RULES:
            self.assertIn(rule["feature"], ios_slim.FEATURE_BY_ID)
            self.assertTrue(rule["when"])
            for condition in rule["when"]:
                parts = condition if isinstance(condition[0], tuple) else (condition,)
                for part in parts:
                    self.assertIn(part[0], kinds, rule["feature"])
        features = [rule["feature"] for rule in ios_slim.SUGGEST_RULES]
        self.assertEqual(len(features), len(set(features)))

    def test_the_features_the_spec_lists_have_a_rule(self) -> None:
        self.assertLessEqual(
            {"storekit", "health", "homekit", "icloud", "contacts", "calendar",
             "reminders", "mail", "photos", "widgets", "wallet", "maps", "weather",
             "game-center", "siri", "spotlight", "universal-links", "push",
             "keychain-sync", "screen-time", "app-clips", "imessage"},
            {rule["feature"] for rule in ios_slim.SUGGEST_RULES})

    def test_every_condition_fires_on_its_fixture_and_not_on_an_empty_app(self) -> None:
        self.assertEqual(ios_slim.suggest_needs(ios_slim.empty_facts()), ([], []))
        for rule in ios_slim.SUGGEST_RULES:
            for condition in rule["when"]:
                parts = condition if isinstance(condition[0], tuple) else (condition,)
                with self.subTest(feature=rule["feature"], condition=condition):
                    need, evidence = ios_slim.suggest_needs(
                        merge(*(facts_for(part) for part in parts)))
                    self.assertIn(rule["feature"], need)
                    hit = [item for item in evidence if item["feature"] == rule["feature"]]
                    self.assertTrue(hit)
                    self.assertEqual(set(hit[0]), {"feature", "source", "detail"})
                    if len(parts) > 1:
                        # a conjunction does not fire on one half
                        for part in parts:
                            alone, _ = ios_slim.suggest_needs(facts_for(part))
                            self.assertNotIn(rule["feature"], alone)

    def test_negative_fixture_fires_nothing(self) -> None:
        plain = facts(
            linked_frameworks={"Foundation", "UIKit", "CoreGraphics", "WebKit"},
            embedded_frameworks={"Flutter", "App", "url_launcher_ios", "shared_preferences"},
            info_keys={"NSCameraUsageDescription", "NSMicrophoneUsageDescription"},
            background_modes={"audio", "fetch"},
            entitlements={"application-identifier": "T.com.example", "get-task-allow": True},
            extension_points={"com.apple.share-services"},
            activity_types={"com.example.view"})
        self.assertEqual(ios_slim.suggest_needs(plain), ([], []))

    def test_named_evidence_from_the_spec(self) -> None:
        cases = {
            "storekit": facts(embedded_frameworks={"in_app_purchase_storekit"}),
            "health": facts(embedded_frameworks={"health"}),
            "push": facts(embedded_frameworks={"firebase_messaging"}),
            "photos": facts(embedded_frameworks={"image_picker_ios"}),
            "calendar": facts(info_keys={"NSCalendarsFullAccessUsageDescription"}),
            "reminders": facts(info_keys={"NSRemindersUsageDescription"}),
            "siri": facts(info_keys={"NSSpeechRecognitionUsageDescription"}),
            "widgets": facts(extension_points={"com.apple.widgetkit-extension"}),
            "imessage": facts(extension_points={"com.apple.message-payload-provider"}),
        }
        for feature, given in cases.items():
            with self.subTest(feature=feature):
                self.assertEqual(ios_slim.suggest_needs(given)[0], [feature])

    def test_a_system_framework_is_never_read_as_a_plugin(self) -> None:
        # HealthKitUI starts with "health" and is no `health` plugin; it still
        # proves the feature as a framework
        need, evidence = ios_slim.suggest_needs(facts(linked_frameworks={"HealthKitUI"}))
        self.assertEqual(need, ["health"])
        self.assertEqual(evidence[0]["source"], "linked_framework")
        self.assertEqual(ios_slim.suggest_needs(facts(linked_frameworks={"Healthy"})),
                         ([], []))

    def test_empty_entitlement_is_no_evidence(self) -> None:
        for value in (None, False, "", [], {}):
            self.assertEqual(ios_slim.suggest_needs(
                facts(entitlements={"aps-environment": value}))[0], [])

    def test_keychain_needs_both_halves_or_webcredentials(self) -> None:
        self.assertEqual(ios_slim.suggest_needs(
            facts(entitlements={"keychain-access-groups": ["T.x"]}))[0], [])
        self.assertIn("keychain-sync", ios_slim.suggest_needs(
            facts(entitlements={"keychain-access-groups": ["T.x"]},
                  linked_frameworks={"AuthenticationServices"}))[0])
        need = ios_slim.suggest_needs(facts(entitlements={
            "com.apple.developer.associated-domains": ["webcredentials:example.com"]}))[0]
        self.assertEqual(need, ["keychain-sync", "universal-links"])

    def test_suggestion_is_a_valid_profile_and_keeps_what_is_needed(self) -> None:
        body = ios_slim.suggestion(
            facts(linked_frameworks={"StoreKit", "HealthKit"},
                  entitlements={"aps-environment": "development"}),
            name="x", description="y")
        self.assertEqual(body["need"], ["health", "push", "storekit"])
        self.assertEqual(body["already_core"], ["push"])
        profile = body["profile"]
        self.assertEqual(profile["schema"], ios_slim.PROFILE_SCHEMA)
        self.assertEqual(profile["preset"], "minimal")
        self.assertEqual((profile["name"], profile["description"]), ("x", "y"))
        self.assertEqual(ios_slim.validate_profile(profile, require_schema=True), profile)
        disabled = ios_slim.resolve(profile)
        self.assertEqual(body["estimate"]["disabled_labels"], len(disabled))
        kept = set(body["estimate"]["kept_for_need"])
        self.assertEqual(kept, (ios_slim.feature_labels("storekit")
                                | ios_slim.feature_labels("health")) - set(ios_slim.CORE_KEEP))
        self.assertFalse(disabled & kept)
        self.assertEqual(body["truncated"], 0)

    def test_core_features_are_the_ones_core_keep_covers(self) -> None:
        self.assertEqual(ios_slim.core_features(), ["push", "universal-links"])

    def test_evidence_is_capped_with_a_count(self) -> None:
        everything = merge(*(facts_for(part) for rule in ios_slim.SUGGEST_RULES
                             for condition in rule["when"]
                             for part in (condition if isinstance(condition[0], tuple)
                                          else (condition,))))
        _need, evidence = ios_slim.suggest_needs(everything)
        self.assertGreater(len(evidence), ios_slim.SUGGEST_EVIDENCE_CAP)
        body = ios_slim.suggestion(everything)
        self.assertEqual(len(body["evidence"]), 60)
        self.assertEqual(body["truncated"], len(evidence) - 60)
        self.assertLess(len(json.dumps(body)), 12000)


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_info_plist_extensions_and_embedded_entitlements(self) -> None:
        app = build_app(
            self.root,
            info={"NSPhotoLibraryUsageDescription": "x", "NSCameraUsageDescription": "x",
                  "UIBackgroundModes": ["remote-notification", "audio"],
                  "NSUserActivityTypes": ["OrderCoffeeIntent"], "NotAUsageKey": 1},
            entitlements=ENTITLEMENTS, frameworks=("health", "Flutter"),
            extensions={"Widget": {"NSExtension": {
                "NSExtensionPointIdentifier": "com.apple.widgetkit-extension"},
                "NSContactsUsageDescription": "x"}},
            app_clip=True)
        found, described, limitations = ios_app_scan.scan(app, runner=no_tools)
        self.assertEqual(found["info_keys"], {"NSPhotoLibraryUsageDescription",
                                              "NSCameraUsageDescription",
                                              "NSContactsUsageDescription"})
        self.assertEqual(found["background_modes"], {"remote-notification", "audio"})
        self.assertEqual(found["activity_types"], {"OrderCoffeeIntent"})
        self.assertEqual(found["extension_points"], {"com.apple.widgetkit-extension"})
        self.assertEqual(found["entitlements"], ENTITLEMENTS)
        self.assertEqual(found["embedded_frameworks"], {"health", "Flutter"})
        self.assertTrue(found["app_clip"])
        self.assertEqual(described["bundle_id"], "com.example.shop")
        self.assertEqual(described["extensions"], 1)
        # no otool on this "host": said once, not once per binary
        self.assertEqual(len([line for line in limitations if "otool" in line]), 1)
        need, _ = ios_slim.suggest_needs(found)
        self.assertEqual(need, ["app-clips", "contacts", "health", "keychain-sync",
                                "photos", "push", "siri", "universal-links", "widgets"])

    def test_codesign_output_wins_and_otool_is_parsed(self) -> None:
        app = build_app(self.root, frameworks=("Flutter",))
        calls: list[list[str]] = []

        def runner(argv):
            calls.append(list(argv))
            if argv[0] == "codesign":
                return completed(0, plistlib.dumps({"aps-environment": "production"}))
            if argv[-1].endswith("/Shop"):
                return completed(0, OTOOL_MAIN.format(path=argv[-1]).encode())
            return completed(0, f"{argv[-1]}:\n\t/System/Library/Frameworks/"
                                "MapKit.framework/MapKit (compatibility version 1)\n".encode())

        found, described, limitations = ios_app_scan.scan(app, runner=runner)
        self.assertEqual(found["entitlements"], {"aps-environment": "production"})
        self.assertEqual(found["linked_frameworks"], {"StoreKit", "Foundation", "MapKit"})
        self.assertEqual(found["embedded_frameworks"], {"Flutter", "image_picker_ios"})
        self.assertEqual(limitations, [])
        self.assertEqual(calls[0], ["codesign", "-d", "--entitlements", "-", "--xml",
                                    str(app)])
        self.assertEqual([call[:2] for call in calls[1:]], [["otool", "-L"]] * 2)
        self.assertEqual(described["linked_frameworks"], 3)
        self.assertEqual(ios_slim.suggest_needs(found)[0],
                         ["maps", "photos", "push", "storekit"])

    def test_a_debug_build_is_scanned_through_its_debug_dylib(self) -> None:
        # Xcode 16+ Debug: the main executable is a stub, the code is in
        # <CFBundleExecutable>.debug.dylib next to it
        app = build_app(self.root)
        (app / "Shop.debug.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
        (app / "__preview.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
        stub = ("{path}:\n\t@rpath/Shop.debug.dylib (compatibility version 0.0.0)\n"
                "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n")
        code = ("{path}:\n\t@rpath/Shop.debug.dylib (compatibility version 0.0.0)\n"
                "\t/System/Library/Frameworks/StoreKit.framework/StoreKit (compatibility "
                "version 1.0.0)\n\t/System/Library/Frameworks/MapKit.framework/MapKit "
                "(compatibility version 1.0.0)\n")
        calls: list[str] = []

        def runner(argv):
            if argv[0] != "otool":
                return completed(1)
            calls.append(Path(argv[-1]).name)
            if argv[-1].endswith("/Shop.debug.dylib"):
                return completed(0, code.format(path=argv[-1]).encode())
            return completed(0, stub.format(path=argv[-1]).encode())

        found, _described, limitations = ios_app_scan.scan(app, runner=runner)
        self.assertEqual(calls, ["Shop", "Shop.debug.dylib", "__preview.dylib"])
        self.assertEqual(found["linked_frameworks"], {"StoreKit", "MapKit"})
        need = ios_slim.suggest_needs(found)[0]
        self.assertIn("storekit", need)
        self.assertIn("maps", need)
        self.assertFalse([line for line in limitations if "incomplete" in line])

    def test_a_debug_dylib_that_cannot_be_read_makes_the_scan_incomplete(self) -> None:
        stub = "{path}:\n\t@rpath/Shop.debug.dylib (compatibility version 0.0.0)\n"

        def runner(argv):
            if argv[0] == "otool" and argv[-1].endswith("/Shop"):
                return completed(0, stub.format(path=argv[-1]).encode())
            return completed(1, b"", b"not an object file")

        # the dylib is there and otool fails on it
        app = build_app(self.root / "a")
        (app / "Shop.debug.dylib").write_bytes(b"junk")
        _found, _described, limitations = ios_app_scan.scan(app, runner=runner)
        line = next(line for line in limitations if "incomplete" in line)
        self.assertIn("Shop.debug.dylib", line)
        # the dylib is linked and missing from the bundle
        app = build_app(self.root / "b")
        _found, _described, limitations = ios_app_scan.scan(app, runner=runner)
        self.assertTrue(any("incomplete" in line and "Shop.debug.dylib" in line
                            for line in limitations))
        # no otool at all is the one otool line, not a second complaint
        _found, _described, limitations = ios_app_scan.scan(app, runner=no_tools)
        self.assertFalse([line for line in limitations if "incomplete" in line])

    def test_parse_otool_reads_a_fat_binary_once_per_name(self) -> None:
        text = ("/x/Shop (architecture x86_64):\n"
                "\t/System/Library/Frameworks/StoreKit.framework/StoreKit (compatibility "
                "version 1.0.0)\n"
                "\t@rpath/Flutter.framework/Flutter (compatibility version 1.0.0)\n"
                "/x/Shop (architecture arm64):\n"
                "\t/System/Library/Frameworks/MapKit.framework/MapKit (compatibility "
                "version 1.0.0)\n"
                "\t/System/Library/Frameworks/StoreKit.framework/StoreKit (compatibility "
                "version 1.0.0)\n"
                "\t@rpath/Flutter.framework/Flutter (compatibility version 1.0.0)\n")
        self.assertEqual(ios_app_scan.parse_otool(text),
                         ({"StoreKit", "MapKit"}, {"Flutter"}))
        # a header is skipped wherever it stands, a first line that is none is read
        self.assertEqual(ios_app_scan.parse_otool(
            "\t/System/Library/Frameworks/MapKit.framework/MapKit (compatibility)\n"),
            ({"MapKit"}, set()))
        self.assertEqual(ios_app_scan.parse_otool(
            "/x/Health.framework/Health (architecture arm64):\n"), (set(), set()))
        self.assertEqual(ios_app_scan.linked_debug_dylibs(
            "/x/Shop:\n\t@rpath/Shop.debug.dylib (compatibility version 0.0.0)\n"
            "\t/usr/lib/libobjc.A.dylib (compatibility version 1.0.0)\n"),
            {"Shop.debug.dylib"})

    def test_every_missing_piece_is_a_limitation_not_an_error(self) -> None:
        app = self.root / "Bare.app"
        app.mkdir()
        found, described, limitations = ios_app_scan.scan(app, runner=no_tools)
        self.assertEqual(ios_slim.suggest_needs(found), ([], []))
        self.assertEqual(described["name"], "Bare")
        self.assertIsNone(described["bundle_id"])
        text = " | ".join(limitations)
        for word in ("Info.plist", "executable", "entitlements", "embedded frameworks"):
            self.assertIn(word, text)

    def test_damaged_files_are_limitations(self) -> None:
        app = build_app(self.root, extensions={"Bad": {}})
        (app / "PlugIns" / "Bad.appex" / "Info.plist").write_bytes(b"not a plist")
        (app / "Shop").write_bytes(b"<?xml broken </plist>" + b"\x00" * 4)
        _found, _described, limitations = ios_app_scan.scan(
            app, runner=lambda argv: completed(1, b"", b"not an object file"))
        text = " | ".join(limitations)
        self.assertIn("Bad.appex", text)
        self.assertIn("entitlements: none found", text)
        self.assertIn("otool could not read 1 of 1", text)

    def test_executable_name_cannot_leave_the_bundle(self) -> None:
        app = build_app(self.root, info={"CFBundleExecutable": "../../etc/passwd"})
        _found, _described, limitations = ios_app_scan.scan(app, runner=no_tools)
        self.assertTrue(any("names no executable" in line for line in limitations))

    def test_parse_entitlements_skips_an_embedded_info_plist(self) -> None:
        data = (plistlib.dumps({"CFBundleIdentifier": "x"}) + b"\x00"
                + plistlib.dumps({"aps-environment": "development"}))
        self.assertEqual(ios_app_scan.parse_entitlements(data),
                         {"aps-environment": "development"})
        self.assertIsNone(ios_app_scan.parse_entitlements(b""))
        self.assertIsNone(ios_app_scan.parse_entitlements(b"<?xml no end"))

    def test_run_tool_reports_a_missing_tool_as_exit_127(self) -> None:
        self.assertEqual(
            ios_app_scan.run_tool(["autonom-no-such-tool-xyz"]).returncode, 127)


class SuggestBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"simctl_devices": {"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted",
                 "isAvailable": True}]}}}), encoding="utf-8")
        self.set_env(AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.root / "log.jsonl"),
                     AUTONOM_HOME=str(self.root / "home"),
                     AUTONOM_SIMDEVICE_TMP_ROOT=str(self.root / "var-tmp"))
        self.env = dict(os.environ)
        self.target = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})
        self.app = build_app(self.root, entitlements=ENTITLEMENTS,
                             frameworks=("in_app_purchase_storekit",))
        patcher = mock.patch.object(ios_app_scan, "run_tool", no_tools)
        patcher.start()
        self.addCleanup(patcher.stop)
        # scan() binds its default runner at definition time
        scan = ios_app_scan.scan
        patcher2 = mock.patch.object(
            ios_app_scan, "scan", lambda app, runner=no_tools: scan(app, runner=runner))
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def suggest(self, target=None, **values) -> dict:
        return simulator_slim.run(target, "suggest", values)

    def error(self, target=None, **values) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            self.suggest(target, **values)
        error = caught.exception
        self.assertTrue(error.code and error.message and error.hint, error.as_dict())
        return error

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        done = subprocess.run([sys.executable, str(CLI), *argv], capture_output=True,
                              text=True, env=self.env, timeout=120)
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        stream = done.stderr if done.returncode == 2 else done.stdout
        return done.returncode, json.loads(stream)


class SuggestTests(SuggestBase):
    def test_reply_for_an_app_path_needs_no_simulator(self) -> None:
        reply = self.suggest(app=str(self.app))
        self.assertEqual(reply["control"], "slim")
        self.assertEqual(reply["action"], "suggest")
        self.assertEqual(reply["need"], ["keychain-sync", "push", "storekit",
                                         "universal-links"])
        self.assertEqual(reply["already_core"], ["push", "universal-links"])
        self.assertEqual(reply["profile"]["preset"], "minimal")
        self.assertEqual(reply["profile"]["need"], reply["need"])
        self.assertIn("com.example.shop", reply["profile"]["name"])
        self.assertTrue(reply["profile"]["description"])
        self.assertIsNone(reply["written"])
        self.assertEqual(reply["truncated"], 0)
        self.assertEqual(reply["next"],
                         "autonom simulator slim apply --platform ios --udid <UDID> "
                         "--value preset=minimal "
                         "--value need=keychain-sync,push,storekit,universal-links")
        self.assertTrue(reply["limitations"])
        self.assertEqual(reply["app"]["path"], str(self.app))
        for key in ("need", "evidence", "already_core", "profile", "estimate",
                    "limitations", "written", "next", "truncated"):
            self.assertIn(key, reply)
        self.assertGreater(reply["estimate"]["disabled_labels"], 100)
        self.assertTrue(reply["estimate"]["kept_for_need"])

    def test_preset_is_taken_and_checked(self) -> None:
        self.assertEqual(self.suggest(app=str(self.app), preset="optimized")
                         ["profile"]["preset"], "optimized")
        error = self.error(app=str(self.app), preset="tiny")
        self.assertEqual(error.code, errors.SLIM_PROFILE_INVALID)

    def test_write_saves_a_profile_apply_can_load(self) -> None:
        out = self.root / "nested" / ".autonom" / "slim-profile.json"
        reply = self.suggest(app=str(self.app), write=str(out))
        self.assertEqual(reply["written"], str(out))
        self.assertEqual(ios_slim.load_profile(out), reply["profile"])
        self.assertTrue(reply["next"].endswith(f"--value profile={out}"))
        spaced = self.root / "my profiles" / "p.json"
        self.assertTrue(self.suggest(app=str(self.app), write=str(spaced))["next"]
                        .endswith(f"--value 'profile={spaced}'"))
        # round trip: every field the resolver reads survives the file
        self.assertEqual(ios_slim.resolve(ios_slim.load_profile(out)),
                         ios_slim.resolve(reply["profile"]))

    def test_write_refuses_to_overwrite_unless_told(self) -> None:
        out = self.root / "profile.json"
        out.write_text("mine", encoding="utf-8")
        error = self.error(app=str(self.app), write=str(out))
        self.assertEqual(error.code, errors.INVALID_VALUE)
        self.assertEqual(error.extra["path"], str(out))
        self.assertEqual(out.read_text(encoding="utf-8"), "mine")
        self.suggest(app=str(self.app), write=str(out), overwrite="true")
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["preset"], "minimal")

    def test_write_errors(self) -> None:
        self.assertEqual(self.error(app=str(self.app), write=str(self.root)).code,
                         errors.INVALID_VALUE)
        self.assertEqual(self.error(app=str(self.app), write=" ").code,
                         errors.INVALID_VALUE)
        blocker = self.root / "file"
        blocker.write_text("x", encoding="utf-8")
        error = self.error(app=str(self.app), write=str(blocker / "p.json"))
        self.assertEqual(error.code, errors.OUTPUT_NOT_WRITABLE)

    def test_app_errors(self) -> None:
        missing = self.error(app=str(self.root / "Nope.app"))
        self.assertEqual(missing.code, errors.INSTALL_PATH_NOT_FOUND)
        self.assertEqual(missing.extra["path"], str(self.root / "Nope.app"))
        plain = self.root / "notes.txt"
        plain.write_text("x", encoding="utf-8")
        self.assertEqual(self.error(app=str(plain)).code, errors.INVALID_VALUE)
        folder = self.root / "folder"
        folder.mkdir()
        self.assertEqual(self.error(app=str(folder)).code, errors.INVALID_VALUE)
        self.assertEqual(self.error().code, errors.INVALID_VALUE)
        self.assertEqual(self.error(app=" ").code, errors.INVALID_VALUE)
        self.assertEqual(self.error(app="com.example.shop").code, errors.NO_TARGET)
        # a bundle id that ends in ".app" is still a bundle id, not a path
        self.assertFalse(simulator_slim.app_is_path({"app": "com.example.app"}))
        self.assertEqual(self.error(app="com.example.app").code, errors.NO_TARGET)
        self.assertTrue(simulator_slim.app_is_path({"app": "build/Runner.app"}))
        self.assertTrue(simulator_slim.app_is_path({"app": "~/Runner.app"}))
        self.assertFalse(simulator_slim.app_is_path({"app": 5}))
        self.assertEqual(self.error(app=str(self.app), prest="minimal").code,
                         errors.SLIM_PROFILE_INVALID)

    def test_bundle_id_is_resolved_on_the_simulator(self) -> None:
        self.state.write_text(json.dumps({**json.loads(self.state.read_text()),
                                          "app_bundle": str(self.app)}), encoding="utf-8")
        reply = self.suggest(self.target, app="com.example.shop")
        self.assertEqual(reply["app"]["path"], str(self.app))
        self.assertIn(f"--udid {UDID} ", reply["next"])
        calls = [json.loads(line)["argv"] for line in
                 (self.root / "log.jsonl").read_text().splitlines()]
        self.assertIn(["simctl", "get_app_container", UDID, "com.example.shop", "app"],
                      calls)

    def test_bundle_id_ending_in_app_is_resolved_on_the_simulator(self) -> None:
        self.state.write_text(json.dumps({**json.loads(self.state.read_text()),
                                          "app_bundle": str(self.app)}), encoding="utf-8")
        reply = self.suggest(self.target, app="com.example.app")
        self.assertEqual(reply["app"]["path"], str(self.app))

    def test_bundle_id_not_installed(self) -> None:
        error = self.error(self.target, app="com.example.absent")
        self.assertEqual(error.code, errors.APP_NOT_INSTALLED)
        self.assertEqual(error.extra["app_id"], "com.example.absent")

    def test_android_is_unsupported(self) -> None:
        error = self.error(Target(ANDROID, "emulator-5554", "adb", {}), app=str(self.app))
        self.assertEqual(error.code, errors.UNSUPPORTED_CAPABILITY)

    def test_nothing_is_written_to_the_simulator(self) -> None:
        self.suggest(self.target, app=str(self.app))
        self.assertFalse((self.root / "var-tmp").exists())
        self.assertFalse((self.root / "home" / "simulator-slim").exists())


class SuggestCliTests(SuggestBase):
    def test_cli_reads_an_app_path_without_a_device(self) -> None:
        code, payload = self.run_cli("simulator", "slim", "suggest",
                                     "--value", f"app={self.app}")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        # entitlements come from the executable whatever tools the host has
        self.assertLessEqual({"push", "universal-links", "keychain-sync", "storekit"},
                             set(payload["need"]))
        self.assertNotIn("target_id", payload)

    def test_cli_with_a_udid_names_it_in_next(self) -> None:
        code, payload = self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                                     "simulator", "slim", "suggest",
                                     "--value", f"app={self.app}")
        self.assertEqual(code, 0, payload)
        self.assertIn(f"--udid {UDID} ", payload["next"])

    def test_cli_errors_are_json_with_a_hint(self) -> None:
        code, payload = self.run_cli("simulator", "slim", "suggest",
                                     "--value", f"app={self.root}/Nope.app")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error_code"], errors.INSTALL_PATH_NOT_FOUND)
        self.assertTrue(payload["hint"])
        code, payload = self.run_cli("simulator", "slim", "suggest")
        self.assertEqual(payload["error_code"], errors.INVALID_VALUE)


class SchemaTests(unittest.TestCase):
    """`schemas/slim-profile-v1.schema.json` and the validator agree."""

    def setUp(self) -> None:
        self.schema = json.loads(
            (ROOT / "schemas/slim-profile-v1.schema.json").read_text("utf-8"))

    def test_field_names_match_the_validator(self) -> None:
        self.assertEqual(set(self.schema["properties"]), set(ios_slim.PROFILE_FIELDS))
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertEqual(self.schema["properties"]["schema"]["const"],
                         ios_slim.PROFILE_SCHEMA)
        self.assertEqual(self.schema["required"], ["schema"])

    def test_presets_and_default_match(self) -> None:
        preset = self.schema["properties"]["preset"]
        self.assertEqual(preset["enum"], list(ios_slim.PRESETS))
        self.assertEqual(preset["default"], ios_slim.DEFAULT_PRESET)

    def test_list_fields_are_the_validators_list_fields(self) -> None:
        lists = {name for name, spec in self.schema["properties"].items()
                 if spec.get("$ref") == "#/$defs/names"}
        self.assertEqual(lists, set(ios_slim._LIST_FIELDS))
        for name in ("name", "description"):
            self.assertEqual(self.schema["properties"][name]["type"], "string")

    def test_a_name_given_twice_is_accepted_by_both(self) -> None:
        # the validator de-duplicates; the schema must not refuse what it takes
        names = self.schema["$defs"]["names"]["oneOf"]
        self.assertEqual([item["type"] for item in names], ["array", "string"])
        self.assertEqual(names[0]["items"], {"type": "string"})
        self.assertNotIn("uniqueItems", names[0])
        for field in ios_slim._LIST_FIELDS:
            item = {"need": "storekit", "keep": "com.apple.cloudd"}.get(field, "search")
            profile = ios_slim.validate_profile(
                {"schema": ios_slim.PROFILE_SCHEMA, field: [item, item]},
                require_schema=True)
            self.assertEqual(profile[field], [item])

    def test_a_field_outside_the_schema_is_refused_by_the_validator(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            ios_slim.validate_profile({"schema": ios_slim.PROFILE_SCHEMA, "extra": 1})
        self.assertEqual(caught.exception.code, errors.SLIM_PROFILE_INVALID)
        # and every schema field is accepted
        full = {"schema": ios_slim.PROFILE_SCHEMA, "preset": "minimal",
                "need": ["storekit"], "enable": ["search"], "disable": ["icloud"],
                "keep": ["com.apple.cloudd"], "name": "n", "description": "d"}
        self.assertEqual(set(full), set(self.schema["properties"]))
        self.assertEqual(ios_slim.validate_profile(full, require_schema=True), full)


if __name__ == "__main__":
    unittest.main()
