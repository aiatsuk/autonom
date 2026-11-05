"""TEST-005: Android typing, `ui fill` and key names (AT-01..AT-03, RISK-005,
RISK-011).

`ui type` through the on-device helper puts any Unicode text at the cursor;
the uiautomator/adb way refuses what `adb shell input text` cannot send
(`text_not_typable`) and sends a literal `%s` as `%s`. `ui fill TEXT`
replaces a field's whole text (helper: a focusing tap, `set_text` checked
against the field's bounds, a read-back; adb: tap, delete keys, `input
text`). Every Android key (`ui key`, Flow and `ui do` key steps, Mobile
Canvas bridge keys) is a `KEYCODE_*` name, a bare upper-case name sent as
`KEYCODE_<name>` (owner decision ODA-004), or a key number; anything else is
`invalid_value` before any device call.

Every case runs against `tests/fakes/fake_adb.py` (every adb argv is logged)
and `tests/fakes/fake_android_helper.py` (protocol 1, with a model of the
text fields on screen); the CLI runs in this process. No device, no javac
or d8.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
SERIAL = "emulator-5612"
IOS_UDID = "AAAAAAAA-1111-2222-3333-DDDDDDDDDDDD"
BUILD = "0123456789abcdef"
APP = "com.example.app"
DUMP = ["exec-out", "uiautomator", "dump", "/dev/tty"]

EMAIL = [60, 400, 1020, 520]
PASSWORD = [60, 600, 1020, 720]
SUBMIT = [60, 800, 1020, 920]
MIXED = "Привет café 東京 😀"

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/fakes"))
from autonom_lib import android_helper as helper_mod  # noqa: E402
from autonom_lib import android_helper_build as build_mod  # noqa: E402
from autonom_lib import control as control_mod  # noqa: E402
from autonom_lib import errors  # noqa: E402
from autonom_lib import journal as journal_mod  # noqa: E402
from autonom_lib import session as session_mod  # noqa: E402
from autonom_lib import ui, ui_android  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402
import fake_android_helper  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def _node(index: int, *, cls: str, bounds: list[int], text: str = "", rid: str = "",
          desc: str = "", focused: bool = False, password: bool = False,
          clickable: bool = False, focusable: bool = False, hint: str = "",
          children: str = "") -> str:
    left, top, right, bottom = bounds
    attrs = (f'index="{index}" text="{text}" resource-id="{rid}" class="{cls}" '
             f'package="{APP}" content-desc="{desc}" checkable="false" checked="false" '
             f'clickable="{str(clickable).lower()}" enabled="true" '
             f'focusable="{str(focusable).lower()}" focused="{str(focused).lower()}" '
             f'scrollable="false" long-clickable="false" password="{str(password).lower()}" '
             f'selected="false" bounds="[{left},{top}][{right},{bottom}]" hint="{hint}"')
    if children:
        return f"<node {attrs}>{children}</node>"
    return f"<node {attrs} />"


def form_xml(*, focused: str | None = None, email: str = "abc", password: str = "••••",
             email_bounds: list[int] = EMAIL) -> str:
    """A sign-in form: an email field, a password field, a button."""
    fields = (
        _node(0, cls="android.widget.TextView", bounds=[60, 200, 1020, 300], text="Welcome")
        + _node(1, cls="android.widget.EditText", bounds=email_bounds, text=email,
                rid=f"{APP}:id/email", focused=focused == "email", clickable=True,
                focusable=True, hint="Email")
        + _node(2, cls="android.widget.EditText", bounds=PASSWORD, text=password,
                rid=f"{APP}:id/password", focused=focused == "password", password=True,
                clickable=True, focusable=True, hint="Password")
        + _node(3, cls="android.widget.Button", bounds=SUBMIT, text="Continue",
                rid=f"{APP}:id/submit", clickable=True, focusable=True))
    root = _node(0, cls="android.widget.FrameLayout", bounds=[0, 0, 1080, 2400],
                 children=fields)
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
            f'<hierarchy rotation="0">{root}</hierarchy>')


def android_decode(argument: str) -> str:
    """What `adb shell input text ARG` types: InputShellCommand.sendText turns
    every `%s` into a space (and has no escape for a literal `%s`)."""
    text = list(argument)
    escape = False
    index = 0
    while index < len(text):
        if escape:
            escape = False
            if text[index] == "s":
                text[index] = " "
                del text[index - 1]
                index -= 1
        if text[index] == "%":
            escape = True
        index += 1
    return "".join(text)


class TextCase(EnvSandboxMixin, unittest.TestCase):
    """A private state directory, the fake adb and a fake helper serving the
    sign-in form; AUTONOM_ANDROID_UI_BACKEND unset (auto) unless a case sets
    it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "state"
        self.state = self.root / "adb-state.json"
        self.log = self.root / "adb-log.jsonl"
        self.dump = self.root / "dump.xml"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        (self.root / "user").mkdir()
        self.set_env(AUTONOM_HOME=str(self.home), AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log), PATH=str(self.bin),
                     HOME=str(self.root / "user"), JAVA_HOME=None, ANDROID_HOME=None,
                     ANDROID_SDK_ROOT=None, AUTONOM_ADB=None, AUTONOM_SIMCTL=None,
                     AUTONOM_UI_BACKEND=None, AUTONOM_ANDROID_UI_BACKEND=None,
                     AUTONOM_CONTROL_HOLDER=None, AUTONOM_CONTROL_GENERATION=None)
        self.helper = fake_android_helper.FakeHelper(state_path=self.state,
                                                     xml=form_xml()).start()
        self.addCleanup(self.helper.close)
        self.set_screen(form_xml())
        self.target = Target("android", SERIAL, str(FAKE_ADB), {"serial": SERIAL})
        self.dex = self.root / "classes.dex"
        self.dex.write_bytes(b"dex\n035\0fake")
        self.build = mock.patch.object(helper_mod.build_mod, "ensure",
                                       return_value=build_mod.Build(self.dex, BUILD, None, None))
        self.built = self.build.start()
        self.addCleanup(self.build.stop)
        ui_android.set_preference(None)
        self.addCleanup(ui_android.set_preference, None)
        token = session_mod.select(None)
        self.addCleanup(session_mod._SELECTED.reset, token)  # noqa: SLF001
        self.cli_module: Any = None

    # -- the fake device

    def set_screen(self, xml: str, **extra: Any) -> None:
        """The screen both backends read: the helper's XML and the dump."""
        self.helper.xml = xml
        self.dump.write_text(xml, encoding="utf-8")
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "helper_port": self.helper.port, "ui_dump": str(self.dump),
                 "wm_size": "Physical size: 1080x2400",
                 "device_files": ["autonom-helper.log"]}
        if self.state.exists():
            previous = json.loads(self.state.read_text(encoding="utf-8"))
            for key in ("helper_procs", "helper_running", "forwards"):
                if key in previous:
                    state[key] = previous[key]
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def model_fields(self, *, focused: int | None = None, email: str = "abc") -> None:
        """The fake helper's text fields, as the form shows them."""
        self.helper.fields = [
            {"bounds": list(EMAIL), "text": email},
            {"bounds": list(PASSWORD), "text": "hunter", "password": True},
            {"bounds": list(SUBMIT), "text": "Continue", "editable": False},
        ]
        self.helper.focused = focused

    def no_tools(self) -> None:
        self.built.return_value = build_mod.Build(
            None, None, "javac not found", "Install a JDK (17 or newer) and put javac on PATH.")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row["argv"][2:] if row["argv"][:1] == ["-s"] else row["argv"]
                for row in rows if row.get("tool", "adb") == "adb"]

    def clear_calls(self) -> None:
        self.log.unlink(missing_ok=True)

    def inputs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:2] == ["shell", "input"]]

    def ops(self, op: str | None = None) -> list[dict[str, Any]]:
        return [item for item in self.helper.ops(op) if item["op"] != "hello"]

    # -- the CLI, in this process

    def cli(self, *argv: str, stdin: str = "") -> tuple[int, dict[str, Any]]:
        return self.cli_raw("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv, stdin=stdin)

    def cli_raw(self, *argv: str, stdin: str = "") -> tuple[int, dict[str, Any]]:
        if self.cli_module is None:
            spec = importlib.util.spec_from_file_location("autonom_cli_text_fill", CLI)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            self.addCleanup(control_mod.end_invocation)
            cwd = os.getcwd()
            os.chdir(self.root)
            self.addCleanup(os.chdir, cwd)
            self.cli_module = module
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = self.cli_module.main(list(argv))
        stream = out.getvalue() if out.getvalue().strip() else err.getvalue()
        self.assertNotIn("Traceback", stream)
        return code, json.loads(stream.strip().splitlines()[-1]) if stream.strip() else {}

    def start_session(self) -> Path:
        code, payload = self.cli("session", "start", "--app-id", APP)
        self.assertEqual(code, 0, payload)
        return Path(payload["session"]["artifacts_dir"])

    def journal_blob(self, artifacts: Path) -> str:
        path = artifacts / journal_mod.JOURNAL_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""


# --- AT-01: Unicode typing through the helper ------------------------------------------


class HelperTypingTests(TextCase):
    def test_mixed_scripts_reach_the_field_exactly(self) -> None:  # AT-01-S01
        self.set_screen(form_xml(focused="email", email=""))
        self.model_fields(focused=0, email="")
        code, payload = self.cli("ui", "type", MIXED)
        self.assertEqual(code, 0, payload)
        [typed] = self.ops("type")
        self.assertEqual(typed["text"], MIXED)
        self.assertEqual(typed["settle"], False, "an unobserved type does not settle")
        self.assertEqual(self.helper.fields[0]["text"], MIXED)
        self.assertEqual((payload["backend"], payload["input_backend"]), ("helper", "helper"))
        self.assertEqual(payload["method"], "mixed")
        self.assertEqual(payload["text_after"], MIXED)
        self.assertIs(payload["complete"], True)
        self.assertEqual(payload["typed"], MIXED)
        self.assertEqual(self.inputs(), [], "no adb shell input")
        self.assertNotIn(DUMP, self.calls())

    def test_typing_inserts_after_the_existing_text(self) -> None:
        self.set_screen(form_xml(focused="email"))
        self.model_fields(focused=0, email="abc")
        code, payload = self.cli("ui", "type", "é東")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["text_after"], "abcé東")
        self.assertEqual(payload["method"], "set_text")

    def test_ascii_text_goes_as_keys_through_the_helper(self) -> None:
        self.set_screen(form_xml(focused="email", email=""))
        self.model_fields(focused=0, email="")
        code, payload = self.cli("ui", "type", "a%sb c")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.ops("type")[0]["text"], "a%sb c", "the helper takes text as is")
        self.assertEqual(payload["method"], "keys")
        self.assertEqual(self.helper.fields[0]["text"], "a%sb c")

    def test_an_unreadable_field_is_not_overwritten(self) -> None:  # RISK-005, ODA-002
        """Only a password field's text cannot be read: text that needs
        set-text there is refused, the field untouched (also one whose node
        exposes no text at all, which is not known to be empty)."""
        self.set_screen(form_xml(focused="password"))
        self.model_fields(focused=1)
        code, envelope = self.cli("ui", "type", "пароль")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.TEXT_NOT_TYPABLE)
        self.assertEqual((envelope["helper_error"], envelope["op"]), ("text_not_typable", "type"))
        self.assertIn("ui fill", envelope["hint"])
        self.assertEqual(self.helper.fields[1]["text"], "hunter", "the field is untouched")
        self.assertEqual(len(self.ops("type")), 1)
        self.assertEqual(self.inputs(), [])
        self.helper.fields[1].update(text="", readable=False)  # exposes no text
        code, envelope = self.cli("ui", "type", "пароль")
        self.assertEqual((code, envelope["error_code"]), (2, errors.TEXT_NOT_TYPABLE), envelope)
        self.assertEqual(self.helper.fields[1]["text"], "")

    def test_a_non_password_field_that_shows_no_text_is_typed_into_as_empty(self) -> None:
        """ODA-002: a field that is not a password field and reports no text
        counts as empty (Android reports an empty field without a hint the
        same way), as Input.java reads it: the text is typed, not refused."""
        self.set_screen(form_xml(focused="email", email=""))
        self.model_fields(focused=0, email="")
        self.helper.fields[0]["readable"] = False
        code, payload = self.cli("ui", "type", "Привет")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.helper.fields[0]["text"], "Привет")
        self.assertEqual((payload["method"], payload["text_after"]), ("set_text", "Привет"))
        self.assertIs(payload["complete"], True)
        self.assertEqual(len(self.ops("type")), 1)
        self.assertEqual(self.inputs(), [])

    def test_nothing_focused_is_no_focused_field(self) -> None:
        self.model_fields(focused=None)
        code, envelope = self.cli("ui", "type", "abc")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.NO_FOCUSED_FIELD)
        self.assertEqual(envelope["helper_error"], "no_focus")
        self.assertEqual(self.inputs(), [], "no fall-back to adb input")

    def test_a_focused_view_that_is_not_a_field_is_no_focused_field(self) -> None:
        self.model_fields(focused=2)
        with self.assertRaises(errors.AutonomError) as caught:
            ui.type_text(self.target, "abc")
        self.assertEqual(caught.exception.code, errors.NO_FOCUSED_FIELD)
        self.assertEqual(caught.exception.extra["helper_error"], "not_editable")

    def test_a_part_that_did_not_land_is_reported_and_not_sent_again(self) -> None:
        self.set_screen(form_xml(focused="email", email=""))
        self.model_fields(focused=0, email="")
        self.helper.keys_drop = 2
        code, payload = self.cli("ui", "type", "hello")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["complete"], False)
        self.assertEqual(payload["text_after"], "hel")
        self.assertEqual([warning["code"] for warning in payload["warnings"]],
                         ["text_incomplete"])
        self.assertEqual(len(self.ops("type")), 1, "nothing is re-sent")
        self.assertEqual(self.inputs(), [])

    def test_a_type_without_a_reply_is_uncertain_and_never_sent_again(self) -> None:
        self.model_fields(focused=0)
        self.helper.drop_after = {"type": 1}
        code, envelope = self.cli("ui", "type", "abc")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.UI_ACTION_UNCERTAIN)
        self.assertEqual(len(self.helper.wait_ops("type")), 1)
        self.assertEqual(self.inputs(), [], "no adb input after an unanswered type")

    def test_an_observed_type_takes_one_round_trip(self) -> None:
        self.set_screen(form_xml(focused="email", email=""))
        self.model_fields(focused=0, email="")
        code, payload = self.cli("ui", "type", "abc", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["op"] for item in self.ops()], ["read", "type"],
                         "type, settle and the read after it in one request")
        typed = self.ops("type")[0]
        self.assertIs(typed["read_after"], True)
        self.assertIsInstance(typed["settle"], dict)
        self.assertIs(payload["settled"], True)
        self.assertIn("screen", payload)

    def test_a_sensitive_type_masks_what_the_field_shows(self) -> None:
        artifacts = self.start_session()
        self.set_screen(form_xml(focused="email", email=""))
        self.model_fields(focused=0, email="")
        code, payload = self.cli("ui", "type", "s3cr3t-ü", "--sensitive")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.helper.fields[0]["text"], "s3cr3t-ü")
        self.assertEqual(payload["typed"], "<8 chars>")
        self.assertEqual(payload["text_after"], "<8 chars>")
        self.assertNotIn("s3cr3t", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("s3cr3t", self.journal_blob(artifacts))

    def test_empty_text_sends_nothing_through_the_helper(self) -> None:
        self.model_fields(focused=0)
        self.assertEqual(ui.type_text(self.target, ""), "helper")
        self.assertEqual(self.ops("type"), [])
        self.assertEqual(self.inputs(), [])

    def test_helper_mode_types_through_the_helper(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        self.model_fields(focused=0)
        self.assertEqual(ui.type_text(self.target, "é"), "helper")
        self.no_tools()
        ui_android.set_preference(None)
        with self.assertRaises(errors.AutonomError) as caught:
            ui.type_text(self.target, "é")
        self.assertEqual(caught.exception.code, errors.ANDROID_HELPER_UNAVAILABLE)
        self.assertEqual(self.inputs(), [], "helper mode never types with adb")


# --- AT-01: the uiautomator/adb way ------------------------------------------------------


class AdbTypingTests(TextCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")

    def typed(self) -> list[str]:
        """The `input text` arguments as the device shell sees them."""
        words = []
        for call in self.inputs():
            if call[2:3] == ["text"]:
                [word] = shlex.split(" ".join(call[3:]))
                words.append(word)
        return words

    def test_non_ascii_is_refused_before_any_input(self) -> None:  # AT-01-S02
        code, envelope = self.cli("ui", "type", "café")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.TEXT_NOT_TYPABLE)
        self.assertIn("helper", envelope["hint"])
        self.assertEqual((envelope["characters"], envelope["first_position"]), (1, 3))
        self.assertNotIn("é", json.dumps(envelope, ensure_ascii=False),
                         "the refused text is never echoed")
        self.assertEqual(self.inputs(), [], "no input text ran")
        self.assertEqual(self.ops(), [], "no helper contact under the opt-out")

    def test_control_characters_are_refused_too(self) -> None:
        for text in ("line\nbreak", "tab\there", "\x7f"):
            with self.subTest(text=repr(text)):
                with self.assertRaises(errors.AutonomError) as caught:
                    ui.type_text(self.target, text)
                self.assertEqual(caught.exception.code, errors.TEXT_NOT_TYPABLE)
        self.assertEqual(self.inputs(), [])

    def test_ascii_typing_is_one_call_as_before(self) -> None:
        code, payload = self.cli("ui", "type", "hello world")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.inputs(), [["shell", "input", "text", "'hello%sworld'"]])
        # the explicit opt-out keeps today's reply (ODA-001)
        self.assertEqual(sorted(key for key in payload if key not in ("target_id", "platform",
                                                                       "serial", "focused",
                                                                       "focus", "warnings")),
                         ["backend", "ok", "typed"])

    def test_a_literal_percent_s_arrives_as_percent_s(self) -> None:
        code, payload = self.cli("ui", "type", "a%sb c")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.typed(), ["a%", "sb%sc"])
        self.assertEqual("".join(android_decode(word) for word in self.typed()), "a%sb c")

    def test_every_split_decodes_to_the_text(self) -> None:
        cases = ["%s", "%%s", "%s%s", "a %s", "% s", "%", "s", "100%", "%ss", " %s ", "%%%s",
                 "x%sy%sz", "%s ", "a%%b", "%% s%s", "plain text", "", "'%s'", "$(%s)"]
        for text in cases:
            with self.subTest(text=text):
                parts = ui_android.adb_text_parts(text)
                self.assertTrue(all(parts) or text == "")
                self.assertEqual("".join(android_decode(part) for part in parts), text)
                if "%s" not in text:
                    self.assertEqual(len(parts), 1, "no literal %s: one call as before")

    def test_auto_fall_back_refuses_non_ascii_with_the_reason(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND=None)
        self.no_tools()
        with self.assertRaises(errors.AutonomError) as caught:
            ui.type_text(self.target, "東京")
        self.assertEqual(caught.exception.code, errors.TEXT_NOT_TYPABLE)
        self.assertEqual(caught.exception.extra["fallback_reason"], "javac not found")
        self.assertEqual(self.inputs(), [])
        self.assertEqual(ui.type_text(self.target, "ok"), "adb")
        self.assertEqual(self.inputs(), [["shell", "input", "text", "'ok'"]])

    def test_flow_and_canvas_typing_inherit_the_refusal(self) -> None:
        # Flow inputText/pasteText and the Canvas bridge type with ui.type_text
        from autonom_lib.flow import executor as flow_executor  # noqa: PLC0415
        self.assertIn("ui_mod.type_text(target, value)",
                      Path(flow_executor.__file__).read_text(encoding="utf-8"))
        bridge = (ROOT / "scripts/autonom_canvas_bridge.py").read_text(encoding="utf-8")
        self.assertIn("ui.type_text(target, text)", bridge)
        with self.assertRaises(errors.AutonomError) as caught:
            ui.type_text(self.target, "ñ")
        self.assertEqual(caught.exception.code, errors.TEXT_NOT_TYPABLE)


# --- AT-02: ui fill ----------------------------------------------------------------------


class HelperFillTests(TextCase):
    def test_fill_replaces_the_text(self) -> None:  # AT-02-S01
        self.model_fields(focused=None, email="abc")
        code, payload = self.cli("ui", "fill", "Привет", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.helper.fields[0]["text"], "Привет")
        self.assertIs(payload["text_matches"], True)
        self.assertEqual((payload["backend"], payload["method"]), ("helper", "set_text"))
        self.assertEqual(payload["text_after"], "Привет")
        self.assertEqual(payload["filled"], "Привет")
        self.assertIs(payload["tapped"], True)
        self.assertEqual((payload["x"], payload["y"]), (540, 460))
        self.assertEqual(payload["field"]["resource_id"], f"{APP}:id/email")
        ops = self.ops()
        self.assertEqual([item["op"] for item in ops], ["read", "tap", "set_text"])
        tap, set_text = ops[1], ops[2]
        self.assertEqual((tap["x"], tap["y"]), (540, 460))
        self.assertIs(tap["read_after"], True, "the field is found again after the tap")
        self.assertIsInstance(tap["settle"], dict, "the focus settles before the text is set")
        self.assertEqual(set_text["expect_bounds"], EMAIL)
        self.assertEqual(set_text["settle"], False)
        self.assertEqual(self.inputs(), [])

    def test_a_field_the_keyboard_moved_is_found_again(self) -> None:
        moved = [60, 300, 1020, 420]
        self.model_fields(focused=None)
        self.helper.bounds_after_tap = {0: moved}
        self.helper.xml_after = form_xml(focused="email", email_bounds=moved)
        code, payload = self.cli("ui", "fill", "new", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.ops("set_text")[0]["expect_bounds"], moved)
        self.assertEqual(self.helper.fields[0]["text"], "new")

    def test_another_focused_field_is_never_filled(self) -> None:  # RISK-011
        self.model_fields(focused=1)
        self.helper.tap_focuses = False  # the tap missed: the password field keeps focus
        code, envelope = self.cli("ui", "fill", "x@example.com", "--resource-id",
                                  f"{APP}:id/email")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.FILL_TARGET_MISMATCH)
        self.assertEqual(envelope["helper_error"], "fill_target_mismatch")
        self.assertEqual(self.helper.fields[1]["text"], "hunter")
        self.assertEqual(self.helper.fields[0]["text"], "abc")
        self.assertEqual([item["op"] for item in self.ops()], ["read", "tap", "set_text"])
        self.assertEqual(self.inputs(), [], "nothing typed by adb either")

    def test_a_target_that_is_not_editable_is_refused_before_any_input(self) -> None:
        self.model_fields(focused=None)
        code, envelope = self.cli("ui", "fill", "x", "--text", "Continue")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
        self.assertEqual(envelope["reason"], "not_editable")
        self.assertEqual([item["op"] for item in self.ops()], ["read"])
        self.assertEqual(self.inputs(), [])

    def test_the_focused_field_without_a_selector(self) -> None:
        self.set_screen(form_xml(focused="email"))
        self.model_fields(focused=0)
        code, payload = self.cli("ui", "fill", "東京")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["tapped"], False)
        self.assertNotIn("x", payload)
        self.assertEqual([item["op"] for item in self.ops()], ["read", "set_text"])
        self.assertEqual(self.ops("set_text")[0]["expect_bounds"], EMAIL)
        self.assertEqual(self.helper.fields[0]["text"], "東京")

    def test_nothing_focused_and_no_selector(self) -> None:
        self.model_fields(focused=None)
        code, envelope = self.cli("ui", "fill", "x")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.NO_FOCUSED_FIELD)
        self.assertEqual([item["op"] for item in self.ops()], ["read"])

    def test_a_password_field_reports_no_text_matches(self) -> None:
        self.model_fields(focused=None)
        code, payload = self.cli("ui", "fill", "n3w-pässword", "--resource-id",
                                 f"{APP}:id/password", "--sensitive")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.helper.fields[1]["text"], "n3w-pässword")
        self.assertNotIn("text_matches", payload)
        self.assertIsNone(payload["text_after"])
        self.assertEqual(payload["filled"], "<12 chars>")
        self.assertIs(payload["sensitive"], True)

    def test_a_field_that_shows_other_text_is_reported(self) -> None:
        self.model_fields(focused=None)
        self.helper.replies["set_text"] = {"text_after": "Прив", "complete": False}
        code, payload = self.cli("ui", "fill", "Привет", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertIs(payload["text_matches"], False)
        self.assertIs(payload["complete"], False)
        self.assertIn("text_mismatch", [warning["code"] for warning in payload["warnings"]])
        self.assertEqual(len(self.ops("set_text")), 1, "nothing is re-sent")

    def test_an_unanswered_set_text_is_uncertain(self) -> None:
        self.model_fields(focused=None)
        self.helper.drop_after = {"set_text": 1}
        code, envelope = self.cli("ui", "fill", "x", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.UI_ACTION_UNCERTAIN)
        self.assertEqual(len(self.helper.wait_ops("set_text")), 1)
        self.assertEqual(len(self.ops("tap")), 1)
        self.assertEqual(self.inputs(), [])

    def helper_gone_before_set_text(self) -> Any:
        """The helper becomes unusable after the focusing tap (auto falls
        back); nothing of the set_text was sent."""
        real = helper_mod.request

        def request(target: Any, op: str, **kwargs: Any) -> dict[str, Any]:
            if op == "set_text":
                raise helper_mod.HelperUnavailable("the helper stopped answering")
            return real(target, op, **kwargs)

        return mock.patch.object(helper_mod, "request", side_effect=request)

    def test_after_the_tap_only_the_text_part_falls_back_to_adb(self) -> None:
        self.model_fields(focused=None)
        with self.helper_gone_before_set_text():
            code, payload = self.cli("ui", "fill", "ok", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertEqual(len(self.ops("tap")), 1)
        self.assertEqual(self.ops("set_text"), [])
        self.assertEqual(self.inputs(), [
            ["shell", "input", "keyevent", "KEYCODE_MOVE_END", "KEYCODE_DEL", "KEYCODE_DEL",
             "KEYCODE_DEL"],
            ["shell", "input", "text", "'ok'"],
        ], "the tap the helper performed is never sent again")
        self.assertEqual((payload["backend"], payload["deleted"]), ("adb", 3))
        self.assertIn("stopped answering", payload["fallback_reason"])

    def test_after_the_tap_non_ascii_is_refused_with_nothing_typed(self) -> None:
        self.model_fields(focused=None)
        with self.helper_gone_before_set_text():
            code, envelope = self.cli("ui", "fill", "Привет", "--resource-id",
                                      f"{APP}:id/email")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.TEXT_NOT_TYPABLE)
        self.assertEqual(len(self.ops("tap")), 1)
        self.assertEqual(self.inputs(), [])
        self.assertEqual(self.helper.fields[0]["text"], "abc")

    def test_an_observed_fill_reads_the_screen_in_the_set_text_request(self) -> None:
        self.model_fields(focused=None)
        code, payload = self.cli("ui", "fill", "abc", "--resource-id", f"{APP}:id/email",
                                 "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["op"] for item in self.ops()], ["read", "tap", "set_text"])
        set_text = self.ops("set_text")[0]
        self.assertIs(set_text["read_after"], True)
        self.assertIn("screen", payload)
        self.assertIs(payload["settled"], True)

    def test_a_sensitive_fill_is_masked_everywhere(self) -> None:
        artifacts = self.start_session()
        self.model_fields(focused=None)
        code, payload = self.cli("ui", "fill", "Tr0ub4dor-é", "--resource-id",
                                 f"{APP}:id/email", "--sensitive", "--observe")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.helper.fields[0]["text"], "Tr0ub4dor-é")
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Tr0ub4dor", blob)
        self.assertEqual(payload["text_after"], "<11 chars>")
        self.assertNotIn("Tr0ub4dor", self.journal_blob(artifacts))
        journal = [json.loads(line) for line in
                   self.journal_blob(artifacts).splitlines() if line.strip()]
        entry = [item for item in journal if item.get("verb") == "ui fill"][-1]
        self.assertIn("<11 chars>", entry["argv"])
        self.assertIn(f"{APP}:id/email", entry["argv"], "the selector value is kept")
        detail = json.loads((artifacts / entry["result"]["detail"]).read_text("utf-8"))
        self.assertEqual((detail["kind"], detail["text"], detail["text_len"]),
                         ("fill", None, 11))

    def test_a_plain_fill_is_masked_in_the_journal_like_ui_type(self) -> None:
        artifacts = self.start_session()
        self.model_fields(focused=None)
        code, payload = self.cli("ui", "fill", "plainvalue", "--resource-id",
                                 f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        blob = self.journal_blob(artifacts)
        self.assertIn('"verb": "ui fill"', blob)
        self.assertNotIn("plainvalue", blob)
        self.assertIn("<10 chars>", blob)

    def test_ios_answers_unsupported_on_platform(self) -> None:
        code, envelope = self.cli_raw("--simctl", str(FAKE_SIMCTL), "--udid", IOS_UDID,
                                      "ui", "fill", "x", "--text", "Email")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.UNSUPPORTED_ON_PLATFORM)
        self.assertIn("ui type", envelope["hint"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ops(), [])


class AdbFillTests(TextCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")

    def test_fill_taps_deletes_and_types(self) -> None:
        code, payload = self.cli("ui", "fill", "new@example.com", "--resource-id",
                                 f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.inputs(), [
            ["shell", "input", "tap", "540", "460"],
            ["shell", "input", "keyevent", "KEYCODE_MOVE_END", "KEYCODE_DEL", "KEYCODE_DEL",
             "KEYCODE_DEL"],
            ["shell", "input", "text", "'new@example.com'"],
        ])
        self.assertEqual((payload["backend"], payload["method"], payload["deleted"]),
                         ("adb", "keys", 3))
        self.assertNotIn("text_matches", payload)
        self.assertEqual(self.ops(), [], "no helper contact under the opt-out")
        for key in ("ui_backend", "input_backend", "fallback_reason"):
            self.assertNotIn(key, payload)

    def test_an_empty_field_still_gets_one_delete(self) -> None:
        self.set_screen(form_xml(email=""))
        code, payload = self.cli("ui", "fill", "x", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.inputs()[1], ["shell", "input", "keyevent", "KEYCODE_MOVE_END",
                                            "KEYCODE_DEL"])

    def test_a_long_field_is_cleared_in_several_calls(self) -> None:
        self.set_screen(form_xml(email="y" * 150))
        code, payload = self.cli("ui", "fill", "x", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        keys = [call for call in self.inputs() if call[2:3] == ["keyevent"]]
        self.assertEqual([len(call) - 3 for call in keys], [100, 51])
        self.assertEqual(sum(call.count("KEYCODE_DEL") for call in keys), 150)

    def test_non_ascii_is_refused_before_the_tap(self) -> None:
        code, envelope = self.cli("ui", "fill", "Привет", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.TEXT_NOT_TYPABLE)
        self.assertEqual(self.inputs(), [], "no tap, no delete, no text")

    def test_the_focused_field_is_not_tapped(self) -> None:
        self.set_screen(form_xml(focused="email"))
        code, payload = self.cli("ui", "fill", "a b")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.inputs(), [
            ["shell", "input", "keyevent", "KEYCODE_MOVE_END", "KEYCODE_DEL", "KEYCODE_DEL",
             "KEYCODE_DEL"],
            ["shell", "input", "text", "'a%sb'"],
        ])

    def test_an_emptied_field_types_nothing(self) -> None:
        code, payload = self.cli("ui", "fill", "", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertFalse(any(call[2:3] == ["text"] for call in self.inputs()))

    def test_a_target_that_is_not_editable_is_refused_before_any_input(self) -> None:
        code, envelope = self.cli("ui", "fill", "x", "--resource-id", f"{APP}:id/submit")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
        self.assertEqual(self.inputs(), [])

    def test_auto_falls_back_to_the_adb_fill(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND=None)
        self.no_tools()
        code, payload = self.cli("ui", "fill", "ok", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["backend"], "adb")
        self.assertEqual(payload["fallback_reason"], "javac not found")
        self.assertEqual(len(self.inputs()), 3)
        code, envelope = self.cli("ui", "fill", "ö", "--resource-id", f"{APP}:id/email")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.TEXT_NOT_TYPABLE)
        self.assertEqual(envelope["fallback_reason"], "javac not found")
        self.assertEqual(len(self.inputs()), 3, "nothing more was sent")


# --- AT-03, widened by ODA-004: key names -----------------------------------------------

# Refused on Android with invalid_value before any device call, by every key
# entry point (`ui key`, Flow and `ui do` key steps, Mobile Canvas bridge keys).
NOT_KEYS = ("back", "Back", "BACK; reboot", "KEYCODE_HOME; reboot", "", "KEYCODE_",
            "KEYCODE_back", "keycode_back", "_BACK", "BACK-1", "4a", "-4", " 4", "BACK\n",
            "KEYCODE_BACK\n", "KEYCODE_BACK KEYCODE_HOME", "$(reboot)", "`id`", "4|sh", "٤",
            "12345", "00004")
# A key number has at most 4 digits (fix round 5): at 808a7b7 one of 4001
# digits reached `adb shell input keyevent`, and one of 5000 digits made
# int() raise (a malformed invalid_value; a Flow run aborted with no manifest).
LONG_NUMBERS = ("4" * 4001, "4" * 5000)


def kit_key_names() -> list[str]:
    """The key of every `ui key` verb `pretest kit` lists (pretest/kit.py)."""
    from autonom_lib.pretest import kit  # noqa: PLC0415 - read only here

    return [verb.split()[2] for case in kit.KIT for verb in case["verbs"]
            if verb.startswith("ui key ")]


class KeyNameTests(TextCase):
    """Under the explicit uiautomator opt-out: what reaches `adb shell input
    keyevent`."""

    def setUp(self) -> None:
        super().setUp()
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")

    def keyevents(self) -> list[list[str]]:
        return [call for call in self.inputs() if call[2:3] == ["keyevent"]]

    def test_shell_text_is_refused_before_any_adb_call(self) -> None:  # AT-03-S01
        code, envelope = self.cli("ui", "key", "KEYCODE_HOME; reboot")
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
        self.assertEqual(envelope["value"], "KEYCODE_HOME; reboot")
        self.assertEqual(self.calls(), [], "no adb call ran")

    def test_bare_back_and_home_reach_adb_as_their_keycodes(self) -> None:  # ODA-004
        # at ccf98bb `adb shell input keyevent BACK` resolved the bare name;
        # tree 22b8e11 refused it with invalid_value
        for key in ("BACK", "HOME"):
            with self.subTest(key=key):
                self.clear_calls()
                code, payload = self.cli("ui", "key", key)
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload["keycode"], key, "the reply names the key as given")
                self.assertEqual(self.inputs(),
                                 [["shell", "input", "keyevent", f"KEYCODE_{key}"]])

    def test_names_and_numbers_pass_and_anything_else_is_refused(self) -> None:
        for key in NOT_KEYS:
            with self.subTest(key=key):
                self.clear_calls()
                code, envelope = self.cli("ui", "key", "--", key)
                self.assertEqual(code, 2, envelope)
                self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
                self.assertEqual((envelope["flag"], envelope["value"]), ("keycode", key))
                self.assertEqual(self.calls(), [], "no adb call ran")
        for key, sent in (("KEYCODE_BACK", "KEYCODE_BACK"), ("KEYCODE_F12", "KEYCODE_F12"),
                          ("KEYCODE_MEDIA_PLAY_PAUSE", "KEYCODE_MEDIA_PLAY_PAUSE"),
                          ("4", "4"), ("283", "283"), ("ENTER", "KEYCODE_ENTER"),
                          ("F12", "KEYCODE_F12"), ("A", "KEYCODE_A"),
                          ("MEDIA_PLAY_PAUSE", "KEYCODE_MEDIA_PLAY_PAUSE")):
            with self.subTest(key=key):
                self.clear_calls()
                code, payload = self.cli("ui", "key", key)
                self.assertEqual(code, 0, payload)
                self.assertEqual(self.inputs(), [["shell", "input", "keyevent", sent]])

    def test_every_key_the_pretest_kit_lists_passes(self) -> None:
        keys = kit_key_names()
        self.assertIn("HOME", keys)  # background-resume
        self.assertIn("BACK", keys)  # fast-back
        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(ui_android.android_key(key), f"KEYCODE_{key}")
                self.clear_calls()
                code, payload = self.cli("ui", "key", key)
                self.assertEqual(code, 0, payload)
                self.assertEqual(self.inputs(),
                                 [["shell", "input", "keyevent", f"KEYCODE_{key}"]])

    def test_flow_key_steps_follow_the_same_rule(self) -> None:
        # Flow pressKey, back and eraseText send every key through ui.press_key
        self.assertEqual(ui.press_key(self.target, "ENTER"), "adb")
        self.assertEqual(ui.press_key(self.target, "KEYCODE_DEL"), "adb")
        self.assertEqual(self.keyevents(), [["shell", "input", "keyevent", "KEYCODE_ENTER"],
                                            ["shell", "input", "keyevent", "KEYCODE_DEL"]])
        self.clear_calls()
        messages = set()
        for key in NOT_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(errors.AutonomError) as caught:
                    ui.press_key(self.target, key)
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
                messages.add(caught.exception.message)
        # one message whatever the word: a Flow key may come from a secret
        # variable, and a step keeps only the code and the message
        self.assertEqual(len(messages), 1, messages)
        self.assertEqual(self.calls(), [], "no adb call ran")

    def test_ui_do_key_steps_follow_the_same_rule(self) -> None:
        self.start_session()
        self.clear_calls()
        code, payload = self.cli("ui", "do", stdin='[{"pressKey":"BACK"}]')
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.keyevents(), [["shell", "input", "keyevent", "KEYCODE_BACK"]])
        self.clear_calls()
        code, envelope = self.cli("ui", "do", stdin='[{"pressKey":"BACK; reboot"}]')
        # invalid_value fails the step as infrastructure: `ui do` exits 2
        self.assertEqual(code, 2, envelope)
        self.assertEqual((envelope["error_code"], envelope["command"], envelope["step"]),
                         (errors.INVALID_VALUE, "pressKey", 1))
        self.assertEqual(self.keyevents(), [], "no key reached the device shell")

    def test_canvas_bridge_keys_follow_the_same_rule(self) -> None:
        import autonom_canvas_bridge as bridge  # noqa: PLC0415 - Canvas only

        answer = bridge.dispatch(self.target, {"id": 1, "op": "key", "origin": "agent",
                                               "payload": {"key": "BACK"}})
        self.assertIs(answer["ok"], True, answer)
        self.assertEqual(self.keyevents(), [["shell", "input", "keyevent", "KEYCODE_BACK"]])
        self.clear_calls()
        for key in ("BACK; reboot", "back", ""):
            with self.subTest(key=key):
                with self.assertRaises(errors.AutonomError) as caught:
                    bridge.dispatch(self.target, {"id": 2, "op": "key", "origin": "agent",
                                                  "payload": {"key": key}})
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(self.keyevents(), [], "no key reached the device shell")


class HelperKeyNameTests(TextCase):
    """In auto mode, with the helper serving the target."""

    def test_bare_back_and_home_reach_the_helper_as_their_keycodes(self) -> None:  # ODA-004
        for key, number in (("BACK", 4), ("HOME", 3)):
            with self.subTest(key=key):
                code, payload = self.cli("ui", "key", key)
                self.assertEqual(code, 0, payload)
                self.assertEqual((payload["backend"], payload["input_backend"]),
                                 ("helper", "helper"))
                self.assertEqual(payload["keycode"], key)
                self.assertEqual(self.ops("key")[-1]["keycode"], number)
        self.assertEqual(self.inputs(), [], "no adb input")

    def test_a_bare_name_the_helper_cannot_number_reaches_adb_with_the_prefix(self) -> None:
        code, payload = self.cli("ui", "key", "BUTTON_A")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["backend"], payload["input_backend"]), ("adb", "adb"))
        # the reason names no key word (a Flow key may come from a secret)
        self.assertIn("the key is not a key name Autonom knows", payload["fallback_reason"])
        self.assertNotIn("BUTTON_A", payload["fallback_reason"])
        self.assertEqual(self.inputs(), [["shell", "input", "keyevent", "KEYCODE_BUTTON_A"]])
        self.assertEqual(self.ops("key"), [])

    def test_refused_keys_reach_neither_the_helper_nor_adb(self) -> None:
        for key in NOT_KEYS:
            with self.subTest(key=key):
                self.clear_calls()
                code, envelope = self.cli("ui", "key", "--", key)
                self.assertEqual(code, 2, envelope)
                self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
                self.assertEqual(self.calls(), [], "no adb call ran")
                self.assertEqual(self.helper.ops(), [], "not even a hello")


class LongKeyNumberTests(TextCase):
    """Item 4 of fix round 5: a decimal key has 1 to 4 digits; a longer one
    gets the normal invalid_value payload, before any device call, from
    every key entry point."""

    def test_ui_key_refuses_a_long_number_with_the_normal_payload(self) -> None:
        for mode in ("uiautomator", None):
            for key in LONG_NUMBERS:
                with self.subTest(mode=mode, digits=len(key)):
                    self.set_env(AUTONOM_ANDROID_UI_BACKEND=mode)
                    self.clear_calls()
                    code, envelope = self.cli("ui", "key", key)
                    self.assertEqual(code, 2, str(envelope)[:300])
                    self.assertEqual(envelope["error_code"], errors.INVALID_VALUE)
                    self.assertEqual((envelope["flag"], envelope["value"]), ("keycode", key))
                    self.assertIn("at most 4 digits", envelope["error"])
                    self.assertIn("at most 4 digits", envelope["hint"])
                    self.assertEqual(self.calls(), [], "no adb call ran")
                    self.assertEqual(self.helper.ops(), [], "not even a hello")

    def test_four_digits_still_pass(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="uiautomator")
        for key in ("4", "0004", "9999"):
            with self.subTest(key=key):
                self.clear_calls()
                code, payload = self.cli("ui", "key", key)
                self.assertEqual(code, 0, payload)
                self.assertEqual(self.inputs(), [["shell", "input", "keyevent", key]])

    def test_ui_do_refuses_a_long_number_as_a_failed_step(self) -> None:
        self.start_session()
        for key in LONG_NUMBERS:
            with self.subTest(digits=len(key)):
                self.clear_calls()
                code, envelope = self.cli("ui", "do", stdin=json.dumps([{"pressKey": key}]))
                self.assertEqual(code, 2, str(envelope)[:300])
                self.assertEqual((envelope["error_code"], envelope["command"], envelope["step"]),
                                 (errors.INVALID_VALUE, "pressKey", 1))
                self.assertFalse(any(call[2:3] == ["keyevent"] for call in self.inputs()))

    def test_a_flow_run_with_a_long_number_fails_its_step_and_writes_a_manifest(self) -> None:
        self.start_session()
        for key in LONG_NUMBERS:
            with self.subTest(digits=len(key)):
                flow = self.root / f"long-{len(key)}.yaml"
                flow.write_text(f"schema: autonom.dev/flow/v1\nappId: {APP}\nname: long key\n"
                                f"---\n- pressKey: \"{key}\"\n", encoding="utf-8")
                self.clear_calls()
                code, envelope = self.cli("flow", "run", str(flow))
                # a failed step of class infrastructure: exit 2, an envelope
                self.assertEqual(code, 2, str(envelope)[:300])
                self.assertEqual((envelope["error_code"], envelope["command"],
                                  envelope["failure_class"]),
                                 (errors.INVALID_VALUE, "pressKey", "infrastructure"))
                self.assertTrue((self.run_dir(envelope["run_id"]) / "manifest.json").is_file(),
                                "the run wrote its manifest")
                self.assertFalse(any(call[2:3] == ["keyevent"] for call in self.inputs()))

    def run_dir(self, run_id: str) -> Path:
        """`<state>/sessions/<session>/flows/<run_id>`."""
        (found,) = list(self.home.glob(f"sessions/*/flows/{run_id}"))
        return found


class SecretKeyWordTests(TextCase):
    """Item 3 of fix round 5: a Flow key word taken from a secret
    (`- pressKey: ${PIN}` with `--secret PIN`) is in no file of the run's
    state directory, in auto mode (it goes through adb, with a
    fallback_reason) and in helper mode (it is refused with
    invalid_value). At 808a7b7 the word was in the message and the
    fallback_reason, so in the run's events, journal and manifest."""

    SECRET = "ZQXSECRET9"

    def run_flow(self) -> tuple[int, dict[str, Any]]:
        self.set_env(PIN=self.SECRET)
        self.start_session()
        flow = self.root / "secret-key.yaml"
        flow.write_text(f"schema: autonom.dev/flow/v1\nappId: {APP}\nname: secret key\n"
                        "---\n- pressKey: ${PIN}\n", encoding="utf-8")
        self.clear_calls()
        return self.cli("flow", "run", str(flow), "--secret", "PIN")

    def files_holding_the_secret(self) -> list[str]:
        """Every file under the state directory (AUTONOM_HOME) and the
        working directory that holds the word; the fake adb's own log of
        the device's argv is not the run's."""
        found = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path != self.log and self.SECRET.encode() in path.read_bytes():
                found.append(str(path.relative_to(self.root)))
        return found

    def assert_run_files(self, run_id: str) -> None:
        """The run's events and manifest and the session journal exist (so
        the scan below covers them)."""
        (run_dir,) = list(self.home.glob(f"sessions/*/flows/{run_id}"))
        for name in ("events.ndjson", "manifest.json"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertTrue((run_dir.parents[1] / journal_mod.JOURNAL_FILE).is_file())

    def test_auto_sends_it_by_adb_and_stores_no_word(self) -> None:
        code, payload = self.run_flow()
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.inputs(), [["shell", "input", "keyevent",
                                          f"KEYCODE_{self.SECRET}"]])
        events = Path(payload["events"]).read_text(encoding="utf-8")
        self.assertIn("the key is not a key name Autonom knows", events)
        self.assert_run_files(payload["run_id"])
        self.assertEqual(self.files_holding_the_secret(), [])
        self.assertNotIn(self.SECRET, json.dumps(payload))

    def test_helper_mode_refuses_it_and_stores_no_word(self) -> None:
        self.set_env(AUTONOM_ANDROID_UI_BACKEND="helper")
        code, envelope = self.run_flow()
        self.assertEqual(code, 2, envelope)
        self.assertEqual((envelope["error_code"], envelope["command"]),
                         (errors.INVALID_VALUE, "pressKey"))
        self.assertIn("the key is not a key name Autonom knows", envelope["error"])
        self.assertEqual(self.inputs(), [], "nothing reached adb input")
        self.assertEqual(self.ops("key"), [], "nothing reached the helper")
        self.assert_run_files(envelope["run_id"])
        self.assertEqual(self.files_holding_the_secret(), [])
        self.assertNotIn(self.SECRET, json.dumps(envelope))


if __name__ == "__main__":
    unittest.main()
