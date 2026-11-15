"""`simulator slim`: switching iOS Simulator background services off.

Hermetic: every device call goes to the fake `simctl`, and the launchd
override store lives under a temporary `AUTONOM_SIMDEVICE_TMP_ROOT`, so no
real simulator and no real `/private/var/tmp` store is touched. What is
asserted is the argv that was dispatched and the files that were written.
"""
from __future__ import annotations

import itertools
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import errors, ios_simctl, ios_slim, simulator_slim  # noqa: E402
from autonom_lib import ios_slim_catalog as catalog  # noqa: E402
from autonom_lib.platform import ANDROID, IOS, Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

MDM = sorted(ios_slim.category_labels("mdm"))
SEARCH = sorted(ios_slim.category_labels("search"))
SMALL = {"preset": "stock", "disable": "mdm"}  # three labels: fast to drive


# --- pure logic ---------------------------------------------------------------------


class CatalogTests(unittest.TestCase):
    def test_shape_of_the_catalog(self) -> None:
        self.assertEqual(len(catalog.CATEGORIES), 16)
        self.assertEqual(len(catalog.FEATURES), 27)
        self.assertEqual(len(ios_slim.CATALOG_LABELS), 173)
        for item in catalog.CATEGORIES:
            with self.subTest(category=item["id"]):
                self.assertEqual(set(item), {"id", "name", "description", "downside",
                                             "approx_memory_mb", "labels", "always_enabled"})
                self.assertTrue(item["labels"])
        for item in catalog.FEATURES:
            self.assertEqual(set(item), {"id", "name", "labels"})
        self.assertEqual(len({item["id"] for item in catalog.CATEGORIES}), 16)
        self.assertEqual(len({item["id"] for item in catalog.FEATURES}), 27)

    def test_no_forbidden_or_always_enabled_label_is_slimmable(self) -> None:
        always = {label for item in catalog.CATEGORIES for label in item["always_enabled"]}
        self.assertTrue(always)
        for item in catalog.CATEGORIES:
            with self.subTest(category=item["id"]):
                self.assertFalse(set(item["labels"]) & set(catalog.FORBIDDEN_LABELS))
                self.assertFalse(set(item["labels"]) & always)
        self.assertFalse(ios_slim.CATALOG_LABELS & ios_slim.NEVER_DISABLED)
        self.assertEqual(ios_slim.NEVER_DISABLED, always | set(catalog.FORBIDDEN_LABELS))

    def test_core_keep_labels_are_catalog_labels_with_a_reason(self) -> None:
        self.assertEqual(set(ios_slim.CORE_KEEP),
                         {"com.apple.assetsd", "com.apple.swcd", "com.apple.apsd"})
        self.assertLessEqual(set(ios_slim.CORE_KEEP), ios_slim.CATALOG_LABELS)
        self.assertTrue(all(ios_slim.CORE_KEEP.values()))

    def test_every_label_has_a_description(self) -> None:
        for label in ios_slim.CATALOG_LABELS:
            self.assertTrue(ios_slim.describe(label), label)


class ResolveTests(unittest.TestCase):
    def resolve(self, **settings) -> frozenset[str]:
        return ios_slim.resolve(ios_slim.validate_profile(settings))

    def test_presets_in_order(self) -> None:
        self.assertEqual(list(ios_slim.PRESETS), ["stock", "optimized", "minimal"])
        self.assertEqual(ios_slim.DEFAULT_PRESET, "optimized")
        self.assertEqual(self.resolve(), self.resolve(preset="optimized"))

    def test_stock_disables_nothing(self) -> None:
        self.assertEqual(self.resolve(preset="stock"), frozenset())

    def test_optimized_disables_its_categories_and_leaves_the_others_whole(self) -> None:
        disabled = self.resolve(preset="optimized")
        off = {"widgets", "siri", "telemetry", "apps", "family", "health", "messaging",
               "connectivity", "mdm", "other"}
        on = {"search", "icloud", "store", "pim", "web", "photos"}
        self.assertEqual(off | on, set(ios_slim.CATEGORY_BY_ID))
        kept = set().union(*(ios_slim.category_labels(item) for item in on))
        self.assertFalse(disabled & kept, "a category left on stays whole")
        for category_id in off:
            with self.subTest(category=category_id):
                self.assertEqual(ios_slim.category_labels(category_id) - kept - disabled,
                                 frozenset())
        # the wallet daemons are in `store` (on) and `other` (off): they stay on
        self.assertNotIn("com.apple.passd", disabled)

    def test_minimal_disables_everything_but_the_core(self) -> None:
        self.assertEqual(self.resolve(preset="minimal"),
                         ios_slim.CATALOG_LABELS - set(ios_slim.CORE_KEEP))

    def test_core_keep_is_on_under_every_combination_of_disable(self) -> None:
        ids = list(ios_slim.CATEGORY_BY_ID)
        combos = [(), tuple(ids), *((item,) for item in ids),
                  *itertools.combinations(("store", "web", "photos"), 2)]
        for preset in ("optimized", "minimal"):
            for combo in combos:
                with self.subTest(preset=preset, disable=combo):
                    disabled = self.resolve(preset=preset, disable=list(combo))
                    self.assertFalse(disabled & set(ios_slim.CORE_KEEP))

    def test_stock_with_an_explicit_disable_is_fully_manual(self) -> None:
        disabled = self.resolve(preset="stock", disable="store")
        self.assertEqual(disabled, ios_slim.category_labels("store"))
        self.assertIn("com.apple.apsd", disabled)
        self.assertNotIn("com.apple.apsd",
                         self.resolve(preset="stock", disable="store", need="push"))

    def test_need_enable_and_keep_win_over_disabling(self) -> None:
        health = ios_slim.feature_labels("health")
        self.assertTrue(health)
        self.assertFalse(self.resolve(preset="minimal", need="health") & health)
        self.assertFalse(self.resolve(preset="minimal", enable="search")
                         & ios_slim.category_labels("search"))
        self.assertNotIn("com.apple.cloudd",
                         self.resolve(preset="minimal", keep="com.apple.cloudd"))
        self.assertIn("com.apple.cloudd", self.resolve(preset="minimal"))
        # a label listed in a kept category stays on although `other` is disabled
        both = self.resolve(preset="stock", enable="store", disable="other")
        self.assertNotIn("com.apple.passd", both)
        self.assertIn("com.apple.passd", self.resolve(preset="stock", disable="other"))

    def test_disable_adds_to_a_preset(self) -> None:
        disabled = self.resolve(preset="optimized", disable="icloud")
        self.assertLessEqual(ios_slim.category_labels("icloud"), disabled)

    def test_nothing_outside_the_catalog_is_ever_resolved(self) -> None:
        ids = list(ios_slim.CATEGORY_BY_ID)
        for preset in ios_slim.PRESETS:
            for disable in ([], ids, ids[:5]):
                for need in ([], list(ios_slim.FEATURE_BY_ID)):
                    with self.subTest(preset=preset, disable=len(disable), need=len(need)):
                        disabled = self.resolve(preset=preset, disable=disable, need=need)
                        self.assertLessEqual(disabled, ios_slim.CATALOG_LABELS)
                        self.assertFalse(disabled & ios_slim.NEVER_DISABLED)
                        self.assertFalse(disabled & set(catalog.FORBIDDEN_LABELS))

    def test_resolve_ids_takes_categories_features_and_labels(self) -> None:
        labels, named, protected = ios_slim.resolve_ids(
            ["mdm", "game-center", "com.apple.cloudd"])
        self.assertLessEqual(set(MDM), labels)
        self.assertIn("com.apple.cloudd", labels)
        self.assertEqual(named, ["com.apple.cloudd"])
        # the `game-center` feature covers a daemon that is never disabled
        self.assertEqual(protected, ["com.apple.gamed"])
        self.assertNotIn("com.apple.gamed", labels)
        # a name that is both a category and a feature is read as the category
        self.assertEqual(ios_slim.resolve_ids(["siri"])[0], ios_slim.category_labels("siri"))
        for bad in ("nope", "com.apple.not.in.catalog", catalog.FORBIDDEN_LABELS[0]):
            with self.subTest(bad=bad), self.assertRaises(errors.AutonomError) as caught:
                ios_slim.resolve_ids([bad])
            self.assertEqual(caught.exception.code, errors.SLIM_PROFILE_INVALID)
            self.assertEqual(caught.exception.extra["item"], bad)


class ProfileTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def assert_invalid(self, raw, item: str, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            ios_slim.validate_profile(raw, **kwargs)
        error = caught.exception
        self.assertEqual(error.code, errors.SLIM_PROFILE_INVALID)
        self.assertTrue(error.hint)
        self.assertEqual(error.extra["item"], item)
        self.assertIn(item, error.message)
        return error

    def test_each_bad_field_is_named(self) -> None:
        self.assert_invalid({"presets": "optimized"}, "presets")
        self.assert_invalid({"schema": "autonom.slim-profile/v2"}, "autonom.slim-profile/v2")
        self.assert_invalid({"preset": "tiny"}, "tiny")
        self.assert_invalid({"need": ["storekit", "teleport"]}, "teleport")
        self.assert_invalid({"enable": "search,nope"}, "nope")
        self.assert_invalid({"disable": ["icloud", "cloud"]}, "cloud")
        self.assert_invalid({"keep": ["com.apple.unknownd"]}, "com.apple.unknownd")
        # a label that is never disabled is outside the catalog too
        self.assert_invalid({"keep": [catalog.FORBIDDEN_LABELS[0]]}, catalog.FORBIDDEN_LABELS[0])
        # a category id is not a feature id, and the other way round
        self.assert_invalid({"need": ["telemetry"]}, "telemetry")
        self.assert_invalid({"enable": ["storekit"]}, "storekit")

    def test_wrong_types_are_refused(self) -> None:
        for raw in ({"need": 3}, {"enable": [1]}, {"name": 5}, {"description": ["x"]}):
            with self.subTest(raw=raw), self.assertRaises(errors.AutonomError) as caught:
                ios_slim.validate_profile(raw)
            self.assertEqual(caught.exception.code, errors.SLIM_PROFILE_INVALID)
            self.assertTrue(caught.exception.hint)
        with self.assertRaises(errors.AutonomError) as caught:
            ios_slim.validate_profile(["optimized"])
        self.assertEqual(caught.exception.code, errors.SLIM_PROFILE_INVALID)

    def test_defaults_and_comma_lists(self) -> None:
        profile = ios_slim.validate_profile({"need": " storekit , health,storekit ", "keep": ""})
        self.assertEqual(profile, {"schema": ios_slim.PROFILE_SCHEMA, "preset": "optimized",
                                   "need": ["storekit", "health"], "enable": [],
                                   "disable": [], "keep": []})

    def test_profile_file_round_trip_keeps_every_field_the_resolver_reads(self) -> None:
        raw = {"schema": "autonom.slim-profile/v1", "preset": "minimal",
               "need": ["storekit", "health"], "enable": ["search"], "disable": ["icloud"],
               "keep": ["com.apple.cloudd"], "name": "checkout", "description": "for pay tests"}
        path = self.root / "profile.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = ios_slim.load_profile(path)
        self.assertEqual(loaded, raw)
        self.assertEqual(ios_slim.resolve(loaded), ios_slim.resolve(raw))
        # every setting changes the result, so a dropped field would show
        for field in ("need", "enable", "keep"):
            with self.subTest(field=field):
                without = {key: value for key, value in raw.items() if key != field}
                self.assertNotEqual(ios_slim.resolve(without), ios_slim.resolve(loaded))
        self.assertNotEqual(ios_slim.resolve({**raw, "preset": "stock"}),
                            ios_slim.resolve(loaded))
        with_disable = {"preset": "optimized", "disable": ["icloud"]}
        self.assertNotEqual(ios_slim.resolve(with_disable),
                            ios_slim.resolve({"preset": "optimized"}))
        # and through the values of the control
        self.assertEqual(ios_slim.profile_from_values({"profile": str(path)}), raw)

    def test_profile_file_errors(self) -> None:
        missing = self.root / "none.json"
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        bare = self.root / "bare.json"
        bare.write_text(json.dumps({"preset": "minimal"}), encoding="utf-8")
        for path in (missing, broken, bare):
            with self.subTest(path=path.name), self.assertRaises(errors.AutonomError) as caught:
                ios_slim.load_profile(path)
            self.assertEqual(caught.exception.code, errors.SLIM_PROFILE_INVALID)
            self.assertTrue(caught.exception.hint)

    def test_profile_is_exclusive_with_the_other_settings(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            ios_slim.profile_from_values({"profile": "x.json", "preset": "minimal"})
        self.assertEqual(caught.exception.code, errors.SLIM_PROFILE_INVALID)
        self.assertEqual(caught.exception.extra["item"], "preset")

    def test_values_without_settings_are_the_default_preset(self) -> None:
        self.assertFalse(ios_slim.has_settings({"reboot": "true"}))
        self.assertTrue(ios_slim.has_settings({"keep": "com.apple.cloudd"}))
        self.assertEqual(ios_slim.profile_from_values({"reboot": "true"})["preset"], "optimized")


class ParserTests(unittest.TestCase):
    def test_empty_and_garbage_output(self) -> None:
        for text in ("", "\n\n", None, "disabled services = {\n}\n", "no quotes => disabled",
                     '"unterminated => disabled', '"com.apple.x" disabled'):
            with self.subTest(text=text):
                self.assertEqual(ios_slim.parse_print_disabled(text), frozenset())

    def test_mixed_spellings_and_odd_whitespace(self) -> None:
        text = ('\tdisabled services = {\n'
                '\t\t"com.apple.a" => disabled\n'
                '"com.apple.b"=>true\n'
                '   "com.apple.c"   =>    enabled   \n'
                '\t"com.apple.d" => false\n'
                '\t\t"com.apple.e"\t=>\tdisabled\r\n'
                '\t\t"com.apple.f" => Disabled,\n'
                '\t}\n')
        self.assertEqual(ios_slim.parse_print_disabled(text),
                         {"com.apple.a", "com.apple.b", "com.apple.e", "com.apple.f"})

    def test_delta_compare_and_summary_are_scoped_to_the_catalog(self) -> None:
        current = {MDM[0], "com.example.foreign"}
        to_disable, to_enable = ios_slim.delta(current, {MDM[1], "com.example.other"})
        self.assertEqual((to_disable, to_enable), ([MDM[1]], [MDM[0]]))
        self.assertEqual(ios_slim.compare(current, {MDM[0]}),
                         {"ok": True, "missing": [], "extra": []})
        self.assertEqual(ios_slim.compare(current, {MDM[1]}),
                         {"ok": False, "missing": [MDM[1]], "extra": [MDM[0]]})
        self.assertEqual(ios_slim.summarize({"com.example.foreign"})["state"], "stock")
        partial = ios_slim.summarize({MDM[0]})
        self.assertEqual((partial["state"], partial["disabled"], partial["total"]),
                         ("partial", 1, 173))
        self.assertIn("mdm", partial["categories"]["partial"])
        whole = ios_slim.summarize(set(MDM), expected=set(MDM))
        self.assertEqual(whole["state"], "slim")
        self.assertIn("mdm", whole["categories"]["off"])
        preset = ios_slim.summarize(ios_slim.resolve({"preset": "minimal"}))
        self.assertEqual((preset["state"], preset["matches_preset"]), ("slim", "minimal"))

    def test_runtime_gate(self) -> None:
        prefix = "com.apple.CoreSimulator.SimRuntime."
        cases = {"iOS-18-5": True, "iOS-18-6": True, "iOS-26-0": True, "iOS-27-2": True,
                 "iOS-18-4": False, "iOS-18-3-1": False, "iOS-17-5": False, "iOS-19": True,
                 "tvOS-18-5": True, "watchOS-11-5": True, "watchOS-11-4": False,
                 "xrOS-2-5": True, "xrOS-2-4": False, "macOS-15-0": False, "garbage": False}
        for name, expected in cases.items():
            with self.subTest(runtime=name):
                self.assertEqual(ios_slim.persistent_supported(prefix + name), expected)
        self.assertFalse(ios_slim.persistent_supported(None))
        self.assertFalse(ios_slim.persistent_supported(""))


# --- store and device ------------------------------------------------------------------


class SlimBase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.state = root / "state.json"
        self.log = root / "log.jsonl"
        self.tmp_root = root / "var-tmp"
        self.state.write_text("{}", encoding="utf-8")
        self.set_env(
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_HOME=str(root / "home"),
            AUTONOM_SIMDEVICE_TMP_ROOT=str(self.tmp_root),
        )
        self.env = dict(os.environ)
        self.target = Target(IOS, UDID, str(FAKE_SIMCTL), {"udid": UDID})

    # fake state -------------------------------------------------------------------
    def fake(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def patch_fake(self, **kwargs) -> None:
        self.state.write_text(json.dumps({**self.fake(), **kwargs}), encoding="utf-8")

    def set_device(self, state: str = "Booted", runtime: str = RUNTIME) -> None:
        self.patch_fake(simctl_devices={"devices": {runtime: [
            {"udid": UDID, "name": "iPhone 17 Pro", "state": state, "isAvailable": True}]}})

    def device_state(self) -> str:
        devices = self.fake().get("simctl_devices")
        if not devices:
            return "Shutdown"
        return next(iter(devices["devices"].values()))[0]["state"]

    def launchd_disabled(self) -> set[str]:
        entries = ((self.fake().get("launchd") or {}).get(UDID) or {}).get("disabled", {})
        return {label for label, value in entries.items() if value}

    def set_launchd(self, disabled=(), loaded=()) -> None:
        self.patch_fake(launchd={UDID: {"disabled": {label: True for label in disabled},
                                        "loaded": list(loaded)}})

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"]
                for line in self.log.read_text(encoding="utf-8").splitlines()]

    def lifecycle(self) -> list[str]:
        return [argv[1] for argv in self.calls() if argv[1:2] in (["shutdown"], ["bootstatus"])]

    def launchctl_calls(self, *verbs: str) -> list[list[str]]:
        return [argv[4:] for argv in self.calls()
                if argv[1:2] == ["spawn"] and argv[3:4] == ["launchctl"]
                and (not verbs or argv[4] in verbs)]

    # store / state file -------------------------------------------------------------
    def store_path(self) -> Path:
        return self.tmp_root / f"com.apple.CoreSimulator.SimDevice.{UDID}" / "disabled.plist"

    def write_store(self, entries: dict) -> None:
        self.store_path().parent.mkdir(parents=True, exist_ok=True)
        with self.store_path().open("wb") as handle:
            plistlib.dump(entries, handle, fmt=plistlib.FMT_XML)

    def store(self) -> dict:
        if not self.store_path().exists():
            return {}
        with self.store_path().open("rb") as handle:
            return plistlib.load(handle)

    def state_file(self) -> Path:
        return self.root / "home" / "simulator-slim" / f"{UDID}.json"

    def saved(self) -> dict:
        return json.loads(self.state_file().read_text(encoding="utf-8"))

    # running ----------------------------------------------------------------------------
    def slim(self, action: str, **values) -> dict:
        return simulator_slim.run(self.target, action, values)

    def slim_error(self, action: str, **values) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            self.slim(action, **values)
        error = caught.exception
        self.assertTrue(error.code and error.message and error.hint, error.as_dict())
        return error

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stderr if completed.returncode == 2 else completed.stdout
        return completed.returncode, json.loads(stream)

    def ios(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--udid", UDID,
                            "simulator", "slim", *argv)


class StoreTests(SlimBase):
    def test_missing_store_is_nothing_disabled(self) -> None:
        self.assertEqual(ios_slim.read_store(UDID), {})
        self.assertEqual(ios_slim.store_path(UDID), self.store_path())

    def test_write_creates_the_directory_and_an_xml_plist(self) -> None:
        entries = ios_slim.merged_entries({}, disable=[MDM[1], MDM[0]], enable=[MDM[2]])
        path = ios_slim.write_store(UDID, entries)
        self.assertEqual(path, self.store_path())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("<?xml"))
        self.assertLess(text.index(MDM[0]), text.index(MDM[1]), "sorted keys")
        self.assertEqual(self.store(), {MDM[0]: True, MDM[1]: True, MDM[2]: False})
        self.assertEqual([item.name for item in path.parent.iterdir()], ["disabled.plist"],
                         "no temp file is left behind")

    def test_merge_keeps_foreign_entries_value_for_value(self) -> None:
        foreign = {"com.apple.runtime.own": False, "com.example.disabled": True,
                   "com.example.text": "kept", "com.example.number": 7}
        self.write_store({**foreign, MDM[0]: False})
        entries = ios_slim.read_store(UDID)
        merged = ios_slim.merged_entries(entries, disable=[MDM[0]], enable=[MDM[1]],
                                         restore={MDM[2]: None})
        ios_slim.write_store(UDID, merged)
        stored = self.store()
        for label, value in foreign.items():
            self.assertEqual(stored[label], value)
            self.assertIs(type(stored[label]), type(value))
        self.assertEqual({label: stored[label] for label in MDM[:2]},
                         {MDM[0]: True, MDM[1]: False})
        self.assertNotIn(MDM[2], stored)
        self.assertEqual(ios_slim.disabled_in(stored), {"com.example.disabled", MDM[0]})

    def test_a_label_outside_the_catalog_is_never_written(self) -> None:
        for kwargs in ({"disable": ["com.example.foreign"]},
                       {"enable": [catalog.FORBIDDEN_LABELS[0]]},
                       {"restore": {"com.apple.assistantd": None}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(errors.AutonomError) as caught:
                ios_slim.merged_entries({}, **kwargs)
            self.assertEqual(caught.exception.code, errors.INVALID_VALUE)

    def test_unreadable_store_is_a_clean_failure(self) -> None:
        self.store_path().parent.mkdir(parents=True)
        self.store_path().write_bytes(b"not a plist")
        with self.assertRaises(errors.AutonomError) as caught:
            ios_slim.read_store(UDID)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertTrue(caught.exception.hint)

    def test_a_damaged_store_never_escapes_as_a_traceback(self) -> None:
        self.store_path().parent.mkdir(parents=True)
        for body in (b"<plist><dict><key>a</key>",
                     b"<plist><dict><key>a</key><date>junk</date></dict></plist>",
                     b"<plist><array/></plist>"):
            self.store_path().write_bytes(body)
            for action in ("show", "apply", "verify", "restore"):
                with self.subTest(body=body, action=action):
                    error = self.slim_error(action, **({"all": "true"}
                                                       if action == "restore" else {}))
                    self.assertEqual(error.code, errors.BACKEND_FAILED)
        self.assertFalse(self.state_file().exists())

    def test_an_entry_plistlib_cannot_write_back_is_a_clean_failure(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            ios_slim.write_store(UDID, {"com.example.huge": 1 << 70})
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertTrue(caught.exception.hint)
        self.assertEqual(list(self.store_path().parent.iterdir()), [])

    def test_a_malformed_state_file_is_normalised_or_ignored(self) -> None:
        self.state_file().parent.mkdir(parents=True)
        base = {"schema": ios_slim.STATE_SCHEMA, "udid": UDID, "profile": None,
                "disabled": [MDM[0]], "previous": {MDM[0]: None}}
        self.state_file().write_text(json.dumps({**base, "live": ["x"]}), encoding="utf-8")
        self.assertEqual(ios_slim.read_state(UDID)["live"], {"enabled": [], "disabled": []})
        self.set_device("Booted")
        self.assertEqual(self.slim("disable", ids=MDM[1])["stopped"], [MDM[1]])
        self.assertEqual(self.slim("enable", ids=MDM[1])["still_disabled"], [])
        for bad in ({**base, "disabled": [3]}, {**base, "previous": []},
                    {**base, "schema": "other"}, ["list"]):
            self.state_file().write_text(json.dumps(bad), encoding="utf-8")
            self.assertIsNone(ios_slim.read_state(UDID))
        self.state_file().write_text("{broken", encoding="utf-8")
        self.assertIsNone(ios_slim.read_state(UDID))

    def test_only_an_exact_udid_names_a_store(self) -> None:
        for bad in ("all", "booted", "", "../../etc", "AAAAAAAA-1111", UDID + "\n",
                    UDID + "\n/../x"):
            with self.subTest(udid=bad), self.assertRaises(errors.AutonomError) as caught:
                ios_slim.store_path(bad)
            self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
            self.assertTrue(caught.exception.hint)


class ApplyTests(SlimBase):
    def test_apply_on_a_shut_down_simulator_writes_the_store_and_the_state_file(self) -> None:
        self.write_store({"com.apple.runtime.own": False, MDM[0]: False})
        result = self.slim("apply", **SMALL)
        self.assertEqual((result["control"], result["action"]), ("slim", "apply"))
        self.assertEqual((result["changed"], result["disabled_now"], result["enabled_now"]),
                         (3, 3, 0))
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "store_written")
        self.assertFalse(result["rebooted"])
        self.assertEqual(self.store(), {"com.apple.runtime.own": False,
                                        **{label: True for label in MDM}})
        saved = self.saved()
        self.assertEqual(saved["profile"]["disable"], ["mdm"])
        self.assertEqual(saved["disabled"], MDM)
        self.assertEqual(saved["previous"], {MDM[0]: False, MDM[1]: None, MDM[2]: None})
        self.assertTrue(saved["applied_at"])
        self.assertEqual(stat.S_IMODE(self.state_file().stat().st_mode), 0o600)
        # a shut-down simulator needs no lifecycle churn and no launchctl
        self.assertEqual(self.lifecycle(), [])
        self.assertEqual(self.launchctl_calls(), [])

    def test_apply_refuses_a_booted_simulator_without_reboot(self) -> None:
        self.set_device("Booted")
        error = self.slim_error("apply", **SMALL)
        self.assertEqual(error.code, errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertIn("reboot=true", error.hint)
        self.assertFalse(self.store_path().exists())
        self.assertFalse(self.state_file().exists())
        self.assertEqual(self.lifecycle(), [])

    def test_apply_refuses_a_simulator_in_flux(self) -> None:
        for state in ("Booting", "Shutting Down"):
            self.set_device(state)
            error = self.slim_error("apply", reboot="true", **SMALL)
            self.assertEqual(error.code, errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertFalse(self.store_path().exists())

    def test_apply_with_reboot_shuts_down_writes_boots_and_reads_back(self) -> None:
        self.set_device("Booted")
        result = self.slim("apply", reboot="true", details="true", **SMALL)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "read_back")
        self.assertTrue(result["rebooted"])
        self.assertFalse(result["fallback"])
        self.assertEqual(result["disabled_labels"], MDM)
        self.assertEqual(self.lifecycle(), ["shutdown", "bootstatus"])
        self.assertEqual(self.device_state(), "Booted")
        self.assertEqual(self.launchd_disabled(), set(MDM))
        # the store did the work: no per-label launchctl was needed
        self.assertEqual(self.launchctl_calls("disable", "enable"), [])
        self.assertEqual([call[0] for call in self.launchctl_calls()],
                         ["print-disabled", "print-disabled"])

    def test_apply_is_idempotent_and_does_not_reboot(self) -> None:
        self.set_device("Booted")
        self.slim("apply", reboot="true", **SMALL)
        self.log.unlink()
        again = self.slim("apply", **SMALL)  # booted, no reboot=true: still fine
        self.assertEqual(again["changed"], 0)
        self.assertTrue(again["verified"])
        self.assertFalse(again["rebooted"])
        self.assertEqual(self.lifecycle(), [])
        # and on a shut-down simulator the store is not rewritten
        self.set_device("Shutdown")
        before = self.store_path().read_bytes()
        mtime = self.store_path().stat().st_mtime_ns
        third = self.slim("apply", **SMALL)
        self.assertEqual((third["changed"], third["verification"]), (0, "store_matches"))
        self.assertFalse(third["verified"])
        self.assertEqual(self.store_path().read_bytes(), before)
        self.assertEqual(self.store_path().stat().st_mtime_ns, mtime)

    def test_apply_brings_the_simulator_to_exactly_the_profile(self) -> None:
        self.write_store({SEARCH[0]: True, "com.example.foreign": True})
        self.slim("apply", **SMALL)
        stored = self.store()
        self.assertIs(stored[SEARCH[0]], False, "a catalog label outside the profile is enabled")
        self.assertIs(stored["com.example.foreign"], True, "a foreign entry is left alone")
        self.assertEqual(self.saved()["disabled"], MDM, "only what Autonom disabled is recorded")

    def test_a_second_apply_keeps_the_first_snapshot_and_puts_back_what_it_drops(self) -> None:
        self.write_store({MDM[0]: False})
        self.slim("apply", **SMALL)
        self.slim("apply", preset="stock", disable="mdm,search")
        saved = self.saved()
        self.assertEqual(saved["disabled"], sorted([*MDM, *SEARCH]))
        self.assertIs(saved["previous"][MDM[0]], False)
        self.slim("apply", preset="stock", disable="search")
        stored = self.store()
        self.assertIs(stored[MDM[0]], False, "back to the value before the first apply")
        self.assertNotIn(MDM[1], stored, "an entry Autonom added is removed")
        self.assertEqual(self.saved()["disabled"], SEARCH)
        self.slim("apply", preset="stock")
        self.assertFalse(self.state_file().exists(), "nothing disabled: nothing to restore")
        self.assertEqual(self.store(), {MDM[0]: False})

    def test_fallback_sets_the_overrides_label_by_label_and_reboots_once_more(self) -> None:
        self.set_device("Booted")
        self.patch_fake(slim_store_ignored=True)
        result = self.slim("apply", reboot="true", **SMALL)
        self.assertTrue(result["verified"])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallback_labels"], MDM)
        self.assertEqual(self.lifecycle(), ["shutdown", "bootstatus", "shutdown", "bootstatus"])
        self.assertEqual(self.launchctl_calls("disable"),
                         [["disable", f"system/{label}"] for label in MDM])
        self.assertEqual(self.launchd_disabled(), set(MDM))

    def test_overrides_that_do_not_survive_are_slim_not_persisted(self) -> None:
        self.set_device("Booted")
        self.patch_fake(slim_overrides_lost=True)
        error = self.slim_error("apply", reboot="true", **SMALL)
        self.assertEqual(error.code, errors.SLIM_NOT_PERSISTED)
        self.assertEqual(error.extra["labels"], MDM)
        self.assertEqual(error.extra["missing"], MDM)
        self.assertEqual(error.extra["extra"], [])
        self.assertEqual(self.device_state(), "Booted", "the simulator is left running")
        # the store does hold the entries, and the state file says so: restore can undo it
        self.assertEqual(self.saved()["disabled"], MDM)
        self.assertEqual(ios_slim.disabled_in(self.store()), set(MDM))

    def test_unsupported_runtime_is_refused_before_anything_is_written(self) -> None:
        old = "com.apple.CoreSimulator.SimRuntime.iOS-18-3"
        self.set_device("Shutdown", runtime=old)
        error = self.slim_error("apply", **SMALL)
        self.assertEqual(error.code, errors.SLIM_UNSUPPORTED_RUNTIME)
        self.assertEqual(error.extra["runtime"], old)
        self.assertIn("slim disable", error.hint)
        self.assertFalse(self.store_path().exists())
        self.assertFalse(self.state_file().exists())
        # nothing to disable is not refused
        self.assertEqual(self.slim("apply", preset="stock")["changed"], 0)

    def test_a_failed_store_write_leaves_no_state_file(self) -> None:
        self.set_device("Booted")
        boom = errors.AutonomError(errors.BACKEND_FAILED, "disk full", "free some space")
        with mock.patch.object(ios_slim, "write_store", side_effect=boom):
            error = self.slim_error("apply", reboot="true", **SMALL)
        self.assertEqual(error.code, errors.BACKEND_FAILED)
        self.assertFalse(self.state_file().exists())
        self.assertFalse(self.store_path().exists())
        self.assertEqual(self.device_state(), "Booted", "the simulator is booted again")

    def test_a_store_directory_that_cannot_be_created_is_a_clean_failure(self) -> None:
        self.tmp_root.mkdir()
        (self.tmp_root / f"com.apple.CoreSimulator.SimDevice.{UDID}").write_text("a file")
        error = self.slim_error("apply", **SMALL)
        self.assertEqual(error.code, errors.BACKEND_FAILED)
        self.assertFalse(self.state_file().exists())

    def test_a_failed_state_write_puts_the_store_back(self) -> None:
        self.write_store({"com.apple.runtime.own": False})
        before = self.store()
        with mock.patch.object(ios_slim, "write_state", side_effect=OSError(28, "No space")):
            error = self.slim_error("apply", **SMALL)
        self.assertEqual(error.code, errors.BACKEND_FAILED)
        self.assertEqual(self.store(), before)
        self.assertFalse(self.state_file().exists())
        # with no store before, none is left behind either
        self.store_path().unlink()
        with mock.patch.object(ios_slim, "write_state", side_effect=OSError(28, "No space")):
            self.slim_error("apply", **SMALL)
        self.assertFalse(self.store_path().exists())

    def test_a_revert_that_fails_is_not_reported_as_put_back(self) -> None:
        self.write_store({"com.apple.runtime.own": False})
        real = ios_slim.write_store
        calls = []

        def write(udid, entries):
            calls.append(entries)
            if len(calls) > 1:
                raise errors.AutonomError(errors.BACKEND_FAILED, "disk full", "free space")
            return real(udid, entries)

        with mock.patch.object(ios_slim, "write_state", side_effect=OSError(28, "No space")), \
                mock.patch.object(ios_slim, "write_store", side_effect=write):
            error = self.slim_error("apply", **SMALL)
        self.assertEqual(error.code, errors.BACKEND_FAILED)
        self.assertFalse(error.extra["store_reverted"])
        self.assertIn("could NOT be put back", error.hint)
        self.assertIn(str(self.store_path()), error.hint)

    def test_a_mistyped_value_key_is_refused_before_anything_happens(self) -> None:
        self.set_device("Booted")
        cases = [("apply", {"prest": "minimal", "reboot": "true"}, "prest"),
                 ("apply", {"ids": "mdm"}, "ids"), ("verify", {"reboot": "true"}, "reboot"),
                 ("show", {"preset": "stock"}, "preset"), ("enable", {"id": "mdm"}, "id"),
                 ("disable", {"ids": "mdm", "forced": "true"}, "forced"),
                 ("restore", {"preset": "stock"}, "preset")]
        for action, values, item in cases:
            with self.subTest(action=action, item=item):
                error = self.slim_error(action, **values)
                self.assertEqual(error.code, errors.SLIM_PROFILE_INVALID)
                self.assertEqual(error.extra["item"], item)
        self.assertEqual(self.calls(), [], "no device call")

    def test_a_failed_boot_leaves_the_state_file_matching_the_store(self) -> None:
        self.set_device("Booted")
        self.patch_fake(simctl_fail={"simctl bootstatus": [1, "Unable to boot device"]})
        error = self.slim_error("apply", reboot="true", **SMALL)
        self.assertEqual(error.code, errors.IOS_BOOT_FAILED)
        self.assertEqual(self.saved()["disabled"], MDM)
        self.assertEqual(ios_slim.disabled_in(self.store()), set(MDM))

    def test_a_shutdown_that_did_nothing_writes_nothing(self) -> None:
        self.set_device("Booted")
        self.patch_fake(shutdown_ignored=True)
        error = self.slim_error("apply", reboot="true", **SMALL)
        self.assertEqual(error.code, errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertFalse(self.store_path().exists())
        self.assertFalse(self.state_file().exists())

    def test_only_catalog_labels_are_ever_touched(self) -> None:
        self.set_device("Booted")
        self.patch_fake(slim_store_ignored=True)
        self.set_launchd(disabled=["com.example.foreign", SEARCH[0]])
        self.write_store({"com.example.foreign": True, "com.apple.runtime.own": False})
        self.slim("apply", reboot="true", **SMALL)
        self.slim("disable", ids="search,siri")
        self.slim("enable", ids="search")
        self.slim("restore", reboot="true", all="true")
        touched = {call[1].split("/", 1)[1]
                   for call in self.launchctl_calls("disable", "enable", "bootout")}
        self.assertTrue(touched)
        self.assertLessEqual(touched, ios_slim.CATALOG_LABELS)
        stored = self.store()
        self.assertIs(stored["com.example.foreign"], True)
        self.assertIs(stored["com.apple.runtime.own"], False)
        self.assertLessEqual(set(stored) - {"com.example.foreign", "com.apple.runtime.own"},
                             ios_slim.CATALOG_LABELS)
        self.assertIn("com.example.foreign", self.launchd_disabled())


class RestoreTests(SlimBase):
    def test_restore_puts_back_exactly_what_autonom_disabled(self) -> None:
        self.write_store({MDM[0]: False, SEARCH[0]: True, "com.example.foreign": True})
        self.slim("apply", preset="stock", disable="mdm", keep=SEARCH[0])
        # someone else disables a label after the apply: not Autonom's to undo
        self.write_store({**self.store(), SEARCH[1]: True})
        result = self.slim("restore", details="true")
        self.assertTrue(result["state_file_found"])
        self.assertEqual((result["changed"], result["restored_labels"]), (3, MDM))
        self.assertEqual(result["verification"], "store_written")
        self.assertFalse(self.state_file().exists())
        self.assertEqual(self.store(), {MDM[0]: False, SEARCH[0]: False, SEARCH[1]: True,
                                        "com.example.foreign": True})

    def test_restore_needs_shutdown_or_reboot(self) -> None:
        self.set_device("Booted")
        self.slim("apply", reboot="true", **SMALL)
        error = self.slim_error("restore")
        self.assertEqual(error.code, errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertTrue(self.state_file().exists())
        self.log.unlink()
        result = self.slim("restore", reboot="true")
        self.assertTrue(result["verified"])
        self.assertTrue(result["rebooted"])
        self.assertEqual(self.lifecycle(), ["shutdown", "bootstatus"])
        self.assertEqual(self.launchd_disabled(), set())
        self.assertEqual(self.store(), {})
        self.assertFalse(self.state_file().exists())

    def test_a_state_file_that_cannot_be_removed_fails_the_restore(self) -> None:
        self.write_store({"com.apple.runtime.own": False})
        self.slim("apply", **SMALL)
        with mock.patch.object(ios_slim, "delete_state", side_effect=OSError(13, "denied")):
            error = self.slim_error("restore")
        self.assertEqual(error.code, errors.BACKEND_FAILED)
        # the store is back as the state file describes it: nothing is half undone
        self.assertEqual(ios_slim.disabled_in(self.store()), set(self.saved()["disabled"]))
        self.assertFalse(ios_slim.delete_state("CCCCCCCC-1111-2222-3333-DDDDDDDDDDDD"))

    def test_the_second_shutdown_of_a_fallback_does_not_claim_nothing_was_written(self) -> None:
        self.set_device("Booted")
        self.patch_fake(slim_store_ignored=True)
        real = ios_simctl.shutdown
        count = []

        def shutdown(tool, udid):
            count.append(udid)
            if len(count) == 1:
                real(tool, udid)

        with mock.patch.object(ios_simctl, "shutdown", side_effect=shutdown):
            error = self.slim_error("apply", reboot="true", **SMALL)
        self.assertEqual(error.code, errors.SIMULATOR_MUST_BE_SHUTDOWN)
        self.assertNotIn("nothing was written", error.message)
        self.assertEqual(self.saved()["disabled"], MDM)

    def test_restore_without_a_state_file_changes_nothing(self) -> None:
        self.write_store({MDM[0]: True})
        self.set_device("Booted")
        result = self.slim("restore")
        self.assertEqual(result["changed"], 0)
        self.assertFalse(result["state_file_found"])
        self.assertIn("all=true", result["note"])
        self.assertEqual(self.store(), {MDM[0]: True})
        self.assertEqual(self.lifecycle(), [])

    def test_restore_all_re_enables_every_catalog_label(self) -> None:
        self.write_store({MDM[0]: True, SEARCH[0]: True, "com.example.foreign": True})
        result = self.slim("restore", all="true")
        self.assertEqual(result["changed"], 2)
        self.assertEqual(self.store(), {MDM[0]: False, SEARCH[0]: False,
                                        "com.example.foreign": True})

    def test_restore_falls_back_when_the_store_is_ignored(self) -> None:
        self.set_device("Booted")
        self.slim("apply", reboot="true", **SMALL)
        self.patch_fake(slim_store_ignored=True)
        result = self.slim("restore", reboot="true")
        self.assertTrue(result["fallback"])
        self.assertEqual(self.launchctl_calls("enable"),
                         [["enable", f"system/{label}"] for label in MDM])
        self.assertEqual(self.launchd_disabled(), set())


class LiveTests(SlimBase):
    def runtime_root(self, *labels: str) -> Path:
        root = self.root / "RuntimeRoot"
        daemons = root / "System/Library/LaunchDaemons"
        agents = root / "System/Library/LaunchAgents"
        daemons.mkdir(parents=True)
        agents.mkdir(parents=True)
        for index, label in enumerate(labels):
            with ((agents if index % 2 else daemons) / f"{label}.plist").open("wb") as handle:
                plistlib.dump({"Label": label}, handle)
        self.patch_fake(runtime_root=str(root))
        return root

    def test_live_changes_need_a_booted_simulator(self) -> None:
        for action in ("enable", "disable"):
            error = self.slim_error(action, ids="mdm")
            self.assertEqual(error.code, errors.UNSUPPORTED_CAPABILITY)
            self.assertEqual(error.extra["state"], "Shutdown")
        self.assertEqual(self.launchctl_calls(), [])

    def test_ids_are_required_and_checked(self) -> None:
        self.set_device("Booted")
        self.assertEqual(self.slim_error("disable").code, errors.INVALID_VALUE)
        self.assertEqual(self.slim_error("enable", ids=" , ").code, errors.INVALID_VALUE)
        error = self.slim_error("disable", ids="mdm,nope")
        self.assertEqual(error.code, errors.SLIM_PROFILE_INVALID)
        self.assertEqual(error.extra["item"], "nope")
        self.assertEqual(self.launchctl_calls(), [])

    def test_disable_stops_services_now_and_flips_the_store(self) -> None:
        self.set_device("Booted")
        self.write_store({"com.apple.runtime.own": False})
        self.patch_fake(launchd_bootout_gone=[MDM[0]])  # exit 3: already gone, fine
        result = self.slim("disable", ids="mdm")
        self.assertEqual(result["stopped"], MDM)
        self.assertEqual(result["still_enabled"], [])
        self.assertEqual(result["still_running"], [])
        self.assertTrue(result["verified"])
        self.assertFalse(result["rebooted"])
        self.assertEqual(self.launchctl_calls("disable", "bootout"), [
            step for label in MDM
            for step in (["disable", f"system/{label}"], ["bootout", f"system/{label}"])])
        self.assertEqual(self.lifecycle(), [])
        self.assertEqual(self.store(), {"com.apple.runtime.own": False,
                                        **{label: True for label in MDM}})
        saved = self.saved()
        self.assertEqual(saved["disabled"], MDM)
        self.assertIsNone(saved["profile"])
        self.assertEqual(saved["live"]["disabled"], MDM)
        # restore undoes a live disable too
        self.set_device("Shutdown")
        self.slim("restore")
        self.assertEqual(self.store(), {"com.apple.runtime.own": False})

    def test_disable_keeps_the_core_services_unless_forced(self) -> None:
        self.set_device("Booted")
        error = self.slim_error("disable", ids="mdm,com.apple.apsd")
        self.assertEqual(error.code, errors.INVALID_VALUE)
        self.assertIn("force=true", error.hint)
        self.assertEqual(self.launchctl_calls(), [])
        # through a category or a feature the core label is skipped and reported
        result = self.slim("disable", ids="push,mdm")
        self.assertEqual(result["stopped"], MDM)
        self.assertEqual(result["still_enabled"], ["com.apple.apsd"])
        self.assertEqual(result["kept_core"], {"com.apple.apsd": "push notifications"})
        self.assertEqual(result["warnings"][0]["code"], "slim_core_kept")
        self.assertNotIn("com.apple.apsd", self.launchd_disabled())
        forced = self.slim("disable", ids="com.apple.apsd", force="true")
        self.assertEqual(forced["stopped"], ["com.apple.apsd"])
        self.assertIn("com.apple.apsd", self.launchd_disabled())

    def test_disable_reports_a_label_that_would_not_take(self) -> None:
        self.set_device("Booted")
        self.patch_fake(simctl_fail={
            f"simctl spawn {UDID} launchctl disable system/{MDM[1]}": [1, "boom"],
            f"simctl spawn {UDID} launchctl bootout system/{MDM[2]}": [5, "Input/output error"]})
        result = self.slim("disable", ids="mdm")
        self.assertEqual(result["stopped"], [MDM[0], MDM[2]])
        self.assertEqual(result["still_enabled"], [MDM[1]])
        self.assertEqual(result["still_running"], [MDM[2]])
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "mismatch")
        self.assertNotIn(MDM[1], self.store())
        self.assertEqual(self.saved()["disabled"], [MDM[0], MDM[2]])

    def test_a_feature_never_disables_a_protected_daemon(self) -> None:
        self.set_device("Booted")
        result = self.slim("disable", ids="game-center")
        self.assertIn("com.apple.gamed", result["still_enabled"])
        self.assertNotIn("com.apple.gamed", self.launchd_disabled())

    def test_enable_starts_services_and_flips_the_store(self) -> None:
        self.set_device("Booted")
        self.runtime_root(SEARCH[0], SEARCH[1], SEARCH[2])
        self.slim("apply", preset="stock", disable="search", reboot="true")
        self.patch_fake(simctl_fail={
            f"simctl spawn {UDID} launchctl bootstrap system "
            f"{self.root}/RuntimeRoot/System/Library/LaunchDaemons/{SEARCH[2]}.plist":
                [5, "Bootstrap failed: 5: Input/output error"]})
        self.log.unlink()
        result = self.slim("enable", ids="search")
        self.assertEqual(result["started"], SEARCH[:2])
        self.assertEqual(result["not_started"], [SEARCH[2]])
        self.assertEqual(result["on_demand"], SEARCH[3:])
        self.assertEqual(result["still_disabled"], [])
        self.assertTrue(result["verified"])
        self.assertEqual(result["warnings"][0]["code"], "slim_not_started")
        self.assertEqual(self.lifecycle(), [], "no reboot")
        self.assertEqual(self.launchctl_calls("enable"),
                         [["enable", f"system/{label}"] for label in SEARCH])
        self.assertEqual(self.launchd_disabled(), set())
        self.assertEqual(ios_slim.disabled_in(self.store()), set())
        saved = self.saved()
        self.assertEqual(saved["disabled"], [])
        self.assertEqual(saved["live"]["enabled"], SEARCH)
        # verify with no settings follows the live change
        self.assertTrue(self.slim("verify")["ok"])

    def test_enable_counts_a_job_that_is_already_loaded_as_started(self) -> None:
        self.set_device("Booted")
        root = self.runtime_root(SEARCH[0])
        self.set_launchd(disabled=[SEARCH[0]], loaded=[SEARCH[0]])
        self.patch_fake(simctl_fail={
            f"simctl spawn {UDID} launchctl bootstrap system "
            f"{root}/System/Library/LaunchDaemons/{SEARCH[0]}.plist": [37, "already loaded"]})
        result = self.slim("enable", ids=SEARCH[0])
        self.assertEqual((result["started"], result["not_started"]), ([SEARCH[0]], []))

    def test_enable_skips_what_is_already_running(self) -> None:
        self.set_device("Booted")
        self.set_launchd(loaded=[SEARCH[0]])
        result = self.slim("enable", ids=SEARCH[0])
        self.assertEqual(result["already_running"], [SEARCH[0]])
        self.assertEqual(self.launchctl_calls("enable", "bootstrap"), [])

    def test_enable_reports_an_override_that_would_not_lift(self) -> None:
        self.set_device("Booted")
        self.set_launchd(disabled=[MDM[0]])
        self.patch_fake(launchctl_ignored=True)
        result = self.slim("enable", ids=MDM[0])
        self.assertEqual(result["still_disabled"], [MDM[0]])
        self.assertFalse(result["verified"])


class ShowVerifyTests(SlimBase):
    def test_show_reads_the_store_of_a_shut_down_simulator(self) -> None:
        shown = self.slim("show")
        self.assertEqual((shown["state"], shown["disabled"], shown["total"]), ("stock", 0, 173))
        self.assertEqual(shown["source"], "store")
        self.assertTrue(shown["persistent"])
        self.assertFalse(shown["state_file"]["exists"])
        self.assertEqual(len(shown["categories"]["on"]), 16)
        self.slim("apply", preset="optimized", need="health")
        shown = self.slim("show")
        self.assertEqual(shown["state"], "slim")
        self.assertTrue(shown["state_file"]["exists"])
        self.assertEqual(shown["state_file"]["profile"]["need"], ["health"])
        self.assertIn("widgets", shown["categories"]["off"])
        self.assertIn("health", shown["categories"]["partial"])
        self.assertIn("search", shown["categories"]["on"])
        self.assertEqual(shown["core_keep"], {label: True for label in ios_slim.CORE_KEEP})
        self.assertEqual(self.launchctl_calls(), [])

    def test_show_reads_a_booted_simulator_back(self) -> None:
        self.set_device("Booted")
        self.set_launchd(disabled=[MDM[0], "com.example.foreign"])
        shown = self.slim("show")
        self.assertEqual((shown["state"], shown["disabled"], shown["source"]),
                         ("partial", 1, "launchctl"))
        self.assertEqual(shown["verification"], "read_back")
        self.assertEqual(shown["simulator_state"], "Booted")

    def test_show_reads_legacy_print_disabled_output(self) -> None:
        self.set_device("Booted")
        self.set_launchd(disabled=MDM)
        self.patch_fake(print_disabled_style="legacy")
        self.assertEqual(self.slim("show")["disabled"], 3)

    def test_show_reports_a_runtime_without_persistent_overrides(self) -> None:
        self.set_device("Shutdown", runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5")
        self.assertFalse(self.slim("show")["persistent"])

    def test_verify_reports_missing_and_extra(self) -> None:
        self.set_device("Booted")
        self.set_launchd(disabled=[MDM[0], SEARCH[0], "com.example.foreign"])
        result = self.slim("verify", **SMALL)
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing"], MDM[1:])
        self.assertEqual(result["extra"], [SEARCH[0]])
        self.assertEqual((result["compared_with"], result["source"]), ("settings", "launchctl"))
        self.set_launchd(disabled=MDM)
        self.assertEqual(self.slim("verify", **SMALL)["ok"], True)
        self.assertEqual(self.launchctl_calls("disable", "enable", "bootout", "bootstrap"), [])

    def test_verify_without_settings_uses_the_state_file_or_stock(self) -> None:
        stock = self.slim("verify")
        self.assertTrue(stock["ok"])
        self.assertEqual(stock["compared_with"], "stock")
        self.assertIn("note", stock)
        self.slim("apply", **SMALL)
        ok = self.slim("verify")
        self.assertEqual((ok["ok"], ok["compared_with"], ok["source"]),
                         (True, "state_file", "store"))
        self.assertEqual(ok["profile"]["disable"], ["mdm"])
        self.write_store({**self.store(), MDM[0]: False})
        drift = self.slim("verify")
        self.assertEqual((drift["ok"], drift["missing"]), (False, [MDM[0]]))

    def test_verify_rejects_a_bad_setting(self) -> None:
        self.assertEqual(self.slim_error("verify", preset="tiny").code,
                         errors.SLIM_PROFILE_INVALID)


class TargetTests(SlimBase):
    def test_android_is_refused(self) -> None:
        android = Target(ANDROID, "emulator-5554", str(FAKE_ADB), {"serial": "emulator-5554"})
        for action in ("show", "apply", "restore", "enable"):
            with self.subTest(action=action), self.assertRaises(errors.AutonomError) as caught:
                simulator_slim.run(android, action, {})
            self.assertEqual(caught.exception.code, errors.UNSUPPORTED_CAPABILITY)
            self.assertEqual(caught.exception.extra["capability"], "simulator.slim")
            self.assertTrue(caught.exception.hint)

    def test_all_and_booted_are_refused(self) -> None:
        for udid in ("all", "booted"):
            target = Target(IOS, udid, str(FAKE_SIMCTL), {"udid": udid})
            for action in ("show", "apply", "restore", "disable"):
                with self.subTest(udid=udid, action=action), \
                        self.assertRaises(errors.AutonomError) as caught:
                    simulator_slim.run(target, action,
                                       {"ids": "mdm"} if action == "disable" else {})
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
                self.assertTrue(caught.exception.hint)
        self.assertEqual(self.calls(), [])

    def test_an_unknown_simulator_is_refused(self) -> None:
        other = "CCCCCCCC-1111-2222-3333-DDDDDDDDDDDD"
        target = Target(IOS, other, str(FAKE_SIMCTL), {"udid": other})
        with self.assertRaises(errors.AutonomError) as caught:
            simulator_slim.run(target, "apply", {})
        self.assertEqual(caught.exception.code, errors.NO_TARGET)
        self.assertTrue(caught.exception.hint)


class SimctlWrapperTests(SlimBase):
    def test_print_disabled_is_retried_while_launchd_settles(self) -> None:
        settling = subprocess.CompletedProcess([], 1, "", "LaunchdSimError code=124")
        ready = subprocess.CompletedProcess([], 0, '"com.apple.x" => disabled\n', "")
        with mock.patch.object(ios_simctl, "LAUNCHD_SETTLE_DELAYS", (0, 0)), \
                mock.patch.object(ios_simctl, "run_simctl",
                                  side_effect=[settling, ready]) as run:
            text = ios_simctl.launchctl_print_disabled("xcrun", UDID)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(ios_slim.parse_print_disabled(text), {"com.apple.x"})

    def test_print_disabled_failure_is_a_clean_error(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:  # the fake device is shut down
            ios_simctl.launchctl_print_disabled(str(FAKE_SIMCTL), UDID)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertTrue(caught.exception.hint)

    def test_a_spawn_that_times_out_is_a_failure_not_an_exception(self) -> None:
        timeout = subprocess.TimeoutExpired("xcrun", 1)
        with mock.patch.object(ios_simctl, "run_simctl", side_effect=timeout):
            self.assertFalse(ios_simctl.launchctl_disable("xcrun", UDID, MDM[0]))
            self.assertFalse(ios_simctl.launchctl_bootout("xcrun", UDID, MDM[0]))
            self.assertEqual(ios_simctl.launchctl_loaded_labels("xcrun", UDID), set())

    def test_runtime_root_and_job_plists(self) -> None:
        root = self.root / "RuntimeRoot"
        (root / "System/Library/LaunchAgents").mkdir(parents=True)
        (root / "System/Library/LaunchAgents/com.apple.x.plist").write_bytes(b"")
        self.patch_fake(runtime_root=str(root))
        tool = str(FAKE_SIMCTL)
        self.assertEqual(ios_simctl.device_runtime_identifier(tool, UDID), RUNTIME)
        self.assertIsNone(ios_simctl.device_runtime_identifier(tool, "nope"))
        self.assertEqual(ios_simctl.runtime_root(tool, RUNTIME), root)
        self.assertIsNone(ios_simctl.runtime_root(tool, "other.runtime"))
        self.assertIsNone(ios_simctl.runtime_root(tool, None))
        self.assertEqual(ios_simctl.launchd_job_plist(root, "com.apple.x"),
                         root / "System/Library/LaunchAgents/com.apple.x.plist")
        self.assertIsNone(ios_simctl.launchd_job_plist(root, "com.apple.y"))
        self.assertIsNone(ios_simctl.launchd_job_plist(None, "com.apple.x"))


class CliTests(SlimBase):
    def test_presets_needs_no_device(self) -> None:
        code, payload = self.run_cli("simulator", "slim", "presets")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["id"] for item in payload["presets"]],
                         ["stock", "optimized", "minimal"])
        self.assertEqual([item["disabled_labels"] for item in payload["presets"]][0], 0)
        self.assertEqual(payload["presets"][2]["disabled_labels"], 170)
        self.assertEqual(payload["core_keep"], ios_slim.CORE_KEEP)
        self.assertEqual(len(payload["categories"]), 16)
        self.assertEqual(set(payload["categories"][0]),
                         {"id", "name", "description", "downside", "approx_memory_mb", "labels"})
        self.assertEqual(len(payload["features"]), 27)
        self.assertEqual(payload["catalog_labels"], 173)
        self.assertEqual(self.calls(), [], "no device call")

    def test_apply_show_verify_restore_through_the_cli(self) -> None:
        code, applied = self.ios("apply", "--value", "preset=stock", "--value", "disable=mdm")
        self.assertEqual(code, 0, applied)
        self.assertEqual((applied["ok"], applied["control"], applied["action"]),
                         (True, "slim", "apply"))
        self.assertEqual((applied["platform"], applied["target_id"]), ("ios", UDID))
        self.assertEqual((applied["verified"], applied["verification"]),
                         (False, "store_written"))
        code, shown = self.ios("show")
        self.assertEqual((code, shown["state"], shown["disabled"]), (0, "slim", 3))
        code, verified = self.ios("verify")
        self.assertEqual((code, verified["ok"]), (0, True))
        code, drift = self.ios("verify", "--json", json.dumps({"preset": "stock",
                                                               "disable": ["search"]}))
        self.assertEqual(code, 1, "a failed check exits 1 with the payload on stdout")
        self.assertFalse(drift["ok"])
        self.assertEqual((drift["missing"], drift["extra"]), (SEARCH, MDM))
        code, restored = self.ios("restore")
        self.assertEqual((code, restored["changed"]), (0, 3))
        self.assertEqual(self.store(), {})

    def test_profile_file_through_the_cli(self) -> None:
        path = self.root / "profile.json"
        path.write_text(json.dumps({"schema": "autonom.slim-profile/v1", "preset": "stock",
                                    "disable": ["mdm"], "name": "tiny"}), encoding="utf-8")
        code, applied = self.ios("apply", "--value", f"profile={path}")
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["profile"]["name"], "tiny")
        self.assertEqual(ios_slim.disabled_in(self.store()), set(MDM))

    def test_every_error_payload_has_a_code_a_message_and_a_hint(self) -> None:
        bad_profile = self.root / "bad.json"
        bad_profile.write_text("{", encoding="utf-8")
        cases = [
            (("apply", "--value", "preset=tiny"), errors.SLIM_PROFILE_INVALID),
            (("apply", "--value", "need=teleport"), errors.SLIM_PROFILE_INVALID),
            (("apply", "--value", "keep=com.apple.unknownd"), errors.SLIM_PROFILE_INVALID),
            (("apply", "--value", f"profile={bad_profile}"), errors.SLIM_PROFILE_INVALID),
            (("apply", "--value", f"profile={bad_profile}", "--value", "preset=stock"),
             errors.SLIM_PROFILE_INVALID),
            (("apply", "--value", "reboot=maybe"), errors.FLOW_COMMAND_INVALID),
            (("verify", "--value", "disable=nope"), errors.SLIM_PROFILE_INVALID),
            (("enable", "--value", "ids=mdm"), errors.UNSUPPORTED_CAPABILITY),
            (("disable",), errors.INVALID_VALUE),
            (("toggle",), errors.INVALID_SIMULATOR_ACTION),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                code, payload = self.ios(*argv)
                self.assertEqual(code, 2, payload)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error_code"], expected)
                self.assertTrue(payload["error"])
                self.assertTrue(payload["hint"])
        self.assertFalse(self.store_path().exists())
        self.assertFalse(self.state_file().exists())

    def test_device_errors_through_the_cli(self) -> None:
        self.set_device("Booted")
        code, payload = self.ios("apply", "--value", "preset=stock", "--value", "disable=mdm")
        self.assertEqual((code, payload["error_code"]), (2, errors.SIMULATOR_MUST_BE_SHUTDOWN))
        self.assertTrue(payload["hint"])
        self.patch_fake(slim_overrides_lost=True)
        code, payload = self.ios("apply", "--value", "preset=stock", "--value", "disable=mdm",
                                 "--value", "reboot=true")
        self.assertEqual((code, payload["error_code"]), (2, errors.SLIM_NOT_PERSISTED))
        self.assertEqual(payload["labels"], MDM)
        self.assertTrue(payload["error"] and payload["hint"])
        self.set_device("Shutdown", runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-3")
        code, payload = self.ios("apply")
        self.assertEqual((code, payload["error_code"]), (2, errors.SLIM_UNSUPPORTED_RUNTIME))
        self.assertTrue(payload["error"] and payload["hint"])
        code, payload = self.run_cli("--adb", str(FAKE_ADB), "--serial", "emulator-5554",
                                     "simulator", "slim", "show")
        self.assertEqual((code, payload["error_code"]), (2, errors.UNSUPPORTED_CAPABILITY))
        self.assertTrue(payload["hint"])

    def test_capability_is_listed(self) -> None:
        from autonom_lib import providers
        self.assertIn("simulator.slim", providers.SEMANTIC_CAPABILITIES)
        ios = providers.LocalDeviceSession(self.target, {"tooling": {"idb": {"state": "ready"}}})
        with mock.patch.object(providers.LocalDeviceSession, "_device_class",
                               return_value="simulator"):
            found = {item.name: item for item in ios.capabilities().capabilities}
        self.assertEqual(found["simulator.slim"].state, "available")
        android = providers.LocalDeviceSession(
            Target(ANDROID, "emulator-5554", str(FAKE_ADB), {}), {})
        found = {item.name: item for item in android.capabilities().capabilities}
        self.assertEqual(found["simulator.slim"].state, "unavailable")


if __name__ == "__main__":
    unittest.main()
