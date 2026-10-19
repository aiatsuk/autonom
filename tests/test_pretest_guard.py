"""Pre-test guard (tp-006): budgets, forbidden targets, allowed apps.

The library tests drive the real ``ui``/``device_state``/``session`` functions
against the fake adb and fake simctl, which record every argv they receive, so
each refusal is proven to have sent nothing to the device. A temporary
``AUTONOM_HOME`` holds the session (``current.json``) and the pre-test. The CLI
tests prove the refusal reaches the journal through the normal error path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import device_state, errors, ios_simctl  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui as ui_mod  # noqa: E402
from autonom_lib.metrics import snapshot as snapshot_mod  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402
from autonom_lib.pretest import guard, store  # noqa: E402

SERIAL = "emulator-5554"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
APP = "com.example.app"

UI_DUMP = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,2400]">
        <node index="0" text="" resource-id="com.example.app:id/buy" class="android.view.View" package="com.example.app" content-desc="Купить" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,300][500,420]" />
        <node index="1" text="Continue" resource-id="com.example.app:id/next" class="android.widget.Button" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,600][500,720]" />
        <node index="2" text="" resource-id="com.example.app:id/row" class="android.widget.LinearLayout" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[0,1000][1080,1400]">
          <node index="0" text="Delete account" resource-id="com.example.app:id/danger" class="android.widget.TextView" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,1100][600,1200]" />
        </node>
      </node>
    </hierarchy>
    """)

# A clickable "Pay now" row whose label sits on a sibling of an unlabeled
# icon, inside a clickable full-screen layout (a layout, not one control).
ROW_DUMP = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="com.example.app:id/screen" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="false" scrollable="false" bounds="[0,0][1080,2400]">
        <node index="0" text="" resource-id="com.example.app:id/pay_row" class="android.widget.LinearLayout" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[0,500][1080,700]">
          <node index="0" text="" resource-id="com.example.app:id/pay_icon" class="android.widget.ImageView" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[20,520][180,680]" />
          <node index="1" text="Pay now" resource-id="com.example.app:id/pay_label" class="android.widget.TextView" package="com.example.app" content-desc="" clickable="false" enabled="true" focusable="false" scrollable="false" bounds="[200,560][700,640]" />
        </node>
        <node index="1" text="Help" resource-id="com.example.app:id/help" class="android.widget.Button" package="com.example.app" content-desc="" clickable="true" enabled="true" focusable="true" scrollable="false" bounds="[100,1500][500,1620]" />
      </node>
    </hierarchy>
    """)

EMPTY_DUMP = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<hierarchy rotation="0"></hierarchy>\n')

# A fake adb that answers `resolve-activity -d <url>` from the state file's
# `url_handlers` ({url: component}, "!fail" for a failing call) and hands
# every other call to the shared fake adb.
URL_FAKE = textwrap.dedent("""\
    #!{python}
    import json, os, shlex, subprocess, sys
    argv = sys.argv[1:]
    if "resolve-activity" in argv and "-d" in argv:
        with open(os.environ["AUTONOM_FAKE_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps({{"tool": "adb", "argv": argv}}) + "\\n")
        state = json.load(open(os.environ["AUTONOM_FAKE_STATE"], encoding="utf-8"))
        url = shlex.split(argv[argv.index("-d") + 1])[0]
        handler = (state.get("url_handlers") or {{}}).get(url)
        if handler == "!fail":
            sys.stderr.write("cmd: Failure calling service package\\n")
            raise SystemExit(255)
        if handler is None:
            print("No activity found")
        else:
            print("priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true")
            print(handler)
        raise SystemExit(0)
    raise SystemExit(subprocess.call([sys.executable, {fake!r}, *argv]))
    """)


class GuardBase(unittest.TestCase):
    """A temporary AUTONOM_HOME with a current session on the fake adb."""

    platform = "android"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "fake.log"
        self.state = self.root / "state.json"
        self.dump = self.root / "dump.xml"
        self.dump.write_text(UI_DUMP, encoding="utf-8")
        self.write_state({"devices": [[SERIAL, "device", ""]], "ui_dump": str(self.dump)})
        env = mock.patch.dict(os.environ, {
            "AUTONOM_HOME": str(self.root / "home"),
            "AUTONOM_FAKE_LOG": str(self.log),
            "AUTONOM_FAKE_STATE": str(self.state),
        })
        env.start()
        self.addCleanup(env.stop)
        home = session_mod.sessions_home()
        artifacts = home / "s_guard"
        artifacts.mkdir(parents=True)
        if self.platform == "ios":
            self.target = Target("ios", UDID, str(FAKE_SIMCTL), {"udid": UDID})
        else:
            self.target = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        self.session = {"session_id": "s_guard", "schema_version": 2,
                        "artifacts_dir": str(artifacts), "platform": self.platform,
                        "target_id": self.target.target_id, "app_id": APP,
                        "network": {"enabled": False}}
        self.write_session(self.session)

    # --- helpers ---------------------------------------------------------------

    def write_state(self, state: dict) -> None:
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def update_state(self, **values) -> None:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update(values)
        self.write_state(state)

    def write_session(self, record: dict) -> None:
        text = json.dumps(record)
        home = session_mod.sessions_home()
        (Path(record["artifacts_dir"]) / "session.json").write_text(text, encoding="utf-8")
        (home / "current.json").write_text(text, encoding="utf-8")

    def start_pretest(self, **budget) -> dict:
        target = SimpleNamespace(platform=self.platform, target_id=self.target.target_id)
        with mock.patch.object(device_state, "crash_list", return_value=[]), \
                mock.patch.object(snapshot_mod, "take",
                                  side_effect=errors.AutonomError("x", "no metrics")):
            record = store.create(self.session, target=target, ticket_text="t",
                                  ticket_ref=None, title=None, repo=None, base=None,
                                  head=None, flows_dir=None, app_id=None,
                                  budget=budget or None,
                                  context={"schema": "autonom.pretest-context/v1"})
        self.clear_log()
        return record

    def clear_log(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"] for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def inputs(self) -> list[list[str]]:
        """Calls that put input on the device (taps, swipes, text, keys, starts)."""
        out = []
        for argv in self.calls():
            words = " ".join(argv)
            if any(marker in words for marker in (
                    "shell input", "am start", "monkey", " launch ", "openurl")):
                out.append(argv)
            elif argv[:1] in (["launch"], ["openurl"]):
                out.append(argv)
        return out

    def budget(self, record: dict) -> dict:
        path = store.pretest_dir(self.session, record["pretest_id"]) / store.BUDGET_FILE
        return json.loads(path.read_text(encoding="utf-8"))

    def refused(self, code: str, fn, *args, **kwargs) -> dict:
        with self.assertRaises(errors.AutonomError) as caught:
            fn(*args, **kwargs)
        error = caught.exception
        self.assertEqual(error.code, code, error.message)
        payload = error.as_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], code)
        self.assertTrue(payload.get("hint"), f"{code} has no hint")
        self.assertIn("no input was sent", payload["error"])
        return payload


# --- no pre-test: nothing changes (tp-028) ---------------------------------------


class NoPretestTests(GuardBase):
    def test_tap_without_a_pretest_sends_exactly_the_input(self) -> None:
        ui_mod.tap(self.target, 300, 360, screen=(1080, 2400))
        self.assertEqual(self.calls(), [["-s", SERIAL, "shell", "input", "tap", "300", "360"]])
        self.assertFalse((Path(self.session["artifacts_dir"]) / "pretests").exists())

    def test_every_verb_without_a_pretest_adds_no_read(self) -> None:
        ui_mod.long_press(self.target, 10, 10, screen=(1080, 2400))
        ui_mod.double_tap(self.target, 10, 10, screen=(1080, 2400))
        ui_mod.swipe(self.target, 1, 2, 3, 4, 0.1, screen=(1080, 2400))
        ui_mod.type_text(self.target, "hello")
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        device_state.open_url(self.target, "myapp://x")
        session_mod.launch_app(str(FAKE_ADB), SERIAL, "com.other.app",
                               activity=".Main")
        self.assertFalse(any("uiautomator" in " ".join(c) or "resolve-activity" in " ".join(c)
                             for c in self.calls()), self.calls())
        self.assertEqual(len(self.inputs()), 8)

    def test_stop_and_clear_without_a_pretest_send_exactly_the_command(self) -> None:
        session_mod.force_stop(str(FAKE_ADB), SERIAL, "com.other.app")
        session_mod.clear_data(str(FAKE_ADB), SERIAL, "com.other.app")
        self.assertEqual(self.calls(), [
            ["-s", SERIAL, "shell", "am", "force-stop", "com.other.app"],
            ["-s", SERIAL, "shell", "pm", "clear", "com.other.app"]])

    def test_no_session_at_all_is_a_no_op(self) -> None:
        (session_mod.sessions_home() / "current.json").unlink()
        self.assertFalse(guard.before_mutation("tap", target=self.target, point=(1, 1)))
        self.assertEqual(self.calls(), [])

    def test_unreadable_current_session_is_a_no_op(self) -> None:
        (session_mod.sessions_home() / "current.json").write_text("{", encoding="utf-8")
        self.assertFalse(guard.before_mutation("tap", target=self.target, point=(1, 1)))

    def test_stopped_pretest_is_a_no_op(self) -> None:
        record = self.start_pretest(max_actions=1)
        store.stop(self.session)
        for _ in range(3):
            ui_mod.tap(self.target, 300, 360, screen=(1080, 2400))
        self.assertEqual(len(self.inputs()), 3)
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_stopped_session_is_a_no_op(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.write_session({**self.session, "stopped_at": "2026-10-10T10:00:00Z"})
        ui_mod.tap(self.target, 300, 360, screen=(1080, 2400))
        ui_mod.tap(self.target, 300, 360, screen=(1080, 2400))
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_another_target_is_not_governed(self) -> None:
        record = self.start_pretest(max_actions=1)
        other = Target("android", "emulator-5556", str(FAKE_ADB), {"serial": "emulator-5556"})
        ui_mod.tap(other, 300, 360, screen=(1080, 2400))
        ui_mod.tap(other, 300, 360, screen=(1080, 2400))
        self.assertEqual(self.budget(record)["actions_used"], 0)
        self.assertEqual(len(self.inputs()), 2)


# --- damaged active record (B.8 / RISK-042) ---------------------------------------


class DamagedRecordTests(GuardBase):
    def damage(self, record: dict, content: bytes = b"{") -> Path:
        path = store.pretest_dir(self.session, record["pretest_id"]) / store.RECORD_FILE
        path.write_bytes(content)
        return path

    def test_every_guarded_verb_refuses_and_sends_nothing(self) -> None:
        record = self.start_pretest(max_actions=50)
        path = self.damage(record)
        other = Target("android", "emulator-5556", str(FAKE_ADB), {"serial": "emulator-5556"})
        attempts = {
            "tap": lambda: ui_mod.tap(self.target, 300, 360, screen=(1080, 2400)),
            "long-press": lambda: ui_mod.long_press(self.target, 10, 10, screen=(1080, 2400)),
            "swipe": lambda: ui_mod.swipe(self.target, 1, 2, 3, 4, 0.1, screen=(1080, 2400)),
            "type": lambda: ui_mod.type_text(self.target, "hello"),
            "key": lambda: ui_mod.press_key(self.target, "KEYCODE_BACK"),
            "open": lambda: device_state.open_url(self.target, "myapp://x"),
            "launch": lambda: session_mod.launch_app(str(FAKE_ADB), SERIAL, APP,
                                                     activity=".Main"),
            "stop": lambda: session_mod.force_stop(str(FAKE_ADB), SERIAL, APP),
            "clear": lambda: session_mod.clear_data(str(FAKE_ADB), SERIAL, APP),
            # the damaged record holds the target: no device is exempt
            "tap on another target": lambda: ui_mod.tap(other, 1, 1, screen=(1080, 2400)),
        }
        for name, attempt in attempts.items():
            with self.subTest(verb=name):
                payload = self.refused(errors.PRETEST_DAMAGED, attempt)
                self.assertEqual(payload["pretest_id"], record["pretest_id"])
                self.assertEqual(payload["path"], str(path))
                self.assertIn(str(path), payload["error"])
                self.assertIn("not valid JSON", payload["reason"])
                self.assertEqual(payload["hint"],
                                 "autonom pretest stop --force, or restore the file")
                self.assertIn(payload["verb"], guard.MUTATING_VERBS)
                json.dumps(payload)
        self.assertEqual(self.inputs(), [])
        self.assertFalse(any("uiautomator" in " ".join(c) for c in self.calls()))
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 0)
        self.assertEqual({item["code"] for item in budget["refusals"]},
                         {errors.PRETEST_DAMAGED})
        self.assertEqual(len(budget["refusals"]), len(attempts))

    def test_before_mutation_refuses_directly(self) -> None:
        record = self.start_pretest()
        self.damage(record, json.dumps({**record, "status": "weird"}).encode())
        payload = self.refused(errors.PRETEST_DAMAGED, guard.before_mutation, "tap",
                               target=self.target, point=(1, 1))
        self.assertEqual(payload["verb"], "tap")
        self.assertIn("status must be one of", payload["reason"])

    def test_refusal_is_raised_even_when_budget_json_cannot_be_written(self) -> None:
        record = self.start_pretest()
        self.damage(record)
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            self.refused(errors.PRETEST_DAMAGED, guard.before_mutation, "tap",
                         target=self.target, point=(1, 1))
        self.assertEqual(self.inputs(), [])

    def test_exempt_input_is_not_refused(self) -> None:
        record = self.start_pretest()
        self.damage(record)
        with guard.exempt():
            self.assertFalse(guard.before_mutation("tap", target=self.target, point=(1, 1)))

    def test_usage_reports_damaged(self) -> None:
        record = self.start_pretest()
        path = self.damage(record)
        usage = guard.usage(self.session)
        self.assertEqual(usage["status"], "damaged")
        self.assertEqual(usage["pretest_id"], record["pretest_id"])
        self.assertEqual(usage["path"], str(path))
        self.assertIn("not valid JSON", usage["reason"])

    def test_after_stop_the_guard_is_a_no_op_again(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.damage(record)
        stopped = store.stop(self.session, force=True)
        self.assertEqual(stopped["status"], "stopped")
        ui_mod.tap(self.target, 300, 360, screen=(1080, 2400))
        ui_mod.tap(self.target, 300, 360, screen=(1080, 2400))
        self.assertEqual(len(self.inputs()), 2)
        self.assertEqual(guard.usage(self.session), {})

    def test_a_stale_pointer_without_a_record_is_still_a_no_op(self) -> None:
        record = self.start_pretest(max_actions=1)
        (store.pretest_dir(self.session, record["pretest_id"]) / store.RECORD_FILE).unlink()
        self.assertFalse(guard.before_mutation("tap", target=self.target, point=(1, 1)))
        self.assertEqual(guard.usage(self.session), {})


# --- budgets -----------------------------------------------------------------------


class BudgetTests(GuardBase):
    def test_third_tap_refused_after_two_and_nothing_sent(self) -> None:
        record = self.start_pretest(max_actions=2)
        ui_mod.tap(self.target, 300, 650, screen=(1080, 2400), node={"text": "Continue"})
        ui_mod.tap(self.target, 300, 650, screen=(1080, 2400), node={"text": "Continue"})
        self.clear_log()
        payload = self.refused(errors.BUDGET_EXHAUSTED, ui_mod.tap, self.target, 300, 650,
                               screen=(1080, 2400), node={"text": "Continue"})
        self.assertEqual(self.calls(), [])
        self.assertEqual(payload["actions_used"], 2)
        self.assertEqual(payload["max_actions"], 2)
        self.assertEqual(payload["verb"], "tap")
        self.assertEqual(payload["pretest_id"], record["pretest_id"])
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 2)
        self.assertEqual(len(budget["refusals"]), 1)
        refusal = budget["refusals"][0]
        self.assertEqual(set(refusal), {"code", "verb", "ts"})
        self.assertEqual((refusal["code"], refusal["verb"]), ("budget_exhausted", "tap"))

    def test_every_mutating_verb_counts_once(self) -> None:
        record = self.start_pretest()
        ui_mod.tap(self.target, 300, 650, screen=(1080, 2400), node={"text": "Continue"})
        ui_mod.long_press(self.target, 300, 650, screen=(1080, 2400), node={"text": "Continue"})
        ui_mod.double_tap(self.target, 300, 650, screen=(1080, 2400), node={"text": "Continue"})
        ui_mod.swipe(self.target, 1, 2, 3, 4, 0.1, screen=(1080, 2400))
        ui_mod.type_text(self.target, "hello")
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        self.assertEqual(self.budget(record)["actions_used"], 6)
        # the double tap is one action but two inputs
        self.assertEqual(len([c for c in self.calls() if c[3:5] == ["input", "tap"]]), 3)

    def test_before_mutation_reports_whether_it_counted(self) -> None:
        record = self.start_pretest()
        self.assertIs(guard.before_mutation("key", target=self.target), True)
        with guard.exempt():
            self.assertIs(guard.before_mutation("key", target=self.target), False)
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_minutes_budget_refuses_before_any_command(self) -> None:
        record = self.start_pretest(max_minutes=1)
        started = guard._epoch(record["created_at"])
        with mock.patch.object(guard, "_clock", return_value=started + 61):
            payload = self.refused(errors.BUDGET_EXHAUSTED, ui_mod.type_text,
                                   self.target, "x")
        self.assertIn("minute", payload["error"])
        self.assertGreaterEqual(payload["minutes_elapsed"], 1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.budget(record)["actions_used"], 0)
        self.assertEqual(self.budget(record)["refusals"][0]["verb"], "type")

    def test_within_minutes_is_counted(self) -> None:
        record = self.start_pretest(max_minutes=1)
        started = guard._epoch(record["created_at"])
        with mock.patch.object(guard, "_clock", return_value=started + 59):
            ui_mod.press_key(self.target, "KEYCODE_BACK")
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_swipe_type_key_and_open_hit_the_budget(self) -> None:
        self.start_pretest(max_actions=1)
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        self.clear_log()
        for fn, args in ((ui_mod.swipe, (self.target, 1, 2, 3, 4, 0.1)),
                         (ui_mod.type_text, (self.target, "x")),
                         (ui_mod.press_key, (self.target, "KEYCODE_HOME"))):
            kwargs = {"screen": (1080, 2400)} if fn is ui_mod.swipe else {}
            self.refused(errors.BUDGET_EXHAUSTED, fn, *args, **kwargs)
        self.refused(errors.BUDGET_EXHAUSTED, device_state.open_url, self.target, "myapp://x")
        self.refused(errors.BUDGET_EXHAUSTED, session_mod.launch_app,
                     str(FAKE_ADB), SERIAL, APP)
        self.assertEqual(self.calls(), [])

    def test_refused_point_outside_the_screen_costs_no_action(self) -> None:
        record = self.start_pretest(max_actions=1)
        with self.assertRaises(errors.AutonomError) as caught:
            ui_mod.tap(self.target, 5000, 5000, screen=(1080, 2400))
        self.assertEqual(caught.exception.code, errors.COORDINATE_SPACE_MISMATCH)
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_android_gesture_refusal_costs_no_action(self) -> None:
        record = self.start_pretest()
        with self.assertRaises(errors.AutonomError) as caught:
            ui_mod.gesture(self.target, "pinch")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_ON_PLATFORM)
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_refusals_accumulate(self) -> None:
        record = self.start_pretest(max_actions=1)
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        for _ in range(3):
            with self.assertRaises(errors.AutonomError):
                ui_mod.press_key(self.target, "KEYCODE_BACK")
        refusals = self.budget(record)["refusals"]
        self.assertEqual([r["verb"] for r in refusals], ["key", "key", "key"])

    def test_refusal_is_raised_even_when_it_cannot_be_recorded(self) -> None:
        self.start_pretest(max_actions=1)
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            self.refused(errors.BUDGET_EXHAUSTED, ui_mod.press_key, self.target, "KEYCODE_BACK")

    def test_pretest_directory_vanishing_mid_call_is_not_an_error(self) -> None:
        record = self.start_pretest()
        gone = errors.AutonomError(errors.NO_ACTIVE_PRETEST, "gone", hint="h")
        with mock.patch.object(store, "lock", side_effect=gone):
            self.assertFalse(guard.before_mutation("key", target=self.target))
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_two_processes_never_exceed_the_budget(self) -> None:
        self.start_pretest(max_actions=7)
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT / 'scripts')!r})
            from autonom_lib import errors
            from autonom_lib.pretest import guard
            ok = 0
            for _ in range(6):
                try:
                    guard.before_mutation("key")
                    ok += 1
                except errors.AutonomError as exc:
                    assert exc.code == "budget_exhausted", exc.code
            print(ok)
            """)
        workers = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env={**os.environ})
                   for _ in range(2)]
        total = 0
        for worker in workers:
            out, err = worker.communicate(timeout=120)
            self.assertEqual(worker.returncode, 0, err)
            total += int(out.strip())
        self.assertEqual(total, 7)
        usage = guard.usage(self.session)
        self.assertEqual(usage["actions_used"], 7)
        self.assertEqual(usage["refusals"], 5)


# --- forbidden targets -------------------------------------------------------------


class ForbidTests(GuardBase):
    def test_default_patterns_refuse_a_node_label(self) -> None:
        record = self.start_pretest()
        self.assertEqual(record["budget"]["forbid_text"], list(guard.DEFAULT_FORBID))
        payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 300, 360,
                               screen=(1080, 2400), node={"desc": "Купить"})
        self.assertEqual(payload["field"], "desc")
        self.assertEqual(payload["pattern"], "купить")
        # a node is known: not even a tree read reaches the device
        self.assertEqual(self.calls(), [])
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 0)
        self.assertEqual(budget["refusals"][0]["code"], "forbidden_action")

    def test_patterns_are_case_insensitive_regex_on_text_desc_label(self) -> None:
        self.start_pretest(forbid_text=[r"^buy\b"])
        for node in ({"text": "BUY now"}, {"label": "Buy"}, {"desc": "buy it"}):
            self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 1, 1,
                         screen=(1080, 2400), node=node)
        ui_mod.tap(self.target, 1, 1, screen=(1080, 2400), node={"text": "Go buy"})
        self.assertEqual(len(self.inputs()), 1)

    def test_coordinate_tap_resolves_the_node_from_a_fresh_tree(self) -> None:
        record = self.start_pretest()
        payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 300, 360,
                               screen=(1080, 2400))
        self.assertEqual(payload["field"], "desc")
        calls = self.calls()
        self.assertTrue(calls and all("uiautomator" in " ".join(c) for c in calls), calls)
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_smallest_containing_node_decides(self) -> None:
        record = self.start_pretest()
        # inside the "Delete account" child: refused
        payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 300, 1150,
                               screen=(1080, 2400))
        self.assertEqual(payload["matched"], "target")
        # the empty, non-clickable screen layout: nothing forbidden takes the tap
        ui_mod.tap(self.target, 900, 2000, screen=(1080, 2400))
        ui_mod.tap(self.target, 300, 650, screen=(1080, 2400))  # "Continue"
        self.assertEqual(len(self.inputs()), 2)
        self.assertEqual(self.budget(record)["actions_used"], 2)

    def test_point_on_a_clickable_row_outside_its_label_is_refused(self) -> None:
        record = self.start_pretest()
        # inside com.example.app:id/row, outside its "Delete account" child
        for x, y in ((800, 1150), (900, 1300)):
            payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, x, y,
                                   screen=(1080, 2400))
            self.assertEqual(payload["matched"], "descendant")
            self.assertEqual(payload["field"], "text")
            self.assertEqual(payload["pattern"], "delete")
            self.assertIn("inside the control", payload["error"])
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_selector_tap_on_a_row_checks_the_labels_inside(self) -> None:
        record = self.start_pretest()
        nodes = ui_mod.snapshot(self.target)  # what `ui tap --resource-id` resolves from
        row = next(n for n in nodes if n.get("resource_id") == "com.example.app:id/row")
        self.assertFalse(row.get("text") or row.get("desc"))
        self.clear_log()
        x, y = ui_mod.center_of(row)
        for fn in (ui_mod.tap, ui_mod.long_press, ui_mod.double_tap):
            payload = self.refused(errors.FORBIDDEN_ACTION, fn, self.target, x, y,
                                   screen=(1080, 2400), node=row)
            self.assertIn(payload["matched"], ("descendant", "target"))
        # the node came from this thread's last snapshot: no second tree read
        self.assertEqual(self.calls(), [])
        # a copy of the node (same ref and bounds) is found in that tree too
        payload = self.refused(errors.FORBIDDEN_ACTION, guard.before_mutation, "tap",
                               target=self.target, node=dict(row))
        self.assertEqual(payload["matched"], "descendant")
        self.assertEqual(payload["node_ref"],
                         next(n["ref"] for n in nodes if n.get("text") == "Delete account"))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_selector_tap_on_an_allowed_control_reads_nothing_more(self) -> None:
        record = self.start_pretest()
        nodes = ui_mod.snapshot(self.target)
        button = next(n for n in nodes if n.get("text") == "Continue")
        self.clear_log()
        x, y = ui_mod.center_of(button)
        ui_mod.tap(self.target, x, y, screen=(1080, 2400), node=button)
        self.assertEqual(self.calls(), [["-s", SERIAL, "shell", "input", "tap", str(x), str(y)]])
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_unlabeled_part_of_a_control_is_refused_by_its_sibling_label(self) -> None:
        self.start_pretest()
        self.dump.write_text(ROW_DUMP, encoding="utf-8")
        # the icon is not clickable: the row takes the tap, and its label says "Pay now"
        payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 100, 600,
                               screen=(1080, 2400))
        self.assertEqual((payload["matched"], payload["pattern"]), ("descendant", "pay"))
        nodes = ui_mod.snapshot(self.target)
        icon = next(n for n in nodes if n.get("resource_id") == "com.example.app:id/pay_icon")
        self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 100, 600,
                     screen=(1080, 2400), node=icon)
        self.assertEqual(self.inputs(), [])

    def test_a_screen_sized_clickable_layout_is_not_one_control(self) -> None:
        record = self.start_pretest()
        self.dump.write_text(ROW_DUMP, encoding="utf-8")
        # the clickable full-screen layout holds "Pay now" deep inside, but a tap
        # on its empty part or on "Help" does not fire the row
        ui_mod.tap(self.target, 900, 2000, screen=(1080, 2400))
        ui_mod.tap(self.target, 300, 1560, screen=(1080, 2400))
        self.assertEqual(len(self.inputs()), 2)
        self.assertEqual(self.budget(record)["actions_used"], 2)

    def test_label_on_an_enclosing_control_is_refused(self) -> None:
        nodes = [
            {"ref": "root", "bounds": [0, 0, 1000, 2000], "parent": None},
            {"ref": "card", "bounds": [0, 0, 400, 200], "clickable": True,
             "desc": "Buy the plan", "parent": "root"},
            {"ref": "art", "bounds": [10, 10, 100, 100], "parent": "card"},
        ]
        relations = {(n["ref"], rel) for n, rel in guard._scope(nodes, nodes[2], 2000000)}
        self.assertEqual(relations, {("art", "target"), ("card", "ancestor")})
        # no enclosing control: only the node itself
        self.assertEqual([n["ref"] for n, _ in guard._scope(nodes[:1] + nodes[2:], nodes[2],
                                                             2000000)], ["art"])
        # a parent cycle in a broken tree ends the walk
        loop = [{"ref": "a", "parent": "b", "bounds": [0, 0, 1, 1]},
                {"ref": "b", "parent": "a", "bounds": [0, 0, 1, 1]}]
        self.assertEqual([n["ref"] for n, _ in guard._scope(loop, loop[0], 100)], ["a"])

    def test_named_node_not_in_the_last_tree_reads_a_fresh_one(self) -> None:
        self.start_pretest()
        ui_mod.tap(self.target, 300, 650, screen=(1080, 2400), node={"text": "Continue"})
        self.assertTrue(any("uiautomator" in " ".join(c) for c in self.calls()))

    def test_named_node_with_an_unreadable_tree_refuses(self) -> None:
        record = self.start_pretest()
        self.update_state(fail={f"-s {SERIAL} exec-out uiautomator dump": [1, "ERROR"]})
        payload = self.refused(errors.FORBIDDEN_ACTION, guard.before_mutation, "tap",
                               target=self.target, node={"text": "Continue", "ref": "x"})
        self.assertNotIn("point", payload)
        self.assertIn("around the target", payload["error"])
        self.assertIn("DEC-007", payload["hint"])
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_named_node_without_a_target_checks_only_its_own_fields(self) -> None:
        record = self.start_pretest()
        with mock.patch.object(guard, "_governed", return_value=True):
            guard.before_mutation("tap", node={"text": "Continue"})
            self.refused(errors.FORBIDDEN_ACTION, guard.before_mutation, "tap",
                         node={"text": "Delete"})
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_last_snapshot_is_per_target_and_per_thread(self) -> None:
        nodes = ui_mod.snapshot(self.target)
        self.assertIs(ui_mod.last_snapshot(self.target), nodes)
        other = Target("android", "emulator-5556", str(FAKE_ADB), {"serial": "emulator-5556"})
        self.assertIsNone(ui_mod.last_snapshot(other))
        seen: list = []
        worker = threading.Thread(target=lambda: seen.append(ui_mod.last_snapshot(self.target)))
        worker.start()
        worker.join(30)
        self.assertEqual(seen, [None])

    def test_long_press_and_double_tap_are_checked_by_point(self) -> None:
        self.start_pretest()
        self.refused(errors.FORBIDDEN_ACTION, ui_mod.long_press, self.target, 300, 360,
                     screen=(1080, 2400))
        self.refused(errors.FORBIDDEN_ACTION, ui_mod.double_tap, self.target, 300, 360,
                     screen=(1080, 2400))
        self.assertEqual(self.inputs(), [])

    def test_swipe_over_a_forbidden_label_is_not_a_tap(self) -> None:
        self.start_pretest()
        ui_mod.swipe(self.target, 300, 360, 300, 1150, 0.2, screen=(1080, 2400))
        self.assertEqual(len(self.inputs()), 1)
        self.assertFalse(any("uiautomator" in " ".join(c) for c in self.calls()))

    def test_unreadable_tree_refuses_with_a_hint(self) -> None:
        record = self.start_pretest()
        self.update_state(fail={f"-s {SERIAL} exec-out uiautomator dump": [1, "ERROR"]})
        payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 900, 1300,
                               screen=(1080, 2400))
        self.assertIn("DEC-007", payload["hint"])
        self.assertEqual(payload["point"], [900, 1300])
        self.assertTrue(payload["reason"])
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.budget(record)["refusals"][0]["code"], "forbidden_action")

    def test_empty_tree_refuses(self) -> None:
        self.start_pretest()
        self.dump.write_text(EMPTY_DUMP, encoding="utf-8")
        payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 10, 10,
                               screen=(1080, 2400))
        self.assertEqual(payload["reason"], "empty tree")
        self.assertEqual(self.inputs(), [])

    def test_no_patterns_means_no_tree_read(self) -> None:
        record = self.start_pretest(forbid_text=[])
        ui_mod.tap(self.target, 300, 360, screen=(1080, 2400))
        self.assertFalse(any("uiautomator" in " ".join(c) for c in self.calls()))
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_invalid_stored_pattern_falls_back_to_a_literal(self) -> None:
        record = self.start_pretest()
        record["budget"]["forbid_text"] = ["pay("]
        store.save(self.session, record)
        self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 1, 1,
                     screen=(1080, 2400), node={"text": "PAY(now)"})
        ui_mod.tap(self.target, 1, 1, screen=(1080, 2400), node={"text": "pay"})

    def test_no_target_with_a_point_refuses(self) -> None:
        self.start_pretest()
        with mock.patch.object(guard, "_governed", return_value=True):
            payload = self.refused(errors.FORBIDDEN_ACTION, guard.before_mutation, "tap",
                                   point=(1, 1))
        self.assertEqual(payload["reason"], "no target to read")

    def test_node_at(self) -> None:
        nodes = [
            {"ref": "root", "bounds": [0, 0, 100, 100]},
            {"ref": "hidden", "bounds": [10, 10, 20, 20], "visible": False},
            {"ref": "flat", "bounds": [10, 10, 10, 20]},
            {"ref": "a", "bounds": [5, 5, 30, 30]},
            {"ref": "b", "bounds": [5, 5, 30, 30]},
            {"ref": "nobounds"},
            {"ref": "garbage", "bounds": ["x", 0, 1]},
            {"ref": "strings", "bounds": ["60", "60", "70", "70"]},
        ]
        self.assertEqual(guard.node_at(nodes, 65, 65)["ref"], "strings")
        self.assertEqual(guard.node_at(nodes, 15, 15)["ref"], "b")
        self.assertEqual(guard.node_at(nodes, 50, 50)["ref"], "root")
        self.assertEqual(guard.node_at(nodes, 30, 30)["ref"], "b")  # edges are inside
        self.assertIsNone(guard.node_at(nodes, 500, 500))


# --- allowed apps ------------------------------------------------------------------


# The screen after "Continue" opened a dialog: a "Pay now" button now sits
# exactly where "Continue" was ([100,600][500,720]).
DIALOG_DUMP = UI_DUMP.replace('text="Continue"', 'text="Pay now"').replace(
    'id/next"', 'id/pay"')


class FreshTreePerPointTapTests(GuardBase):
    """Fix round 5, item 1: no point-tap resolution cache. Every guarded point
    tap reads a fresh tree, so a tap that changed the screen cannot let a
    following point tap on a forbidden control at the same point through."""

    def dumps(self) -> int:
        return sum(1 for argv in self.calls() if "uiautomator" in " ".join(argv))

    def taps(self) -> list:
        return [c for c in self.inputs() if "tap" in c]

    def test_a_changed_screen_refuses_the_next_tap_at_the_same_point(self) -> None:
        record = self.start_pretest()
        clock = [100.0]
        with mock.patch.object(guard, "_monotonic", side_effect=lambda: clock[0]):
            ui_mod.tap(self.target, 300, 660, screen=(1080, 2400))  # "Continue"
            self.assertEqual(len(self.taps()), 1)
            self.dump.write_text(DIALOG_DUMP, encoding="utf-8")  # the tap opened a dialog
            clock[0] += 0.5  # well inside 2 s
            payload = self.refused(errors.FORBIDDEN_ACTION, ui_mod.tap, self.target, 300, 660,
                                 screen=(1080, 2400))
        self.assertEqual(len(self.taps()), 1)  # the second tap sent no input
        self.assertEqual(self.dumps(), 2)
        self.assertEqual(self.budget(record)["actions_used"], 1)
        refusals = self.budget(record)["refusals"]
        self.assertEqual([r["code"] for r in refusals], [errors.FORBIDDEN_ACTION])
        self.assertEqual(payload["verb"], "tap")
        self.assertEqual(payload["pretest_id"], record["pretest_id"])
        self.assertRegex(payload["pattern"], "pay")

    def test_repeated_point_taps_read_the_tree_each_time(self) -> None:
        record = self.start_pretest()
        for offset in range(3):  # inside "Continue"
            ui_mod.tap(self.target, 300 + offset, 660, screen=(1080, 2400))
        self.assertEqual(self.dumps(), 3)
        self.assertEqual(self.budget(record)["actions_used"], 3)
        self.assertEqual(len(self.taps()), 3)

    def test_the_cache_is_gone(self) -> None:
        for name in ("POINT_CACHE_SECONDS", "_POINT_CACHE", "_cached_point",
                     "_remember_point", "forget_points"):
            self.assertFalse(hasattr(guard, name), name)


class AllowAppsTests(GuardBase):
    def install_url_fake(self) -> str:
        path = self.root / "url_adb.py"
        path.write_text(URL_FAKE.format(python=sys.executable, fake=str(FAKE_ADB)),
                        encoding="utf-8")
        path.chmod(0o700)
        self.target = Target("android", SERIAL, str(path), {"serial": SERIAL})
        return str(path)

    def test_default_allow_list_is_the_session_app(self) -> None:
        record = self.start_pretest()
        self.assertEqual(record["budget"]["allow_apps"], [APP])

    def test_launch_outside_the_allow_list_is_refused(self) -> None:
        record = self.start_pretest()
        for fn in (session_mod.launch_app, session_mod.launch_app_fresh):
            payload = self.refused(errors.APP_NOT_ALLOWED, fn, str(FAKE_ADB), SERIAL,
                                   "com.android.settings")
            self.assertEqual(payload["app_id"], "com.android.settings")
            self.assertEqual(payload["allow_apps"], [APP])
            self.assertEqual(payload["verb"], "launch")
        self.assertEqual(self.calls(), [])
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 0)
        self.assertEqual([r["code"] for r in budget["refusals"]],
                         ["app_not_allowed", "app_not_allowed"])

    def test_launch_of_another_apps_component_is_refused(self) -> None:
        record = self.start_pretest()
        for fn in (session_mod.launch_app, session_mod.launch_app_fresh):
            payload = self.refused(errors.APP_NOT_ALLOWED, fn, str(FAKE_ADB), SERIAL, APP,
                                   activity="com.android.vending/.AssetBrowserActivity")
            self.assertEqual(payload["app_id"], "com.android.vending")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.budget(record)["actions_used"], 0)
        # the app's own component, a short activity name, or an allowed
        # component named under another app_id all start an allowed package
        session_mod.launch_app(str(FAKE_ADB), SERIAL, APP, activity=f"{APP}/.MainActivity")
        session_mod.launch_app_fresh(str(FAKE_ADB), SERIAL, APP, activity=".MainActivity")
        session_mod.launch_app(str(FAKE_ADB), SERIAL, "com.other.app",
                               activity=f"{APP}/.MainActivity")
        starts = [" ".join(c) for c in self.inputs() if "am start" in " ".join(c)]
        self.assertEqual(len(starts), 3)
        self.assertFalse(any("com.android.vending" in c or "com.other.app/" in c
                             for c in starts), starts)
        self.assertEqual(self.budget(record)["actions_used"], 3)

    def test_stop_and_clear_of_another_app_are_refused(self) -> None:
        record = self.start_pretest()
        for fn, verb in ((session_mod.force_stop, "stop"), (session_mod.clear_data, "clear")):
            payload = self.refused(errors.APP_NOT_ALLOWED, fn, str(FAKE_ADB), SERIAL,
                                   "com.android.settings")
            self.assertEqual(payload["verb"], verb)
            self.assertEqual(payload["app_id"], "com.android.settings")
            self.assertEqual(payload["allow_apps"], [APP])
        self.assertEqual(self.calls(), [])
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 0)
        self.assertEqual([(r["code"], r["verb"]) for r in budget["refusals"]],
                         [("app_not_allowed", "stop"), ("app_not_allowed", "clear")])

    def test_stop_and_clear_of_the_session_app_are_counted(self) -> None:
        record = self.start_pretest(max_actions=2)
        session_mod.force_stop(str(FAKE_ADB), SERIAL, APP)
        session_mod.clear_data(str(FAKE_ADB), SERIAL, APP)
        self.assertEqual(self.calls(), [
            ["-s", SERIAL, "shell", "am", "force-stop", APP],
            ["-s", SERIAL, "shell", "pm", "clear", APP]])
        self.clear_log()
        for fn in (session_mod.force_stop, session_mod.clear_data):
            self.refused(errors.BUDGET_EXHAUSTED, fn, str(FAKE_ADB), SERIAL, APP)
        self.assertEqual(self.calls(), [])
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 2)
        self.assertEqual([(r["code"], r["verb"]) for r in budget["refusals"]],
                         [("budget_exhausted", "stop"), ("budget_exhausted", "clear")])

    def test_exempt_stop_and_clear_are_not_counted(self) -> None:
        record = self.start_pretest(max_actions=1)
        with guard.exempt():
            session_mod.force_stop(str(FAKE_ADB), SERIAL, "com.android.settings")
            session_mod.clear_data(str(FAKE_ADB), SERIAL, APP)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.budget(record), {"actions_used": 0, "refusals": []})

    def test_launched_package(self) -> None:
        self.assertEqual(session_mod._launched_package(APP, None), APP)
        self.assertEqual(session_mod._launched_package(APP, ".Main"), APP)
        self.assertEqual(session_mod._launched_package(APP, "com.x.y/.Main"), "com.x.y")
        self.assertEqual(session_mod._launched_package(APP, "/.Main"), APP)

    def test_launch_of_an_allowed_app_is_counted(self) -> None:
        record = self.start_pretest(allow_apps=[APP, "com.example.helper"])
        session_mod.launch_app(str(FAKE_ADB), SERIAL, APP)
        session_mod.launch_app_fresh(str(FAKE_ADB), SERIAL, "com.example.helper")
        self.assertEqual(self.budget(record)["actions_used"], 2)
        self.assertEqual(len([c for c in self.inputs() if "am start" in " ".join(c)]), 2)

    def test_open_resolves_the_handler_and_refuses_another_app(self) -> None:
        record = self.start_pretest()
        self.install_url_fake()
        self.update_state(url_handlers={
            "https://shop.example.com/x": "com.android.chrome/.Main",
            "myapp://cart": f"{APP}/.MainActivity",
            "both://x": "android/com.android.internal.app.ResolverActivity",
            "broken://x": "!fail",
        })
        payload = self.refused(errors.APP_NOT_ALLOWED, device_state.open_url, self.target,
                               "https://shop.example.com/x")
        self.assertEqual(payload["app_id"], "com.android.chrome")
        payload = self.refused(errors.APP_NOT_ALLOWED, device_state.open_url, self.target,
                               "both://x")
        # an unverified link resolves to the system chooser: said so, not app "android"
        self.assertIn("opens the system app chooser", payload["error"])
        self.assertIn("not verified for an allowed app", payload["error"])
        self.assertTrue(payload["chooser"])
        self.assertNotIn("app_id", payload)
        self.assertNotIn("android is not", payload["error"])
        payload = self.refused(errors.APP_NOT_ALLOWED, device_state.open_url, self.target,
                               "broken://x")
        self.assertIn("could not tell", payload["error"])
        self.assertEqual(self.inputs(), [])
        device_state.open_url(self.target, "myapp://cart")
        device_state.open_url(self.target, "nothing://handles-this")
        self.assertEqual(len([c for c in self.inputs() if "am start" in " ".join(c)]), 2)
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 2)
        self.assertEqual([r["verb"] for r in budget["refusals"]], ["open"] * 3)

    def test_open_with_an_empty_allow_list_does_not_resolve(self) -> None:
        record = self.start_pretest()
        record["budget"]["allow_apps"] = []
        store.save(self.session, record)
        self.install_url_fake()
        device_state.open_url(self.target, "https://anything.example.com")
        self.assertFalse(any("resolve-activity" in " ".join(c) for c in self.calls()))
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_invalid_url_is_refused_before_the_guard(self) -> None:
        record = self.start_pretest()
        with self.assertRaises(errors.AutonomError) as caught:
            device_state.open_url(self.target, "not a url")
        self.assertEqual(caught.exception.code, errors.INVALID_URL)
        self.assertEqual(self.budget(record)["actions_used"], 0)

    def test_exempt_skips_counting_and_refusals(self) -> None:
        record = self.start_pretest(max_actions=1)
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        with guard.exempt():
            ui_mod.tap(self.target, 300, 360, screen=(1080, 2400), node={"desc": "Купить"})
            session_mod.launch_app(str(FAKE_ADB), SERIAL, "com.android.settings")
            with guard.exempt():
                ui_mod.press_key(self.target, "KEYCODE_BACK")
            ui_mod.press_key(self.target, "KEYCODE_BACK")
        self.assertFalse(guard.is_exempt())
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 1)
        self.assertEqual(budget["refusals"], [])
        self.refused(errors.BUDGET_EXHAUSTED, ui_mod.press_key, self.target, "KEYCODE_BACK")

    def test_exempt_is_thread_local(self) -> None:
        self.start_pretest(max_actions=1)
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        seen: list[str] = []

        def other() -> None:
            try:
                guard.before_mutation("key", target=self.target)
            except errors.AutonomError as exc:
                seen.append(exc.code)

        with guard.exempt():
            worker = threading.Thread(target=other)
            worker.start()
            worker.join(30)
        self.assertEqual(seen, ["budget_exhausted"])

    def test_read_only_verbs_are_never_counted_or_refused(self) -> None:
        record = self.start_pretest(max_actions=1)
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        self.assertTrue(ui_mod.snapshot(self.target))
        nodes, _warnings = ui_mod.tree(self.target)
        self.assertTrue(nodes)
        self.assertTrue(ui_mod.screen_size(self.target))
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 1)
        self.assertEqual(budget["refusals"], [])

    def test_usage(self) -> None:
        self.assertEqual(guard.usage(self.session), {})
        self.assertEqual(guard.usage(None), {})
        record = self.start_pretest(max_actions=3, max_minutes=10)
        ui_mod.press_key(self.target, "KEYCODE_BACK")
        with self.assertRaises(errors.AutonomError):
            session_mod.launch_app(str(FAKE_ADB), SERIAL, "com.android.settings")
        usage = guard.usage(self.session)
        self.assertEqual(usage["pretest_id"], record["pretest_id"])
        self.assertEqual((usage["actions_used"], usage["max_actions"], usage["actions_left"]),
                         (1, 3, 2))
        self.assertEqual(usage["max_minutes"], 10)
        self.assertLessEqual(usage["minutes_elapsed"], 10)
        self.assertEqual(usage["allow_apps"], [APP])
        self.assertEqual(usage["refusals"], 1)
        self.assertEqual(usage["last_refusal"]["code"], "app_not_allowed")
        self.assertEqual(guard.usage(self.session, record)["actions_used"], 1)


class IosTests(GuardBase):
    platform = "ios"

    def setUp(self) -> None:
        super().setUp()
        self.update_state(installed=[APP, "com.apple.Preferences"])

    def test_ios_launch_through_the_session_layer_is_guarded(self) -> None:
        record = self.start_pretest()
        payload = self.refused(errors.APP_NOT_ALLOWED, session_mod.launch_ios_app,
                               str(FAKE_SIMCTL), UDID, "com.apple.Preferences")
        self.assertEqual(payload["app_id"], "com.apple.Preferences")
        self.assertEqual(self.calls(), [])
        pid = session_mod.launch_ios_app(str(FAKE_SIMCTL), UDID, APP, env={"A": "1"})
        self.assertEqual(pid, 4242)
        self.assertIn(["simctl", "launch", UDID, APP], self.calls())
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_ios_launch_without_a_pretest_is_unchanged(self) -> None:
        self.assertEqual(session_mod.launch_ios_app(str(FAKE_SIMCTL), UDID, APP), 4242)
        self.assertEqual(ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP), 4242)
        self.assertIn(["simctl", "launch", UDID, APP], self.calls())

    def test_simctl_launch_itself_is_the_guarded_choke_point(self) -> None:
        # `session start --launch`, `session launch`, the flow executor's
        # launchApp step and the Canvas launch all call ios_simctl.launch.
        from autonom_lib.flow import executor as executor_mod

        self.assertIs(executor_mod.ios_simctl.launch, ios_simctl.launch)
        self.assertTrue(getattr(ios_simctl.launch, "pretest_guarded", False))
        record = self.start_pretest(max_actions=1)
        payload = self.refused(errors.APP_NOT_ALLOWED, ios_simctl.launch,
                               str(FAKE_SIMCTL), UDID, "com.apple.Preferences")
        self.assertEqual(payload["app_id"], "com.apple.Preferences")
        self.assertEqual(payload["verb"], "launch")
        self.assertEqual(self.calls(), [])  # not even the listapps install check
        self.assertEqual(ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP,
                                           args=["-x"], env={"A": "1"}), 4242)
        self.assertIn(["simctl", "launch", UDID, APP, "-x"], self.calls())
        self.clear_log()
        self.refused(errors.BUDGET_EXHAUSTED, ios_simctl.launch, str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.calls(), [])
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 1)
        self.assertEqual([r["code"] for r in budget["refusals"]],
                         ["app_not_allowed", "budget_exhausted"])
        self.assertEqual({r["verb"] for r in budget["refusals"]}, {"launch"})

    def test_simctl_launch_guard_is_installed_once(self) -> None:
        guarded = ios_simctl.launch
        session_mod._install_ios_guards()  # noqa: SLF001 - a module reload
        self.assertIs(ios_simctl.launch, guarded)
        record = self.start_pretest()
        ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_simctl_terminate_is_guarded(self) -> None:
        # `session force-stop`, the flow executor's stopApp step and every
        # fresh launch call ios_simctl.terminate.
        from autonom_lib.flow import executor as executor_mod

        self.assertIs(executor_mod.ios_simctl.terminate, ios_simctl.terminate)
        self.assertTrue(getattr(ios_simctl.terminate, "pretest_guarded", False))
        record = self.start_pretest(max_actions=1)
        payload = self.refused(errors.APP_NOT_ALLOWED, ios_simctl.terminate,
                               str(FAKE_SIMCTL), UDID, "com.apple.Preferences")
        self.assertEqual((payload["verb"], payload["app_id"]), ("stop", "com.apple.Preferences"))
        self.assertEqual(self.calls(), [])
        self.assertIs(ios_simctl.terminate(str(FAKE_SIMCTL), UDID, APP), True)
        self.assertEqual(self.calls(), [["simctl", "terminate", UDID, APP]])
        self.clear_log()
        self.refused(errors.BUDGET_EXHAUSTED, ios_simctl.terminate, str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.calls(), [])
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 1)
        self.assertEqual([(r["code"], r["verb"]) for r in budget["refusals"]],
                         [("app_not_allowed", "stop"), ("budget_exhausted", "stop")])

    def test_simctl_terminate_without_a_pretest_is_unchanged(self) -> None:
        ios_simctl.terminate(str(FAKE_SIMCTL), UDID, "com.apple.Preferences")
        self.assertEqual(self.calls(), [["simctl", "terminate", UDID, "com.apple.Preferences"]])

    def test_simctl_terminate_guard_is_installed_once(self) -> None:
        guarded = ios_simctl.terminate
        session_mod._install_ios_guards()  # noqa: SLF001 - a module reload
        self.assertIs(ios_simctl.terminate, guarded)
        record = self.start_pretest()
        ios_simctl.terminate(str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_fresh_launch_of_another_app_is_refused_before_terminate(self) -> None:
        # the fresh-launch sequence every caller uses: terminate, then launch
        record = self.start_pretest()

        def fresh(app_id: str) -> None:
            ios_simctl.terminate(str(FAKE_SIMCTL), UDID, app_id)
            ios_simctl.launch(str(FAKE_SIMCTL), UDID, app_id)

        self.refused(errors.APP_NOT_ALLOWED, fresh, "com.apple.Preferences")
        self.assertEqual(self.calls(), [])  # the other app was never stopped
        fresh(APP)
        self.assertEqual([argv[:2] for argv in self.calls() if argv[1:2] != ["listapps"]],
                         [["simctl", "terminate"], ["simctl", "launch"]])
        self.assertEqual(self.budget(record)["actions_used"], 1)  # one operation

    def fresh(self, app_id: str = APP) -> None:
        ios_simctl.terminate(str(FAKE_SIMCTL), UDID, app_id)
        ios_simctl.launch(str(FAKE_SIMCTL), UDID, app_id)

    def test_fresh_launch_is_one_action_checked_before_terminate(self) -> None:
        # contract 12.1: one check before `simctl terminate`, one action
        record = self.start_pretest(max_actions=1)
        self.fresh()  # takes the last action and still launches
        self.assertEqual([argv[:2] for argv in self.calls() if argv[1:2] != ["listapps"]],
                         [["simctl", "terminate"], ["simctl", "launch"]])
        self.clear_log()
        self.refused(errors.BUDGET_EXHAUSTED, self.fresh)
        self.assertEqual(self.calls(), [])  # refused before the terminate: app not stopped
        budget = self.budget(record)
        self.assertEqual(budget["actions_used"], 1)
        self.assertEqual([(r["code"], r["verb"]) for r in budget["refusals"]],
                         [("budget_exhausted", "stop")])

    def test_fresh_launch_through_the_session_layer_is_one_action(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.assertEqual(session_mod.launch_ios_app(str(FAKE_SIMCTL), UDID, APP, fresh=True),
                         4242)
        self.assertIn(["simctl", "terminate", UDID, APP], self.calls())
        self.assertIn(["simctl", "launch", UDID, APP], self.calls())
        self.assertEqual(self.budget(record), {"actions_used": 1, "refusals": []})

    def test_launch_after_terminate_of_another_app_is_counted(self) -> None:
        record = self.start_pretest(allow_apps=[APP, "com.apple.Preferences"])
        ios_simctl.terminate(str(FAKE_SIMCTL), UDID, "com.apple.Preferences")
        ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.budget(record)["actions_used"], 2)

    def test_launch_after_another_guarded_action_is_counted(self) -> None:
        record = self.start_pretest()
        ios_simctl.terminate(str(FAKE_SIMCTL), UDID, APP)
        guard.before_mutation("key", target=self.target)
        ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.budget(record)["actions_used"], 3)

    def test_launch_long_after_terminate_is_counted(self) -> None:
        record = self.start_pretest()
        clock = [100.0]
        with mock.patch.object(guard, "_monotonic", side_effect=lambda: clock[0]):
            ios_simctl.terminate(str(FAKE_SIMCTL), UDID, APP)
            clock[0] += guard.FOLLOW_UP_SECONDS + 0.5
            ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.budget(record)["actions_used"], 2)

    def test_follow_up_is_taken_once(self) -> None:
        record = self.start_pretest()
        self.fresh()
        ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP)  # a second launch: its own action
        self.assertEqual(self.budget(record)["actions_used"], 2)

    def test_follow_up_is_thread_local(self) -> None:
        record = self.start_pretest()
        ios_simctl.terminate(str(FAKE_SIMCTL), UDID, APP)
        worker = threading.Thread(
            target=lambda: ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP))
        worker.start()
        worker.join(60)
        self.assertEqual(self.budget(record)["actions_used"], 2)

    def test_refused_terminate_leaves_no_follow_up(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.refused(errors.APP_NOT_ALLOWED, ios_simctl.terminate,
                     str(FAKE_SIMCTL), UDID, "com.apple.Preferences")
        ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP)  # counted on its own
        self.refused(errors.BUDGET_EXHAUSTED, ios_simctl.launch,
                     str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_fresh_launch_without_a_pretest_sets_no_follow_up(self) -> None:
        self.fresh()
        self.assertIsNone(getattr(guard._LOCAL, "follow_up", None))  # noqa: SLF001
        self.assertEqual([argv[:2] for argv in self.calls() if argv[1:2] != ["listapps"]],
                         [["simctl", "terminate"], ["simctl", "launch"]])

    def test_exempt_terminate_sets_no_follow_up(self) -> None:
        record = self.start_pretest()
        with guard.exempt():
            ios_simctl.terminate(str(FAKE_SIMCTL), UDID, APP)
        ios_simctl.launch(str(FAKE_SIMCTL), UDID, APP)
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_exempt_simctl_launch_is_not_counted(self) -> None:
        record = self.start_pretest(max_actions=1)
        with guard.exempt():
            ios_simctl.launch(str(FAKE_SIMCTL), UDID, "com.apple.Preferences")
        self.assertIn(["simctl", "launch", UDID, "com.apple.Preferences"], self.calls())
        self.assertEqual(self.budget(record), {"actions_used": 0, "refusals": []})

    def test_ios_open_is_counted_without_a_resolver(self) -> None:
        record = self.start_pretest(max_actions=1)
        device_state.open_url(self.target, "https://example.com")
        self.assertIn(["simctl", "openurl", UDID, "https://example.com"], self.calls())
        self.clear_log()
        self.refused(errors.BUDGET_EXHAUSTED, device_state.open_url, self.target,
                     "myapp://x")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.budget(record)["actions_used"], 1)


# --- read-only verbs that relaunch the app internally ----------------------------------


class ReadOnlyRecoveryTests(GuardBase):
    """`ui tree` force-stops and relaunches a Flutter app to recover its
    accessibility tree; the CLI runs that recovery inside ``guard.exempt()``
    (contract section 9), which this proves is enough."""

    def exhaust(self, record: dict) -> None:
        budget = self.budget(record)
        budget["actions_used"] = int(record["budget"]["max_actions"])
        store.write_json(store.pretest_dir(self.session, record["pretest_id"])
                         / store.BUDGET_FILE, budget)

    def recover(self) -> None:
        with guard.exempt():
            session_mod.force_stop(str(FAKE_ADB), SERIAL, APP)
            session_mod.launch_app(str(FAKE_ADB), SERIAL, APP)

    def test_exempt_recovery_is_not_counted(self) -> None:
        record = self.start_pretest(max_actions=5)
        self.recover()
        self.assertEqual(self.budget(record), {"actions_used": 0, "refusals": []})

    def test_exempt_recovery_with_a_spent_budget_still_relaunches(self) -> None:
        record = self.start_pretest(max_actions=1)
        self.exhaust(record)
        self.recover()
        words = [" ".join(argv) for argv in self.calls()]
        self.assertTrue(any("am force-stop" in w for w in words), words)
        self.assertTrue(any("am start" in w for w in words), words)  # not left stopped
        self.assertEqual(self.budget(record), {"actions_used": 1, "refusals": []})

    def test_the_guard_does_not_inspect_the_call_stack(self) -> None:
        # the guard decides from exempt() alone, whatever name the CLI has
        self.assertFalse(hasattr(guard, "inside_read_only_verb"))
        self.assertFalse(hasattr(guard, "READ_ONLY_HANDLERS"))


# --- --activity validation (contract 12.2) -------------------------------------------


class ActivityTests(GuardBase):
    BAD = ("; reboot", ".Main && rm -rf /", "$(id)", ".Main`id`", ".Main|cat",
           ".Main x", "com.x/.Main'", ".Main\n", "../.Main;", ".Main>out")

    def check_refused(self) -> None:
        for activity in self.BAD:
            for fn in (session_mod.launch_app, session_mod.launch_app_fresh):
                with self.assertRaises(errors.AutonomError) as caught:
                    fn(str(FAKE_ADB), SERIAL, APP, activity=activity)
                payload = caught.exception.as_dict()
                self.assertEqual(payload["error_code"], errors.INVALID_VALUE, activity)
                self.assertTrue(payload["hint"])
                self.assertEqual(payload["activity"], activity)
        self.assertEqual(self.calls(), [])

    def test_invalid_activity_refused_without_a_pretest(self) -> None:
        self.check_refused()

    def test_invalid_activity_refused_before_the_guard(self) -> None:
        record = self.start_pretest()
        self.check_refused()
        self.assertEqual(self.budget(record), {"actions_used": 0, "refusals": []})

    def test_valid_activity_names_pass(self) -> None:
        for activity in (".Main", "com.example.app.ui.Main$Inner", f"{APP}/.Main_2", ""):
            session_mod._check_activity(activity)  # noqa: SLF001
        session_mod._check_activity(None)  # noqa: SLF001
        session_mod.launch_app(str(FAKE_ADB), SERIAL, APP, activity=".Main$Inner")
        self.assertTrue(any("am start" in " ".join(argv) for argv in self.calls()))


# --- follow-up primitive -----------------------------------------------------------------


class FollowUpTests(unittest.TestCase):
    def setUp(self) -> None:
        guard._LOCAL.follow_up = None  # noqa: SLF001

    def test_taken_once_for_the_same_key(self) -> None:
        guard.expect_follow_up(("k", 1))
        self.assertFalse(guard.take_follow_up(("k", 2)))
        self.assertFalse(guard.take_follow_up(("k", 1)))  # cleared by the miss
        guard.expect_follow_up(("k", 1))
        self.assertTrue(guard.take_follow_up(("k", 1)))
        self.assertFalse(guard.take_follow_up(("k", 1)))

    def test_nothing_pending(self) -> None:
        self.assertFalse(guard.take_follow_up(("k",)))

    def test_expires(self) -> None:
        clock = [10.0]
        with mock.patch.object(guard, "_monotonic", side_effect=lambda: clock[0]):
            guard.expect_follow_up(("k",))
            clock[0] += guard.FOLLOW_UP_SECONDS
            self.assertTrue(guard.take_follow_up(("k",)))
            guard.expect_follow_up(("k",))
            clock[0] += guard.FOLLOW_UP_SECONDS + 0.01
            self.assertFalse(guard.take_follow_up(("k",)))

    def test_any_guarded_call_clears_it(self) -> None:
        guard.expect_follow_up(("k",))
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"AUTONOM_HOME": home}):
            self.assertFalse(guard.before_mutation("key"))  # no pre-test: not counted
        self.assertFalse(guard.take_follow_up(("k",)))


# --- CLI: exit code and journal ------------------------------------------------------


class GuardCliTests(unittest.TestCase):
    """tp-006-S01/S02 through the real CLI and the fake adb."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.log = self.cwd / "fake.log"
        dump = self.cwd / "dump.xml"
        dump.write_text(UI_DUMP, encoding="utf-8")
        state = self.cwd / "state.json"
        state.write_text(json.dumps({"devices": [[SERIAL, "device", ""]],
                                     "ui_dump": str(dump)}), encoding="utf-8")
        self.env = {**os.environ, "AUTONOM_FAKE_STATE": str(state),
                    "AUTONOM_FAKE_LOG": str(self.log),
                    "AUTONOM_HOME": str(self.cwd / "home")}
        patch = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(self.cwd / "home")})
        patch.start()
        self.addCleanup(patch.stop)

    def cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI), *argv, "--adb", str(FAKE_ADB)],
                              capture_output=True, text=True, env=self.env, cwd=self.cwd,
                              timeout=120)

    def session(self) -> dict:
        found = list((self.cwd / "home/sessions").glob("s_*/session.json"))
        self.assertEqual(len(found), 1)
        return json.loads(found[0].read_text(encoding="utf-8"))

    def journal(self, session: dict) -> list[dict]:
        path = Path(session["artifacts_dir"]) / "journal.ndjson"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def input_taps(self) -> int:
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        argvs = [json.loads(line)["argv"] for line in lines]
        return sum(1 for argv in argvs if "input" in argv and "dumpsys" not in argv)

    def start(self, **budget) -> dict:
        started = self.cli("session", "start", "--serial", SERIAL, "--app-id", APP)
        self.assertEqual(started.returncode, 0, started.stderr)
        session = self.session()
        target = SimpleNamespace(platform="android", target_id=SERIAL)
        with mock.patch.object(device_state, "crash_list", return_value=[]), \
                mock.patch.object(snapshot_mod, "take",
                                  side_effect=errors.AutonomError("x", "no metrics")):
            store.create(session, target=target, ticket_text="t", ticket_ref=None,
                         title=None, repo=None, base=None, head=None, flows_dir=None,
                         app_id=None, budget=budget or None,
                         context={"schema": "autonom.pretest-context/v1"})
        return session

    def test_budget_refusal_exits_2_and_is_journaled(self) -> None:
        session = self.start(max_actions=2)
        for _ in range(2):
            tap = self.cli("ui", "tap", "--text", "Continue")
            self.assertEqual(tap.returncode, 0, tap.stderr)
        before = self.input_taps()
        third = self.cli("ui", "tap", "--text", "Continue")
        self.assertEqual(third.returncode, 2, third.stdout + third.stderr)
        payload = json.loads(third.stderr)
        self.assertEqual(payload["error_code"], "budget_exhausted")
        self.assertTrue(payload["hint"])
        self.assertEqual(self.input_taps(), before)
        entry = [e for e in self.journal(session) if e.get("verb") == "ui tap"][-1]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error_code"], "budget_exhausted")

    def test_forbidden_selector_and_coordinate_taps(self) -> None:
        session = self.start()
        by_desc = self.cli("ui", "tap", "--desc", "Купить")
        by_point = self.cli("ui", "tap", "--x", "300", "--y", "360")
        for result in (by_desc, by_point):
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stderr)["error_code"], "forbidden_action")
        self.assertEqual(self.input_taps(), 0)
        codes = [e.get("error_code") for e in self.journal(session) if e.get("verb") == "ui tap"]
        self.assertEqual(codes, ["forbidden_action", "forbidden_action"])

    def test_selector_tap_on_a_row_with_a_forbidden_child_label(self) -> None:
        session = self.start()
        result = self.cli("ui", "tap", "--resource-id", "com.example.app:id/row")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error_code"], "forbidden_action")
        self.assertEqual(payload["field"], "text")
        self.assertEqual(self.input_taps(), 0)
        codes = [e.get("error_code") for e in self.journal(session) if e.get("verb") == "ui tap"]
        self.assertEqual(codes, ["forbidden_action"])

    def test_session_launch_of_another_apps_component_is_refused(self) -> None:
        self.start()
        result = self.cli("session", "launch", APP, "--activity",
                          "com.android.vending/.AssetBrowserActivity")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error_code"], "app_not_allowed")
        self.assertEqual(payload["app_id"], "com.android.vending")
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        self.assertFalse(any("com.android.vending" in line for line in lines))

    def test_session_launch_with_a_shell_activity_is_invalid_value(self) -> None:
        self.start()
        self.log.write_text("", encoding="utf-8")
        result = self.cli("session", "launch", APP, "--activity", ".Main;reboot")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stderr)["error_code"], "invalid_value")
        self.assertEqual(self.log.read_text(encoding="utf-8"), "")

    def test_session_force_stop_and_clear_of_another_app_are_refused(self) -> None:
        session = self.start()
        for verb in ("force-stop", "clear"):
            result = self.cli("session", verb, "com.android.settings")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stderr)
            self.assertEqual(payload["error_code"], "app_not_allowed")
            self.assertTrue(payload["hint"])
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        self.assertFalse(any("com.android.settings" in line for line in lines))
        codes = {e.get("verb"): e.get("error_code") for e in self.journal(session)
                 if e.get("verb") in ("session force-stop", "session clear")}
        self.assertEqual(codes, {"session force-stop": "app_not_allowed",
                                 "session clear": "app_not_allowed"})



class GuardIosCliTests(GuardBase):
    """tp-006 on iOS through the real CLI: `session launch` reaches simctl
    through the guarded ``ios_simctl.launch`` and the refusal is journaled."""

    platform = "ios"

    def setUp(self) -> None:
        super().setUp()
        self.update_state(installed=[APP, "com.apple.Preferences"], simctl_devices={
            "devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
                {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted",
                 "isAvailable": True}]}})

    def cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), "--simctl", str(FAKE_SIMCTL), "--udid", UDID, *argv],
            capture_output=True, text=True, env=dict(os.environ), cwd=self.root,
            timeout=120)

    def launches(self) -> list[list[str]]:
        return [argv for argv in self.calls() if argv[:2] == ["simctl", "launch"]]

    def journal(self) -> list[dict]:
        path = Path(self.session["artifacts_dir"]) / "journal.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_session_launch_outside_the_allow_list_exits_2_and_is_journaled(self) -> None:
        record = self.start_pretest()
        result = self.cli("session", "launch", "com.apple.Preferences")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error_code"], "app_not_allowed")
        self.assertEqual(payload["app_id"], "com.apple.Preferences")
        self.assertTrue(payload["hint"])
        self.assertEqual(self.launches(), [])
        self.assertFalse(any("listapps" in argv for argv in self.calls()))
        codes = [e.get("error_code") for e in self.journal()
                 if e.get("verb") == "session launch"]
        self.assertEqual(codes, ["app_not_allowed"])
        self.assertEqual(self.budget(record)["refusals"][0]["code"], "app_not_allowed")

    def test_session_launch_of_the_session_app_is_counted(self) -> None:
        record = self.start_pretest(max_actions=1)
        first = self.cli("session", "launch", APP)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(len(self.launches()), 1)
        second = self.cli("session", "launch", APP)
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertEqual(json.loads(second.stderr)["error_code"], "budget_exhausted")
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_session_launch_fresh_is_one_action(self) -> None:
        record = self.start_pretest(max_actions=1)
        first = self.cli("session", "launch", APP, "--fresh")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(json.loads(first.stdout)["mode"], "fresh")
        self.assertIn(["simctl", "terminate", UDID, APP], self.calls())
        self.assertEqual(len(self.launches()), 1)
        self.clear_log()
        second = self.cli("session", "launch", APP, "--fresh")
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertEqual(json.loads(second.stderr)["error_code"], "budget_exhausted")
        self.assertEqual(self.calls(), [])  # not even the terminate
        self.assertEqual(self.budget(record)["actions_used"], 1)

    def test_session_launch_without_a_pretest_is_unchanged(self) -> None:
        result = self.cli("session", "launch", "com.apple.Preferences")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.launches(), [["simctl", "launch", UDID, "com.apple.Preferences"]])


if __name__ == "__main__":
    unittest.main()
