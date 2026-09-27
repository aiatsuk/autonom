"""UI core fixes found by a live run against a Flutter app (Android + iOS).

Each case reproduces what the devices reported, from trimmed and anonymised
captures in ``tests/fixtures/woolbox/``:

1. iOS lists Flutter's off-screen (scroll-cache) nodes at ``[0, 0, 0, 0]``.
   ``ui tap`` tapped (0, 0) and said ok; ``assertVisible`` passed on them;
   ``scrollUntilVisible`` "found" one without scrolling.
2. iOS joins a merged Flutter label with a newline, Android with a space, so
   one exact selector matched on one platform only.
3. Android reports window focus on a FrameLayout ahead of the focused
   EditText; ``ui type`` blamed the FrameLayout.
4. The ``--text`` -> ``--desc`` hint was iOS-only; Flutter on Android puts
   labels in ``desc`` too.
5. Flutter marks static labels focusable; ``--interactable`` kept them —
   while a long-press-only or checkable-only control must stay.
6. ``ui wait`` began snapshots it could not finish before ``--timeout-ms``,
   and must not let one cold first dump end the wait early.

Because flows count on-screen nodes only, the repair brief ranks and indexes
the same set, and the CLI's action verbs resolve on-screen matches first.

Hermetic: in-process calls with ``ui.snapshot`` / ``describe_all`` patched,
or the CLI against ``tests/fakes`` with ``AUTONOM_HOME`` in a temp dir. No
device, adb, simctl, idb or AXe is ever reached.
"""
from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"
FAKE_SIMCTL = ROOT / "tests/fakes/fake_simctl.py"
FAKE_IDB = ROOT / "tests/fakes/fake_idb.py"
WOOLBOX = ROOT / "tests/fixtures/woolbox"
IOS_CATALOG = WOOLBOX / "ios_catalog_offscreen.json"
ANDROID_EMAIL = WOOLBOX / "android_email_focused.xml"
ANDROID_SIGN_IN = WOOLBOX / "android_sign_in.xml"
ANDROID_LONG_PRESS = WOOLBOX / "android_long_press.xml"
# `ui tap` / `ui find` resolve on-screen matches first once cmd_ui_tap calls
# ui.select_for_action and cmd_ui_find calls ui.select_for_find (the CLI
# wiring of this change); until then the CLI-level tests of that behaviour
# are skipped, while the library tests below still run.
CLI_SOURCE = CLI.read_text(encoding="utf-8")
CLI_WIRED = "select_for_action" in CLI_SOURCE
FIND_WIRED = "select_for_find" in CLI_SOURCE
SERIAL = "emulator-5554"
UDID = "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB"
BOOTED = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
    {"udid": UDID, "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
MERGED_LABEL = "M Marta Écru"  # iOS reports it as "M\nMarta Écru"
WHISPER = 'Cardigan "Whisper"'      # a card Flutter keeps below the fold

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors, selector, ui, ui_android, ui_ios  # noqa: E402
from autonom_lib.flow import executor as flow_executor  # noqa: E402
from autonom_lib.flow import parser as flow_parser  # noqa: E402
from autonom_lib.flow import repair as flow_repair  # noqa: E402
from autonom_lib.flow import schema as flow_schema  # noqa: E402
from autonom_lib.flow import selectors as flow_selectors  # noqa: E402
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402

IOS_TARGET = Target("ios", UDID, "/fake/xcrun", {"udid": UDID})
_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.yarnshop\nname: t\n---\n"


def ios_payload(*, scrolled: bool = False, irina_scrolled: bool = False) -> str:
    """The raw idb describe-all of the catalog. ``scrolled`` moves the
    Whisper card into view, the way a real swipe re-lays the list out.
    ``irina_scrolled`` is the list one row further: the first "Irina Weaver"
    has left the screen (0x0) and the later copy is on screen, so the
    off-screen copy now comes *first* in tree order."""
    elements = copy.deepcopy(json.loads(IOS_CATALOG.read_text(encoding="utf-8")))
    irinas = [element for element in elements if element["AXLabel"] == "Irina Weaver"]
    for element in elements:
        if scrolled and element["AXLabel"] == WHISPER:
            element["frame"] = {"x": 16, "y": 420, "width": 200, "height": 208}
    if irina_scrolled:
        irinas[0]["frame"] = {"x": 0, "y": 0, "width": 0, "height": 0}
        irinas[1]["frame"] = {"x": 16, "y": 640, "width": 200, "height": 44}
    return json.dumps(elements)


def ios_nodes(*, scrolled: bool = False, irina_scrolled: bool = False) -> list[dict[str, Any]]:
    """What ``ui.snapshot`` builds from that payload, parents included."""
    return ui.annotate_parents(ui_ios.parse_all(
        ios_payload(scrolled=scrolled, irina_scrolled=irina_scrolled)))


def android_nodes(path: Path) -> list[dict[str, Any]]:
    return ui.annotate_parents(ui_android.parse_all(path.read_text(encoding="utf-8")))


def by_desc(nodes: list[dict[str, Any]], desc: str) -> list[dict[str, Any]]:
    return [node for node in nodes if node.get("desc") == desc]


def node(ref: str, **fields: Any) -> dict[str, Any]:
    base = {"ref": ref, "role": "view", "text": None, "desc": None, "resource_id": None,
            "class": "android.view.View", "bounds": [0, 0, 10, 10], "clickable": False,
            "enabled": True, "focusable": False, "focused": False, "scrollable": False,
            "selected": False, "checked": False, "depth": 0}
    base.update(fields)
    return base


# --- 1. off-screen nodes ------------------------------------------------------


class OffscreenTapTests(unittest.TestCase):
    """A node that is not on screen is never tapped by selector."""

    def test_zero_area_node_refuses_with_element_offscreen(self) -> None:
        """Before: center_of returned (0, 0) and the tap 'succeeded' there."""
        (whisper,) = by_desc(ios_nodes(), WHISPER)
        with self.assertRaises(errors.AutonomError) as caught:
            ui.center_of(whisper)
        self.assertEqual(caught.exception.code, "element_offscreen")
        self.assertIn("ui swipe", caught.exception.hint)
        self.assertIn("scrollUntilVisible", caught.exception.hint)
        self.assertEqual(caught.exception.extra.get("bounds"), [0, 0, 0, 0])

    def test_on_screen_and_partly_on_screen_nodes_still_have_a_centre(self) -> None:
        nodes = ios_nodes()
        self.assertEqual(ui.center_of(by_desc(nodes, "Filters")[0]), (350, 94))
        # the last card is cut by the screen edge: partly visible is visible
        self.assertEqual(ui.center_of(by_desc(nodes, 'Jumper "Lagoon"')[0]), (116, 902))

    def test_node_outside_the_app_frame_is_marked_and_never_selected_by_a_flow(self) -> None:
        """Positive area, but entirely below the 440x956 application frame."""
        elements = json.loads(ios_payload())
        elements.append({"AXLabel": "Below the fold", "type": "Button",
                         "frame": {"x": 16, "y": 1200, "width": 200, "height": 44}})
        with mock.patch.object(ui_ios, "describe_all", return_value=json.dumps(elements)):
            nodes = ui.snapshot(IOS_TARGET)
        (below,) = by_desc(nodes, "Below the fold")
        self.assertIs(below.get("visible"), False)
        flow = flow_schema.build_flow(flow_parser.parse_document(
            _HEAD + "- assertVisible:\n    selector:\n      description: Below the fold\n",
            "t.yaml"))
        self.assertEqual(flow_selectors.select_all(nodes, flow.steps[0].selector), [])
        # an explicit viewport refuses it by name ...
        with self.assertRaises(errors.AutonomError) as caught:
            ui.center_of(below, viewport=(0, 0, 440, 956))
        self.assertEqual(caught.exception.code, "element_offscreen")
        # ... but by default the tap guard keeps answering: a whole tree off
        # by the display scale is coordinate_space_mismatch (INV-06)
        self.assertEqual(ui.center_of(below), (116, 1222))

    def test_snapshot_marks_offscreen_nodes_and_keeps_them(self) -> None:
        with mock.patch.object(ui_ios, "describe_all", return_value=ios_payload()):
            nodes = ui.snapshot(IOS_TARGET)
        self.assertEqual(len(nodes), 19, "off-screen nodes are still listed")
        hidden = sorted(n["desc"] for n in nodes if n.get("visible") is False)
        self.assertEqual(hidden, ["Alina August", WHISPER, "Irina Weaver", "Lena Purl",
                                  'Scarf "Café"'])
        # additive: an on-screen node carries no new key at all
        self.assertNotIn("visible", by_desc(nodes, "Filters")[0])

    def test_tree_counts_offscreen_nodes_and_marks_them(self) -> None:
        with mock.patch.object(ui_ios, "describe_all", return_value=ios_payload()):
            nodes, _warnings = ui.tree(IOS_TARGET)
        self.assertEqual(len(nodes), 19)
        self.assertIs(by_desc(nodes, WHISPER)[0].get("visible"), False)

    def test_outline_flags_offscreen_nodes(self) -> None:
        with mock.patch.object(ui_ios, "describe_all", return_value=ios_payload()):
            nodes, _warnings = ui.tree(IOS_TARGET)
        lines = {line.split(" ", 1)[0]: line for line in ui.outline(nodes).splitlines()}
        whisper = by_desc(nodes, WHISPER)[0]["ref"]
        self.assertTrue(lines[whisper].endswith("[offscreen]"), lines[whisper])
        self.assertNotIn("offscreen", lines[by_desc(nodes, "Filters")[0]["ref"]])

    def test_ambiguity_names_the_offscreen_duplicate(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            selector.select(ios_nodes(), {"desc": "Irina Weaver"}, mode="exact")
        self.assertEqual(caught.exception.code, errors.AMBIGUOUS_SELECTOR)
        self.assertIn("1:Irina Weaver (off-screen)", caught.exception.message)
        self.assertNotIn("0:Irina Weaver (off-screen)", caught.exception.message)

    def test_visibility_helpers(self) -> None:
        view = selector.viewport(ios_nodes())
        self.assertEqual(view, (0, 0, 440, 956))
        self.assertFalse(selector.is_visible(node("a", bounds=[0, 0, 0, 0])))
        self.assertFalse(selector.is_visible(node("b", bounds=[5, 5, 5, 40])))
        self.assertTrue(selector.is_visible(node("c", bounds=[10, 10, 20, 20]), view))
        self.assertFalse(selector.is_visible(node("d", bounds=[10, 960, 20, 990]), view))
        # no geometry at all proves nothing: not refused as off-screen
        self.assertTrue(selector.is_visible(node("e", bounds=None), view))


class ActionSelectionTests(unittest.TestCase):
    """``ui.select_for_action``: what ``ui tap`` resolves, on-screen first."""

    def test_the_on_screen_duplicate_wins_as_on_android(self) -> None:
        """Before: ambiguous_selector on iOS, one match on Android and in flows."""
        nodes = ios_nodes()
        picked = ui.select_for_action(nodes, {"desc": "Irina Weaver"}, mode="exact")
        self.assertEqual(picked["ref"], by_desc(nodes, "Irina Weaver")[0]["ref"])
        self.assertEqual(ui.center_of(picked), (116, 336))

    def test_index_counts_on_screen_matches_like_a_flow(self) -> None:
        nodes = ios_nodes(irina_scrolled=True)
        off, on = by_desc(nodes, "Irina Weaver")
        picked = ui.select_for_action(nodes, {"desc": "Irina Weaver"}, mode="exact", index=0)
        self.assertEqual(picked["ref"], on["ref"])
        with self.assertRaises(errors.AutonomError) as caught:
            ui.select_for_action(nodes, {"desc": "Irina Weaver"}, mode="exact", index=1)
        self.assertEqual(caught.exception.code, errors.SELECTOR_INDEX_OUT_OF_RANGE)
        self.assertIn("on-screen", caught.exception.message)
        # `ui find` resolves exactly as tap does; only --all lists every match,
        # in the order --index counts them: on-screen first
        self.assertEqual(ui.select_for_find(nodes, {"desc": "Irina Weaver"}, mode="exact",
                                            index=0)[0]["ref"], on["ref"])
        listed = ui.select_for_find(nodes, {"desc": "Irina Weaver"}, mode="exact",
                                    all_matches=True)
        self.assertEqual([(m["ref"], m.get("visible", True), m["index"]) for m in listed],
                         [(on["ref"], True, 0), (off["ref"], False, None)])

    def test_only_offscreen_matches_refuse_with_element_offscreen(self) -> None:
        nodes = ios_nodes()
        with self.assertRaises(errors.AutonomError) as caught:
            ui.select_for_action(nodes, {"desc": WHISPER}, mode="exact")
        self.assertEqual(caught.exception.code, "element_offscreen")
        self.assertIn("scrollUntilVisible", caught.exception.hint)
        # two off-screen matches: the same ambiguity find reports, each marked
        # off-screen; picked by --index, tap refuses that one by name
        both = {"desc": "^(Lena Purl|Alina August)$"}
        with self.assertRaises(errors.AutonomError) as caught:
            ui.select_for_action(nodes, both, mode="regex")
        self.assertEqual(caught.exception.code, errors.AMBIGUOUS_SELECTOR)
        self.assertEqual(caught.exception.message.count("(off-screen)"), 2)
        with self.assertRaises(errors.AutonomError) as caught:
            ui.select_for_action(nodes, both, mode="regex", index=1)
        self.assertEqual(caught.exception.code, "element_offscreen")
        self.assertEqual(caught.exception.extra["ref"], by_desc(nodes, "Alina August")[0]["ref"])
        self.assertEqual(caught.exception.extra["match_count"], 2)

    def test_no_match_is_none(self) -> None:
        self.assertIsNone(ui.select_for_action(ios_nodes(), {"desc": "Nope"}, mode="exact"))

    def test_real_area_outside_the_screen_still_meets_the_tap_guard(self) -> None:
        """INV-06: a tree off by the display scale answers coordinate_space_mismatch."""
        elements = json.loads(ios_payload())
        elements.append({"AXLabel": "Below the fold", "type": "Button",
                         "frame": {"x": 16, "y": 1200, "width": 200, "height": 44}})
        nodes = ui.annotate_parents(ui_ios.parse_all(json.dumps(elements)))
        picked = ui.select_for_action(nodes, {"desc": "Below the fold"}, mode="exact")
        x, y = ui.center_of(picked)
        with mock.patch.object(ui_ios, "tap") as dispatch:
            with self.assertRaises(errors.AutonomError) as caught:
                ui.tap(IOS_TARGET, x, y, screen=(440, 956))
        self.assertEqual(caught.exception.code, errors.COORDINATE_SPACE_MISMATCH)
        dispatch.assert_not_called()


def add_payload(*frames: tuple[int, int, int, int]) -> str:
    """An iOS screen (440x956 app frame) holding one "Add" button per frame."""
    elements: list[dict[str, Any]] = [
        {"AXLabel": "Yarn Shop", "type": "Application",
         "frame": {"x": 0, "y": 0, "width": 440, "height": 956}}]
    for x, y, width, height in frames:
        elements.append({"AXLabel": "Add", "type": "Button",
                         "frame": {"x": x, "y": y, "width": width, "height": height}})
    return json.dumps(elements)


# the reviewer's screens: refs are n1, n2, n3 in tree order
ZERO_THEN_TWO = add_payload((0, 0, 0, 0), (16, 100, 200, 44), (16, 200, 200, 44))
ABOVE_THEN_ONE = add_payload((16, -300, 200, 44), (16, 100, 200, 44))


def find_command_args(command: str) -> tuple[dict[str, Any], str, bool, int | None]:
    """Parse the `autonom ui find ...` command a repair candidate prints."""
    argv = shlex.split(command)
    assert argv[:3] == ["autonom", "ui", "find"], argv
    fields: dict[str, Any] = {}
    mode, case_sensitive, index = "contains", False, None
    flags = {"--desc": "desc", "--text": "text", "--resource-id": "resource_id",
             "--role": "role"}
    rest = argv[3:]
    while rest:
        flag = rest.pop(0)
        if flag in flags:
            fields[flags[flag]] = rest.pop(0)
        elif flag == "--mode":
            mode = rest.pop(0)
        elif flag == "--case-sensitive":
            case_sensitive = True
        elif flag == "--index":
            index = int(rest.pop(0))
        else:
            raise AssertionError(f"unexpected flag {flag}")
    return fields, mode, case_sensitive, index


class FindTapParityTests(unittest.TestCase):
    """`ui find` (without --all) resolves exactly the node `ui tap` acts on."""

    @staticmethod
    def _find(nodes: list[dict[str, Any]], fields: dict[str, Any], **kwargs: Any) -> tuple:
        try:
            found = ui.select_for_find(nodes, fields, **kwargs)
        except errors.AutonomError as exc:
            return ("error", exc.code, exc.message)
        return ("node", found[0]["ref"]) if found else ("none",)

    @staticmethod
    def _tap(nodes: list[dict[str, Any]], fields: dict[str, Any], **kwargs: Any) -> tuple:
        try:
            node = ui.select_for_action(nodes, fields, **kwargs)
        except errors.AutonomError as exc:
            if exc.code == "element_offscreen":  # tap refuses what find shows
                return ("node", exc.extra["ref"])
            return ("error", exc.code, exc.message)
        return ("node", node["ref"]) if node else ("none",)

    def test_zero_area_first_then_two_on_screen(self) -> None:
        """Before: find --index 1 returned n2 while tap --index 1 tapped n3."""
        nodes = ui.annotate_parents(ui_ios.parse_all(ZERO_THEN_TWO))
        add = {"desc": "Add"}
        for index in (0, 1):
            with self.subTest(index=index):
                self.assertEqual(self._find(nodes, add, mode="exact", index=index),
                                 self._tap(nodes, add, mode="exact", index=index))
        self.assertEqual(self._find(nodes, add, mode="exact", index=1), ("node", "n3"))
        # and the same refusal for the same reasons
        for index in (None, 2):
            with self.subTest(index=index):
                found = self._find(nodes, add, mode="exact", index=index)
                self.assertEqual(found[0], "error")
                self.assertEqual(found, self._tap(nodes, add, mode="exact", index=index))

    def test_real_area_above_the_screen_then_one_on_screen(self) -> None:
        nodes = ui.annotate_parents(ui_ios.parse_all(ABOVE_THEN_ONE))
        add = {"desc": "Add"}
        self.assertEqual(self._find(nodes, add, mode="exact", index=0), ("node", "n2"))
        for index in (None, 0, 1):
            with self.subTest(index=index):
                self.assertEqual(self._find(nodes, add, mode="exact", index=index),
                                 self._tap(nodes, add, mode="exact", index=index))
        found = self._find(nodes, add, mode="exact", index=1)
        self.assertEqual(found[:2], ("error", errors.SELECTOR_INDEX_OUT_OF_RANGE))

    def test_find_and_tap_agree_on_every_selector_and_index(self) -> None:
        """Exhaustive over the catalog: one resolution core, no drift."""
        selectors: list[tuple[dict[str, Any], str]] = [
            ({"desc": n["desc"]}, "exact") for n in ios_nodes() if n.get("desc")]
        selectors += [({"desc": "Weaver"}, "contains"), ({"desc": "a"}, "contains"),
                      ({"desc": "^(Lena Purl|Alina August)$"}, "regex")]
        for nodes in (ios_nodes(), ios_nodes(irina_scrolled=True)):
            for fields, mode in selectors:
                for index in (None, 0, 1, 2, -1):
                    with self.subTest(fields=fields, index=index):
                        self.assertEqual(
                            self._find(nodes, fields, mode=mode, index=index),
                            self._tap(nodes, fields, mode=mode, index=index))

    def test_find_shows_an_offscreen_node_marked_even_from_an_unmarked_tree(self) -> None:
        nodes = ui_ios.parse_all(ios_payload())  # a --dump: no visible marks yet
        (whisper,) = ui.select_for_find(nodes, {"desc": WHISPER}, mode="exact")
        self.assertIs(whisper["visible"], False)
        self.assertNotIn("visible", by_desc(nodes, WHISPER)[0], "the tree is not mutated")

    def test_the_repair_confirm_command_resolves_to_the_suggested_node(self) -> None:
        """Before: the printed `ui find` answered ambiguous_selector."""
        nodes = ios_nodes(irina_scrolled=True)
        (candidate,) = [c for c in flow_repair.rank_candidates(
            {"description": "Irina Waever"}, nodes) if c["node"].get("desc") == "Irina Weaver"]
        fields, mode, case_sensitive, index = find_command_args(candidate["command"])
        found = ui.select_for_find(nodes, fields, mode=mode, case_sensitive=case_sensitive,
                                   index=index)
        self.assertEqual([n["ref"] for n in found], [candidate["node"]["ref"]])


class RepairCandidateTests(unittest.TestCase):
    """The repair brief ranks and indexes what a flow can select."""

    def test_suggestion_skips_the_offscreen_copy_and_needs_no_index(self) -> None:
        """Before: the 0x0 copy came first, so the on-screen node got index 1,
        and pasting it into tapOn raised selector_index_out_of_range."""
        nodes = ios_nodes(irina_scrolled=True)
        on_screen = by_desc(nodes, "Irina Weaver")[1]
        candidates = flow_repair.rank_candidates({"description": "Irina Waever"}, nodes)
        irina = [c for c in candidates if c["node"].get("desc") == "Irina Weaver"]
        self.assertEqual([c["selector"] for c in irina], [{"description": "Irina Weaver"}])
        self.assertEqual(irina[0]["node"]["ref"], on_screen["ref"])
        pasted = flow_schema.build_flow(flow_parser.parse_document(
            _HEAD + "- tapOn:\n    selector:\n      description: Irina Weaver\n",
            "t.yaml")).steps[0].selector
        self.assertEqual([n["ref"] for n in flow_selectors.select(nodes, pasted)],
                         [on_screen["ref"]])

    def test_ambiguous_listing_counts_on_screen_matches_only(self) -> None:
        nodes = ios_nodes()
        listed = flow_repair.rank_candidates(
            {"description": "Irina Weaver", "match": "exact"}, nodes,
            error_code=errors.AMBIGUOUS_SELECTOR)
        self.assertEqual([c["node"]["ref"] for c in listed],
                         [by_desc(nodes, "Irina Weaver")[0]["ref"]])


class _Executor(EnvSandboxMixin, unittest.TestCase):
    """The flow executor in-process with a scripted screen."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sandbox_home()
        no_shot = errors.AutonomError(errors.BACKEND_FAILED, "no screenshot in tests")
        for patcher in (
                mock.patch("autonom_lib.screenshot.capture_evidence", side_effect=no_shot),
                mock.patch("autonom_lib.logs.tail", return_value=([], []))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.swipe = self._patch("autonom_lib.ui.swipe")
        self.tap = self._patch("autonom_lib.ui.tap")
        self.screen = ios_nodes()
        self._patch("autonom_lib.ui.snapshot", side_effect=lambda _t: self.current())
        self._patch("autonom_lib.ui.tree", side_effect=lambda _t, **_k: (self.current(), []))

    def _patch(self, name: str, **kwargs: Any) -> mock.MagicMock:
        patcher = mock.patch(name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def current(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.screen)

    def run_flow(self, body: str):
        flow = flow_schema.build_flow(
            flow_parser.parse_document(_HEAD + body, str(self.root / "t.yaml")))
        session = {"session_id": "s_test", "artifacts_dir": str(self.root / "art")}
        clock = [0.0]
        runner = flow_executor.Executor(
            IOS_TARGET, session,
            flow_executor.RunConfig(default_timeout_ms=1000, interval_ms=500),
            clock=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            screen=(440, 956),
        )
        return runner.run(flow)


class OffscreenFlowTests(_Executor):
    def test_assert_visible_fails_on_an_offscreen_node(self) -> None:
        """Before: passed on the 0x0 node (a false green)."""
        result = self.run_flow(
            "- assertVisible:\n    selector:\n"
            f"      description: '{WHISPER}'\n    timeoutMs: 1000\n")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure["error_code"], "flow_assertion_timeout")

    def test_assert_not_visible_passes_for_an_offscreen_node(self) -> None:
        result = self.run_flow(
            "- assertNotVisible:\n    selector:\n"
            f"      description: '{WHISPER}'\n    timeoutMs: 1000\n")
        self.assertEqual(result.status, "passed", result.failure)

    def test_scroll_until_visible_scrolls_until_the_node_is_on_screen(self) -> None:
        """Before: 'found' the 0x0 node at once and never scrolled."""
        def screen() -> list[dict[str, Any]]:
            return ios_nodes(scrolled=self.swipe.call_count >= 2)

        self.current = screen  # type: ignore[method-assign]
        result = self.run_flow(
            "- scrollUntilVisible:\n    selector:\n"
            f"      description: '{WHISPER}'\n    maxSwipes: 4\n")
        self.assertEqual(result.status, "passed", result.failure)
        self.assertEqual(self.swipe.call_count, 2)

    def test_tap_on_never_taps_an_offscreen_node(self) -> None:
        """Before: tapped (0, 0) and passed."""
        result = self.run_flow(
            "- tapOn:\n    selector:\n"
            f"      description: '{WHISPER}'\n    timeoutMs: 1000\n")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure["error_code"], "flow_assertion_timeout")
        self.tap.assert_not_called()

    def test_tap_on_takes_the_on_screen_duplicate(self) -> None:
        """Android lists only the on-screen author; iOS adds a 0x0 copy.
        Before: ambiguous_selector on iOS for a selector unique on Android."""
        result = self.run_flow(
            "- tapOn:\n    selector:\n      description: Irina Weaver\n")
        self.assertEqual(result.status, "passed", result.failure)
        self.assertEqual(self.tap.call_args.args[1:3], (116, 336))

    def test_visible_text_matches_the_merged_label_on_both_platforms(self) -> None:
        """Before: iOS's "M\\nMarta" missed an exact visibleText selector."""
        body = ("- assertVisible:\n    selector:\n"
                f"      visibleText: {MERGED_LABEL}\n    timeoutMs: 1000\n")
        self.assertEqual(self.run_flow(body).status, "passed")
        self.screen = android_nodes(ANDROID_SIGN_IN)
        self.assertEqual(self.run_flow(body).status, "passed")


class FlowSelectorShimTests(unittest.TestCase):
    @staticmethod
    def _selector(yaml_fields: str) -> Any:
        body = "- assertVisible:\n    selector:\n" + yaml_fields
        flow = flow_schema.build_flow(flow_parser.parse_document(_HEAD + body, "t.yaml"))
        return flow.steps[0].selector

    def test_select_all_drops_offscreen_matches(self) -> None:
        nodes = ios_nodes()
        whisper = self._selector(f"      description: '{WHISPER}'\n")
        self.assertEqual(flow_selectors.select_all(nodes, whisper), [])
        irina = self._selector("      description: Irina Weaver\n")
        refs = [n["ref"] for n in flow_selectors.select_all(nodes, irina)]
        self.assertEqual(refs, [by_desc(nodes, "Irina Weaver")[0]["ref"]])

    def test_offscreen_geometric_anchor_is_ignored(self) -> None:
        """An off-screen copy of the anchor must neither be the reference
        rectangle nor make the anchor ambiguous. Before: ambiguous_selector."""
        relational = self._selector(
            "      description: Tatiana Skein\n"
            "      rightOf:\n"
            "        description: Irina Weaver\n")
        matches = flow_selectors.select(ios_nodes(), relational)
        self.assertEqual([m["desc"] for m in matches], ["Tatiana Skein"])


# --- 2. whitespace ------------------------------------------------------------


class WhitespaceTests(unittest.TestCase):
    def test_ios_newline_label_matches_a_spaced_exact_selector(self) -> None:
        matches = selector.select(ios_nodes(), {"desc": MERGED_LABEL}, mode="exact",
                                  case_sensitive=True)
        self.assertEqual([m["desc"] for m in matches], ["M\nMarta Écru"])

    def test_android_spaced_label_matches_a_newline_selector(self) -> None:
        matches = selector.select(android_nodes(ANDROID_SIGN_IN),
                                  {"desc": "M\nMarta Écru"}, mode="exact",
                                  case_sensitive=True)
        self.assertEqual([m["desc"] for m in matches], [MERGED_LABEL])

    def test_contains_and_case_insensitive_collapse_runs(self) -> None:
        for nodes in (ios_nodes(), android_nodes(ANDROID_SIGN_IN)):
            with self.subTest(platform=nodes[0]["role"]):
                self.assertEqual(len(selector.select(
                    nodes, {"desc": "m  marta écru "}, mode="exact")), 1)
                self.assertEqual(len(selector.select(
                    nodes, {"desc": "M\tMarta"}, mode="contains", case_sensitive=True)), 1)

    def test_visible_text_field_is_normalized_too(self) -> None:
        self.assertEqual(len(selector.select(
            ios_nodes(), {"visible_text": MERGED_LABEL}, mode="exact")), 1)

    def test_regex_sees_the_raw_value(self) -> None:
        nodes = ios_nodes()
        self.assertEqual(selector.select(nodes, {"desc": "^M Marta"}, mode="regex"), [])
        self.assertEqual(len(selector.select(nodes, {"desc": "^M\\nMarta"}, mode="regex")), 1)

    def test_whitespace_only_selector_is_compared_raw(self) -> None:
        """Normalized, " " would be "" — and contains "" matches every node.
        Before: all 12 sign-in nodes matched --text " "."""
        android = android_nodes(ANDROID_SIGN_IN)
        self.assertEqual(selector.select(android, {"text": " "}, mode="contains",
                                         all_matches=True), [])
        ios = ios_nodes()
        spaced = [n["ref"] for n in ios if " " in (n.get("desc") or "")]
        found = selector.select(ios, {"desc": " "}, mode="contains", all_matches=True)
        self.assertEqual([n["ref"] for n in found], spaced)
        self.assertNotIn(by_desc(ios, "Filters")[0]["ref"], [n["ref"] for n in found])


# --- 3. typing focus ----------------------------------------------------------


class FocusTests(unittest.TestCase):
    def test_typing_target_prefers_the_focused_edit_text(self) -> None:
        """Before: the focused window FrameLayout came first and won."""
        focused, certainty = ui.typing_target("android", android_nodes(ANDROID_EMAIL))
        self.assertEqual((focused["class"], certainty), ("android.widget.EditText", "focused"))

    def test_deepest_focused_node_when_none_is_editable(self) -> None:
        nodes = [node("n0", focused=True, depth=1, **{"class": "android.widget.FrameLayout"}),
                 node("n1", depth=2),
                 node("n2", focused=True, depth=4, role="button",
                      **{"class": "android.widget.Button"})]
        self.assertEqual(ui.focused_node(nodes)["ref"], "n2")
        self.assertEqual(ui.typing_target("android", nodes)[1], "focused_not_editable")


# --- 4. the --text -> --desc hint -------------------------------------------------


class DescHintTests(unittest.TestCase):
    def test_android_text_miss_that_matches_desc_hints_desc(self) -> None:
        """Before: the hint was iOS-only."""
        hint = ui.no_match_hint("android", android_nodes(ANDROID_SIGN_IN),
                                {"text": "Sign in with Email"}, mode="exact")
        self.assertIsNotNone(hint)
        self.assertIn("--desc", hint)
        self.assertIn("content-description", hint)

    def test_no_hint_when_desc_does_not_match_either(self) -> None:
        self.assertIsNone(ui.no_match_hint("android", android_nodes(ANDROID_SIGN_IN),
                                           {"text": "Elsewhere"}))
        self.assertIsNone(ui.no_match_hint("android", android_nodes(ANDROID_SIGN_IN),
                                           {"text": "Back", "desc": "Back"}))

    def test_ios_wording_is_unchanged(self) -> None:
        hint = ui.no_match_hint("ios", ios_nodes(), {"text": "Filters"}, mode="exact")
        self.assertIn("AXLabel", hint)
        self.assertIn("--desc", hint)


# --- 5. interactable ----------------------------------------------------------


class InteractableTests(unittest.TestCase):
    def test_focusable_flutter_text_is_not_interactable(self) -> None:
        """Before: every focusable Flutter label (and the root frames) stayed."""
        kept = [n.get("desc") or n["class"] for n in ui.interactable(
            android_nodes(ANDROID_SIGN_IN))]
        self.assertEqual(kept, ["Back", "P Sign in with Partner ID", "Sign in with Email",
                                "android.widget.CheckBox", "Privacy Policy"])

    def test_parser_carries_long_clickable_and_checkable(self) -> None:
        nodes = {n.get("desc"): n for n in android_nodes(ANDROID_LONG_PRESS)}
        self.assertIs(nodes["Hold to delete"]["long_clickable"], True)
        self.assertIs(nodes["Hold to delete"]["checkable"], False)
        self.assertIs(nodes["Accept terms"]["checkable"], True)
        self.assertIs(nodes["Static label"]["long_clickable"], False)

    def test_long_press_only_and_checkable_only_controls_stay(self) -> None:
        """Through the real parser, not hand-built dicts. Before this round:
        both controls were dropped, since no parser set the keys read."""
        kept = [n.get("desc") for n in ui.interactable(android_nodes(ANDROID_LONG_PRESS))]
        # the EditText is editable though not clickable; a disabled control never counts
        self.assertEqual(kept, ["Hold to delete", "Accept terms", "Note"])


# --- 6. ui wait deadline ------------------------------------------------------


class SettleDeadlineTests(unittest.TestCase):
    def _settle(self, cost_s: float, changing: bool, **kwargs: Any) -> dict[str, Any]:
        now = [0.0]
        count = [0]

        def take() -> list[dict[str, Any]]:
            now[0] += cost_s  # a real dump takes time; Android's ~1.5 s
            count[0] += 1
            return [node("n1", text=str(count[0]) if changing else "still")]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        return ui.settle(None, snapshot_fn=take, clock=lambda: now[0], sleep=sleep, **kwargs)

    # Durations are binary-exact (1.5 s, 0.25 s, 0.125 s) so the fake clock
    # never picks up float drift; the arithmetic in each docstring is exact.

    def test_never_starts_a_snapshot_it_cannot_finish(self) -> None:
        """Before: a fifth dump began at 7.0 s and ended at 8.5 s (8500 > 8000).
        Now the fourth ends at 6.75 s and 1.25 s is left, less than one dump."""
        result = self._settle(1.5, True, timeout_ms=8000, quiet_ms=500, interval_ms=250)
        self.assertFalse(result["settled"])
        self.assertLessEqual(result["elapsed_ms"], 8000)
        self.assertEqual((result["snapshots"], result["elapsed_ms"]), (4, 6750))

    def test_a_quiet_screen_still_settles_with_slow_snapshots(self) -> None:
        result = self._settle(1.5, False, timeout_ms=8000, quiet_ms=500, interval_ms=250)
        self.assertTrue(result["settled"])
        self.assertEqual((result["snapshots"], result["elapsed_ms"]), (2, 3250))

    def test_free_snapshots_still_use_the_whole_budget(self) -> None:
        result = self._settle(0.0, True, timeout_ms=1000, quiet_ms=0, interval_ms=125)
        self.assertFalse(result["settled"])
        self.assertEqual((result["snapshots"], result["elapsed_ms"]), (9, 1000))

    def _settle_costs(self, costs: list[float], changing: bool,
                      **kwargs: Any) -> dict[str, Any]:
        """Like ``_settle``, but each dump costs the next entry (the last repeats)."""
        now = [0.0]
        count = [0]

        def take() -> list[dict[str, Any]]:
            now[0] += costs[min(count[0], len(costs) - 1)]
            count[0] += 1
            return [node("n1", text=str(count[0]) if changing else "still")]

        return ui.settle(None, snapshot_fn=take, clock=lambda: now[0],
                         sleep=lambda s: now.__setitem__(0, now[0] + s), **kwargs)

    def test_a_cold_first_dump_does_not_end_the_wait(self) -> None:
        """First dump 3 s (idb reaching its companion), later ones 1 s, 5 s budget.
        Before: the 3 s forecast left no room after it, so a still screen was
        reported unsettled from a single dump. Now the second dump (3.25 s ->
        4.25 s) fits and proves the screen still."""
        result = self._settle_costs([3.0, 1.0], False, timeout_ms=5000, quiet_ms=500,
                                    interval_ms=250)
        self.assertTrue(result["settled"], result)
        self.assertEqual((result["snapshots"], result["elapsed_ms"]), (2, 4250))

    def test_the_forecast_follows_recent_dumps(self) -> None:
        """A moving screen: after the warm 1 s dump ends at 4.25 s, 0.75 s is
        left, too little for 0.25 s of pause plus a 1 s dump — it stops there."""
        result = self._settle_costs([3.0, 1.0], True, timeout_ms=5000, quiet_ms=500,
                                    interval_ms=250)
        self.assertFalse(result["settled"])
        self.assertEqual((result["snapshots"], result["elapsed_ms"]), (2, 4250))

    def test_even_dumps_overrun_by_at_most_half_a_dump(self) -> None:
        """The price of discounting the first dump: when it was not cold after
        all, the second can overrun, never by more than half a dump. 1.5 s
        dumps, 2.6 s budget: the second ends at 3.25 s, 650 ms over (< 750)."""
        result = self._settle(1.5, True, timeout_ms=2600, quiet_ms=500, interval_ms=250)
        self.assertEqual((result["snapshots"], result["elapsed_ms"]), (2, 3250))
        self.assertLessEqual(result["elapsed_ms"] - 2600, 750)

    def test_elapsed_is_the_real_clock_not_clamped(self) -> None:
        """A dump slower than any before it can still overrun; the report
        says so rather than pretending the budget held."""
        now = [0.0]
        costs = iter([0.25, 9.0])

        def take() -> list[dict[str, Any]]:
            now[0] += next(costs, 0.0)
            return [node("n1", text=str(now[0]))]

        result = ui.settle(None, snapshot_fn=take, clock=lambda: now[0],
                           sleep=lambda s: now.__setitem__(0, now[0] + s),
                           timeout_ms=8000, quiet_ms=500, interval_ms=250)
        self.assertFalse(result["settled"])
        self.assertEqual(result["elapsed_ms"], 9500)


class ErrorCodeTests(unittest.TestCase):
    def test_element_offscreen_is_a_stable_code(self) -> None:
        self.assertEqual(getattr(errors, "ELEMENT_OFFSCREEN", None), "element_offscreen")


# --- through the CLI, against the fakes -------------------------------------------


class CliTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state.json"
        self.log = self.root / "log.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        self.write_state()
        self.set_env(
            AUTONOM_HOME=str(self.root / "home"), AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
            AUTONOM_ADB=None, AUTONOM_SIMCTL=None, AUTONOM_IDB=None,
            AUTONOM_AXE=None, AUTONOM_IOS_HID="idb", AUTONOM_IDB_COMPANION=None,
            AUTONOM_IDB_STATE_FILE=str(self.root / "idb-state.json"),
            AUTONOM_IOS_LOG_MAX_MB=None, DEVELOPER_DIR=None,
        )
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])

    def write_state(self, **extra: Any) -> None:
        state = {"devices": [[SERIAL, "device", "product:sdk_gphone64_arm64"]],
                 "ui_dump": str(ANDROID_EMAIL), "simctl_devices": BOOTED,
                 "idb_describe_all": str(IOS_CATALOG)}
        state.update(extra)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=self.root, env=self.env, text=True,
            stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        stream = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return completed.returncode, json.loads(stream)

    def ios(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--simctl", str(FAKE_SIMCTL), "--idb", str(FAKE_IDB),
                            "--udid", UDID, *argv)

    def android(self, *argv: str) -> tuple[int, dict]:
        return self.run_cli("--adb", str(FAKE_ADB), "--serial", SERIAL, *argv)

    def dispatched(self, tool: str, verb: list[str]) -> list[list[str]]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8")
                   .splitlines()]
        return [e["argv"] for e in entries
                if e.get("tool") == tool and e["argv"][:len(verb)] == verb]

    def test_ios_tap_on_an_offscreen_node_refuses_and_dispatches_nothing(self) -> None:
        """Before: exit 0 with x=0, y=0 — and a real tap at the origin."""
        code, payload = self.ios("ui", "tap", "--desc", WHISPER, "--mode", "exact")
        self.assertEqual((code, payload.get("error_code")), (2, "element_offscreen"), payload)
        self.assertIn("scrollUntilVisible", payload["hint"])
        self.assertEqual(self.dispatched("idb", ["ui", "tap"]), [])

    def test_ios_tap_on_a_merged_label_with_a_spaced_selector(self) -> None:
        code, payload = self.ios("ui", "tap", "--desc", MERGED_LABEL, "--mode", "exact")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["x"], payload["y"]), (220, 388))
        self.assertEqual(len(self.dispatched("idb", ["ui", "tap"])), 1)

    def test_ios_tree_and_find_keep_offscreen_nodes_marked(self) -> None:
        code, tree = self.ios("ui", "tree")
        self.assertEqual(code, 0, tree)
        self.assertEqual(tree["count"], 19)
        self.assertIs(by_desc(tree["nodes"], WHISPER)[0].get("visible"), False)
        code, found = self.ios("ui", "find", "--desc", "Irina Weaver", "--mode", "exact",
                               "--all")
        self.assertEqual(code, 0, found)
        self.assertEqual([m.get("visible", True) for m in found["matches"]], [True, False])

    def test_android_type_names_the_focused_edit_text(self) -> None:
        """Before: focus 'unverified' on the window FrameLayout."""
        code, payload = self.android("ui", "type", "hello")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["focus"], "verified")
        self.assertEqual(payload["focused"]["role"], "textfield")
        self.assertNotIn("warnings", payload)

    def test_android_find_text_miss_warns_label_is_in_desc(self) -> None:
        code, payload = self.run_cli("ui", "find", "--dump", str(ANDROID_SIGN_IN),
                                     "--text", "Sign in with Email", "--mode", "exact")
        self.assertEqual((code, payload["count"]), (0, 0))
        codes = {w["code"]: w for w in payload.get("warnings", [])}
        self.assertIn("label_is_in_desc", codes)
        self.assertIn("--desc", codes["label_is_in_desc"]["hint"])

    def test_android_tap_text_miss_hints_desc(self) -> None:
        self.write_state(ui_dump=str(ANDROID_SIGN_IN))
        code, payload = self.android("ui", "tap", "--text", "Sign in with Email",
                                     "--mode", "exact")
        self.assertEqual((code, payload["error_code"]), (2, "no_matching_node"))
        self.assertIn("--desc", payload["hint"])
        self.assertEqual(self.dispatched("adb", ["-s", SERIAL, "shell", "input"]), [])

    def test_android_tree_interactable_drops_static_flutter_text(self) -> None:
        code, payload = self.run_cli("ui", "tree", "--dump", str(ANDROID_SIGN_IN),
                                     "--interactable")
        self.assertEqual(code, 0, payload)
        labels = [n.get("desc") for n in payload["nodes"]]
        self.assertIn("Sign in with Email", labels)
        self.assertNotIn("Welcome to Yarn Shop", labels)
        self.assertNotIn("I give my", labels)

    def test_android_tree_interactable_keeps_long_press_and_checkable_controls(self) -> None:
        code, payload = self.run_cli("ui", "tree", "--dump", str(ANDROID_LONG_PRESS),
                                     "--interactable")
        self.assertEqual(code, 0, payload)
        self.assertEqual([n.get("desc") for n in payload["nodes"]],
                         ["Hold to delete", "Accept terms", "Note"])
        self.assertIs(payload["nodes"][0]["long_clickable"], True)

    @unittest.skipUnless(CLI_WIRED, "needs cmd_ui_tap -> ui.select_for_action (cli_wiring)")
    def test_ios_tap_takes_the_on_screen_duplicate(self) -> None:
        """Before: ambiguous_selector on iOS; Android and flows match one node."""
        code, payload = self.ios("ui", "tap", "--desc", "Irina Weaver", "--mode", "exact")
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["x"], payload["y"]), (116, 336))
        self.assertEqual(len(self.dispatched("idb", ["ui", "tap"])), 1)

    @unittest.skipUnless(CLI_WIRED, "needs cmd_ui_tap -> ui.select_for_action (cli_wiring)")
    def test_ios_tap_with_only_offscreen_matches_refuses(self) -> None:
        """Two off-screen matches: the ambiguity find reports; picked by
        --index, tap refuses that node by name and dispatches nothing."""
        both = ("--desc", "^(Lena Purl|Alina August)$", "--mode", "regex")
        code, payload = self.ios("ui", "tap", *both)
        self.assertEqual((code, payload.get("error_code")), (2, "ambiguous_selector"), payload)
        self.assertEqual(payload["error"].count("(off-screen)"), 2)
        code, payload = self.ios("ui", "tap", *both, "--index", "1")
        self.assertEqual((code, payload.get("error_code")), (2, "element_offscreen"), payload)
        self.assertEqual(payload["match_count"], 2)
        self.assertEqual(self.dispatched("idb", ["ui", "tap"]), [])

    # --- `ui find` resolves like `ui tap` (after the cmd_ui_find wiring) ------

    def use_screen(self, payload: str) -> None:
        screen = self.root / "screen.json"
        screen.write_text(payload, encoding="utf-8")
        self.write_state(idb_describe_all=str(screen))

    def find_then_tap(self, *argv: str) -> tuple[tuple, tuple]:
        """Run `ui find` and `ui tap` with the same selector; reduce each to
        the node it resolved, or the error it raised."""
        code, found = self.ios("ui", "find", *argv)
        find = (("node", found["matches"][0]["ref"]) if code == 0 and found["count"]
                else ("error", found.get("error_code"), found.get("error")))
        code, tapped = self.ios("ui", "tap", *argv)
        tap = (("node", tapped["ref"]) if code == 0
               else ("error", tapped.get("error_code"), tapped.get("error")))
        return find, tap

    @unittest.skipUnless(FIND_WIRED and CLI_WIRED, "needs the cmd_ui_find/cmd_ui_tap wiring")
    def test_find_and_tap_agree_with_a_zero_area_copy_first(self) -> None:
        """Before: find --index 1 returned n2 while tap --index 1 tapped n3."""
        self.use_screen(ZERO_THEN_TWO)
        find, tap = self.find_then_tap("--desc", "Add", "--mode", "exact", "--index", "1")
        self.assertEqual(find, tap)
        self.assertEqual(find, ("node", "n3"))

    @unittest.skipUnless(FIND_WIRED and CLI_WIRED, "needs the cmd_ui_find/cmd_ui_tap wiring")
    def test_find_and_tap_agree_with_a_real_area_node_above_the_screen(self) -> None:
        self.use_screen(ABOVE_THEN_ONE)
        for index in ("0", "1"):
            with self.subTest(index=index):
                find, tap = self.find_then_tap("--desc", "Add", "--mode", "exact",
                                               "--index", index)
                self.assertEqual(find, tap)
                if index == "0":
                    self.assertEqual(find, ("node", "n2"))
                else:
                    self.assertEqual(find[1], "selector_index_out_of_range")

    @unittest.skipUnless(FIND_WIRED, "needs cmd_ui_find -> ui.select_for_find (cli_wiring)")
    def test_ios_find_resolves_the_on_screen_duplicate_like_tap(self) -> None:
        """Before: ambiguous_selector, though tap and flows match one node."""
        code, payload = self.ios("ui", "find", "--desc", "Irina Weaver", "--mode", "exact")
        self.assertEqual((code, payload["count"]), (0, 1), payload)
        self.assertEqual(payload["matches"][0]["bounds"], [16, 314, 216, 358])
        self.assertNotIn("visible", payload["matches"][0])

    @unittest.skipUnless(FIND_WIRED, "needs cmd_ui_find -> ui.select_for_find (cli_wiring)")
    def test_the_repair_confirm_command_resolves_to_the_suggested_node(self) -> None:
        """The printed `ui find`, run as printed, finds the suggested node."""
        self.use_screen(ios_payload(irina_scrolled=True))
        (candidate,) = [c for c in flow_repair.rank_candidates(
            {"description": "Irina Waever"}, ios_nodes(irina_scrolled=True))
            if c["node"].get("desc") == "Irina Weaver"]
        argv = shlex.split(candidate["command"])
        self.assertEqual(argv[:3], ["autonom", "ui", "find"])
        code, payload = self.ios(*argv[1:])
        self.assertEqual((code, payload["count"]), (0, 1), payload)
        self.assertEqual(payload["matches"][0]["ref"], candidate["node"]["ref"])

    @unittest.skipUnless(FIND_WIRED, "needs cmd_ui_find -> ui.select_for_find (cli_wiring)")
    def test_find_from_a_dump_marks_an_offscreen_node(self) -> None:
        code, payload = self.run_cli("ui", "find", "--dump", str(IOS_CATALOG),
                                     "--desc", WHISPER, "--mode", "exact")
        self.assertEqual((code, payload["count"]), (0, 1), payload)
        self.assertIs(payload["matches"][0]["visible"], False)


if __name__ == "__main__":
    unittest.main()
