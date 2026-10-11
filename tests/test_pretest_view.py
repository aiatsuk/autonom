"""Pre-test pack view (tp-019): one offline, injection-safe HTML document.

``view.render`` is pure: it reads a summary dict and an optional review status
and returns text. The tests still run under a temporary ``AUTONOM_HOME`` so a
regression that starts touching the machine store cannot reach the real one.
The page is parsed with the stdlib ``HTMLParser`` to prove which elements and
attributes exist, not just which substrings appear.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.pretest import view  # noqa: E402

BLOB_A = "blobs/sha256/ab/" + "ab" * 32
BLOB_B = "blobs/sha256/cd/" + "cd" * 32
SECTIONS = ("findings", "checks", "health", "review", "coverage", "files", "timing")


def full_summary() -> dict:
    """A summary with every section filled, shaped like contract section 3."""
    return {
        "schema": "autonom.pretest-summary/v1",
        "pretest_id": "pt_20261010T101500_a1b2c3",
        "title": "Catalog shows a retry button on server error",
        "ticket_ref": "WB-42",
        "verdict": "fail",
        "verdict_reasons": ["Check chk-2 failed: 500 not handled", "Finding f-1 is major"],
        "target": {"platform": "android", "target_id": "emulator-5554"},
        "app_id": "com.woolbox.app",
        "base": "main", "head": "feature/retry",
        "created_at": "2026-10-10T10:15:00Z", "ended_at": "2026-10-10T10:27:40Z",
        "parent_pack": None, "kind": "pretest",
        "changed_files": [
            {"path": "lib/catalog/catalog_screen.dart", "status": "M"},
            {"path": "lib/catalog/retry_button.dart", "status": "A"},
            {"path": "lib/catalog/new_name.dart", "status": "R",
             "renamed_from": "lib/catalog/old_name.dart"},
        ],
        "diff_truncated": False,
        "checks": [
            {"check_id": "chk-1", "title": "Catalog opens", "kind": "ticket", "case": None,
             "verdict": "pass", "state": "ended", "note": None, "duration_s": 42.4,
             "run_ids": ["r-1"], "evidence": ["e-1"], "repro": [], "before": None, "after": None},
            {"check_id": "chk-2", "title": "Server error shows retry", "kind": "edge",
             "case": "server-error", "verdict": "fail", "state": "ended",
             "note": "Spinner never ends", "duration_s": 95,
             "run_ids": [], "evidence": ["e-2", "e-4"],
             "repro": [{"seq": 3, "verb": "network mock add", "args": ["--status", "500"],
                        "origin": "agent", "mocks_active": []},
                       {"seq": 4, "verb": "ui tap", "selector": {"text": "Catalog"},
                        "origin": "agent", "mocks_active": ["m-1"]}],
             "before": None, "after": None},
            {"check_id": "chk-3", "title": "Offline banner", "kind": "edge", "case": "offline",
             "verdict": "blocked", "state": "ended", "note": "No network toggle on this device",
             "duration_s": 12, "run_ids": [], "evidence": [], "repro": [],
             "before": None, "after": None},
            {"check_id": "chk-4", "title": "Rotation keeps scroll", "kind": "edge",
             "case": "rotation", "verdict": None, "state": "added", "note": None,
             "duration_s": None, "run_ids": [], "evidence": [], "repro": [],
             "before": None, "after": None},
        ],
        "findings": [
            {"finding_id": "f-1", "check_id": "chk-2", "origin": "agent", "source": None,
             "severity": "major", "title": "No retry after 500",
             "expected": "A retry button appears", "actual": "The spinner keeps turning",
             "confirmed": True, "evidence": ["e-2"], "reproduced": "3/3",
             "stability": "stable", "draft_flow": "drafts/wb-42-f-1.yaml"},
            {"finding_id": "f-2", "check_id": "chk-2", "origin": "auto", "source": "request",
             "severity": "info", "title": "GET /api/v1/catalog answered 500", "expected": None,
             "actual": "500 from api.example.test", "confirmed": False, "evidence": ["e-3"],
             "reproduced": None, "stability": None, "draft_flow": None},
            {"finding_id": "f-3", "check_id": None, "origin": "auto", "source": "log",
             "severity": "info", "title": "E/flutter: unhandled exception", "expected": None,
             "actual": "3 lines", "confirmed": False, "evidence": [],
             "reproduced": None, "stability": None, "draft_flow": None},
        ],
        "not_covered": ["chk-4"], "blocked": ["chk-3"], "open": [],
        "kit": [
            {"id": "offline", "title": "No network", "covered": True, "checks": ["chk-3"],
             "verdicts": ["blocked"]},
            {"id": "server-error", "title": "Server error (5xx)", "covered": True,
             "checks": ["chk-2"], "verdicts": ["fail"]},
            {"id": "slow", "title": "Slow network", "covered": False, "checks": [],
             "verdicts": []},
        ],
        "health": {
            "computed_at": "2026-10-10T10:27:40Z",
            "crashes": {"state": "available", "new": []},
            "logs": {"state": "available", "groups": [
                {"pattern": "E/flutter: Unhandled Exception: SocketException", "count": 3,
                 "first_ts": "2026-10-10T10:20:00Z", "check_id": "chk-2", "level": "error"}]},
            "requests": {"state": "available", "failed": [
                {"id": "fl-9", "method": "GET", "host": "api.example.test",
                 "path": "/api/v1/catalog", "status": 500, "reason": "5xx",
                 "check_id": "chk-2"}]},
            "metrics": {"state": "available", "leads": [
                {"metric": "PSS", "before": 120, "after": 154, "delta": 34,
                 "check_id": "chk-2"}]},
        },
        "evidence": [
            {"id": "e-1", "kind": "screenshot", "path": BLOB_A, "label": "Catalog loaded",
             "check_id": "chk-1", "width": 1080, "height": 2400},
            {"id": "e-2", "kind": "screenshot", "path": BLOB_B, "label": "Spinner stuck",
             "check_id": "chk-2"},
            {"id": "e-3", "kind": "request", "path": "blobs/sha256/ef/" + "ef" * 32,
             "label": "GET /api/v1/catalog", "check_id": "chk-2"},
            {"id": "e-4", "kind": "run", "path": "runs/r-1/", "label": "Flow run r-1",
             "check_id": "chk-2"},
        ],
        "timing": {"wall_s": 760, "checks_s": {"chk-1": 42.4, "chk-2": 95, "chk-3": 12},
                   "actions": 86, "checks": 4, "findings": 3, "auto_findings": 2},
        "review_commands": {
            "approve": "autonom pretest review pack approve",
            "more_checks": "autonom pretest review pack more-checks --check \"<title>\"",
            "return_to_dev": "autonom pretest review pack return-to-dev --finding f-1",
        },
    }


class Page(HTMLParser):
    """Collects every start tag with its attributes, script bodies and ids."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self._script: dict[str, str | None] | None = None
        self._buffer: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_map = dict(attrs)
        self.tags.append((tag, attrs_map))
        if tag == "script":
            self._script, self._buffer = attrs_map, []

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append((self._script, "".join(self._buffer)))
            self._script = None

    def handle_data(self, data):
        if self._script is not None:
            self._buffer.append(data)
        else:
            self.text.append(data)

    def ids(self) -> list[str]:
        return [a["id"] for _t, a in self.tags if a.get("id")]

    def visible(self) -> str:
        return " ".join(" ".join(self.text).split())


def parse(document: str) -> Page:
    page = Page()
    page.feed(document)
    page.close()
    return page


class ViewTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"AUTONOM_HOME": str(Path(self._tmp.name) / "home")})
        env.start()
        self.addCleanup(env.stop)

    def assert_safe(self, document: str) -> Page:
        """No element, attribute or URL in the page comes from data."""
        page = parse(document)
        self.assertNotRegex(document.lower(), r"[a-z][a-z0-9+.-]*://")
        for tag, attrs in page.tags:
            self.assertNotIn(tag, ("iframe", "object", "embed", "svg", "math", "form",
                                   "base", "link", "frame", "video", "audio", "source"))
            for name, value in attrs.items():
                self.assertFalse(name.startswith("on"), f"event handler {name} on <{tag}>")
                self.assertNotIn(name, ("srcdoc", "formaction", "style"))
                if name in ("href", "src"):
                    self.assertTrue(
                        value.startswith("#") or re.match(r"^(blobs|runs)/[A-Za-z0-9._/-]+$",
                                                          value or ""),
                        f"<{tag} {name}={value!r}> is not pack-relative")
                    self.assertNotIn("..", value)
        kinds = [attrs.get("type") for attrs, _body in page.scripts]
        self.assertEqual(kinds, ["application/json", None])
        return page


class RefusalTests(ViewTestBase):
    def _refused(self, *args, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            view.render(*args, **kwargs)
        error = caught.exception
        self.assertEqual(error.code, errors.REPORT_MODEL_INVALID)
        self.assertTrue(error.hint)
        payload = error.as_dict()
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["error_code"], "report_model_invalid")
        self.assertIn("summary.build_summary", payload["hint"])
        json.dumps(payload)  # the CLI emits it as JSON
        return error

    def test_summary_not_an_object(self):
        error = self._refused(["not", "a", "dict"])
        self.assertEqual(error.as_dict()["got"], "list")
        self.assertIn("summary object", error.message)

    def test_wrong_schema(self):
        summary = full_summary()
        summary["schema"] = "autonom.bundle/v2"
        error = self._refused(summary)
        self.assertEqual(error.as_dict()["schema"], "autonom.bundle/v2")

    def test_missing_schema(self):
        summary = full_summary()
        del summary["schema"]
        self.assertEqual(self._refused(summary).as_dict()["schema"], "NoneType")

    def test_overlong_schema_is_cut_in_payload(self):
        summary = full_summary()
        summary["schema"] = "x" * 500
        self.assertEqual(len(self._refused(summary).as_dict()["schema"]), 80)

    def test_review_not_an_object(self):
        error = self._refused(full_summary(), review="approve")
        self.assertEqual(error.as_dict()["got"], "str")
        self.assertIn("review", error.message)

    def test_summary_that_is_not_json(self):
        summary = full_summary()
        summary["timing"]["wall_s"] = float("nan")
        self.assertEqual(self._refused(summary).as_dict()["reason"], "ValueError")
        summary = full_summary()
        summary["extra"] = {1, 2}
        self.assertEqual(self._refused(summary).as_dict()["reason"], "TypeError")


class InjectionTests(ViewTestBase):
    def hostile(self) -> dict:
        summary = full_summary()
        summary["title"] = "<img src=x onerror=alert(1)> see https://evil.example/x"
        summary["ticket_ref"] = '"><script>alert(2)</script>'
        summary["verdict_reasons"] = ["<b onmouseover=alert(3)>reason</b>"]
        summary["target"]["target_id"] = "<svg onload=alert(4)>"
        summary["app_id"] = "javascript:alert(5)"
        summary["checks"][0]["title"] = "</details><a href='http://evil'>x</a>"
        summary["checks"][0]["check_id"] = 'chk-1" onclick="alert(6)'
        summary["checks"][1]["repro"][1]["selector"] = {"text": "</script><script>alert(7)"}
        summary["findings"][0]["actual"] = "</script><script>alert(8)</script>"
        summary["findings"][0]["expected"] = "'\"&<>"
        summary["findings"][0]["draft_flow"] = "HTTP://evil.example/flow.yaml"
        summary["evidence"][0]["label"] = '"><img src=x onerror=alert(9)>'
        summary["evidence"][1]["path"] = "https://evil.example/shot.png"
        summary["evidence"][2]["path"] = "javascript:alert(10)"
        summary["evidence"][3]["path"] = "runs/../../etc/passwd"
        summary["health"]["requests"]["failed"][0]["host"] = "evil.example"
        summary["health"]["requests"]["failed"][0]["path"] = "//evil.example/<x>"
        summary["health"]["logs"]["groups"][0]["pattern"] = "<iframe src=//evil>"
        summary["kit"][0]["title"] = "<form action=x>"
        summary["changed_files"][0]["path"] = "lib/<style>body{}</style>.dart"
        summary["review_commands"]["approve"] = "autonom pretest review '</code><script>x</script>'"
        return summary

    def test_hostile_strings_render_as_text_only(self):
        summary = self.hostile()
        document = view.render(summary, review={
            "decision": "approve", "author": "<i>qa</i>", "note": "<script>alert(11)</script>",
            "at": "2026-10-10T11:00:00Z"})
        page = self.assert_safe(document)
        text = page.visible()
        # The data is shown, as text.
        self.assertIn("<img src=x onerror=alert(1)>", text)
        self.assertIn("</script><script>alert(8)</script>", text)
        self.assertIn("'\"&<>", text)
        self.assertIn('"><img src=x onerror=alert(9)>', text)
        self.assertIn("<i>qa</i>", text)
        self.assertIn("https://evil.example/x", text)  # shown, but not as a URL in the source
        # Nothing from the data became an element: the one image is the safe blob of e-1.
        self.assertEqual([a.get("src") for t, a in page.tags if t == "img"], [BLOB_A])
        self.assertNotIn("<i>qa</i>", document)
        self.assertEqual(document.lower().count("<script"), 2)  # the JSON block and the script

    def test_unsafe_evidence_paths_are_not_linked(self):
        page = self.assert_safe(view.render(self.hostile()))
        hrefs = [a.get("href") for t, a in page.tags if t == "a"]
        srcs = [a.get("src") for t, a in page.tags if t == "img"]
        self.assertIn(BLOB_A, hrefs)  # the safe screenshot still links
        for value in hrefs + srcs:
            self.assertNotIn("evil", value)
            self.assertNotIn("javascript", value)
            self.assertNotIn("..", value)
        self.assertIn("javascript:alert(10)", page.visible())

    def test_safe_screenshot_alt_escaped(self):
        summary = full_summary()
        summary["evidence"][0]["label"] = '"><img src=x onerror=alert(9)>'
        page = self.assert_safe(view.render(summary))
        images = [a for t, a in page.tags if t == "img" and a.get("src") == BLOB_A]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["alt"], 'Screenshot: "><img src=x onerror=alert(9)>, chk-1')
        self.assertEqual(images[0]["loading"], "lazy")
        self.assertEqual((images[0]["width"], images[0]["height"]), ("1080", "2400"))

    def test_hostile_check_id_never_becomes_an_id(self):
        page = self.assert_safe(view.render(self.hostile()))
        ids = page.ids()
        self.assertEqual(len(ids), len(set(ids)))
        for value in ids:
            self.assertRegex(value, r"^[a-z][a-z0-9-]*$")
        self.assertIn("check-row-1", ids)

    def test_embedded_json_round_trips_and_is_escaped(self):
        summary = self.hostile()
        document = view.render(summary)
        page = self.assert_safe(document)
        (attrs, body), = [s for s in page.scripts if s[0].get("type") == "application/json"]
        self.assertEqual(attrs.get("id"), "pretest-data")
        for raw in ("<", ">", "&", "://"):
            self.assertNotIn(raw, body)
        self.assertIn("\\u003c/script\\u003e", body)
        self.assertEqual(json.loads(body), summary)

    def test_inline_script_never_evaluates_data(self):
        page = parse(view.render(full_summary()))
        (_attrs, code), = [s for s in page.scripts if s[0].get("type") is None]
        # the page's own path is the one location fact it reads (the review
        # commands' pack directory); nothing navigates or reads the URL else
        self.assertEqual(code.count("location."), 2)
        code = code.replace("location.protocol", "").replace("location.pathname", "")
        for banned in ("eval", "innerHTML", "outerHTML", "insertAdjacentHTML", "Function(",
                       "document.write", "JSON.parse", "pretest-data", "setTimeout('",
                       "fetch(", "XMLHttpRequest", "import(", "location"):
            self.assertNotIn(banned, code)
        self.assertLess(len(code), 3600)

    def test_no_network_url_anywhere(self):
        document = view.render(self.hostile())
        lowered = document.lower()
        self.assertNotIn("http://", lowered)
        self.assertNotIn("https://", lowered)
        self.assertNotRegex(lowered, r"(src|href)=\"//")
        self.assertNotIn("@import", lowered)
        self.assertNotIn("url(", lowered)


class LayoutTests(ViewTestBase):
    def test_every_section_and_landmark_present(self):
        document = view.render(full_summary())
        page = self.assert_safe(document)
        ids = page.ids()
        for section in SECTIONS:
            self.assertIn(section, ids)
        self.assertEqual(len(ids), len(set(ids)))
        tags = [t for t, _a in page.tags]
        for landmark in ("header", "main", "aside", "footer", "nav"):
            self.assertEqual(tags.count(landmark), 1, landmark)
        self.assertEqual(tags.count("h1"), 1)
        self.assertGreaterEqual(tags.count("h2"), len(SECTIONS))
        self.assertTrue(document.startswith("<!doctype html>\n<html lang=\"en\">"))
        self.assertIn('name="viewport"', document)
        self.assertIn("Content-Security-Policy", document)
        self.assertIn("prefers-color-scheme:dark", document)
        self.assertIn("prefers-reduced-motion:reduce", document)
        self.assertIn("@media print", document)

    def test_phone_dom_order(self):
        page = parse(view.render(full_summary()))
        order = [a["id"] for _t, a in page.tags if a.get("id") in SECTIONS]
        self.assertEqual(order, ["findings", "checks", "health", "review", "coverage",
                                 "files", "timing"])

    def test_full_summary_facts(self):
        page = parse(view.render(full_summary()))
        text = page.visible()
        for fact in ("Catalog shows a retry button on server error", "WB-42", "Android",
                     "emulator-5554", "com.woolbox.app", "Fail", "Awaiting review",
                     "Check chk-2 failed: 500 not handled",
                     "1 pass \u00b7 1 fail \u00b7 1 blocked \u00b7 1 not covered",
                     "1 confirmed \u00b7 2 automatic", "12m 40s", "86",
                     "Expected", "A retry button appears", "The spinner keeps turning",
                     "Reproduced 3/3, stable", "drafts/wb-42-f-1.yaml",
                     "Automatic signals \u2014 unconfirmed", "Never run", "1m 35s",
                     "Spinner never ends", "network mock add", "--status 500", "text=Catalog",
                     "mocks: m-1", "E/flutter: Unhandled Exception: SocketException",
                     "/api/v1/catalog", "\u2014 500", "PSS +34 (lead, not a leak)",
                     "None since start", "lib/catalog/old_name.dart \u2192 lib/catalog/new_name.dart",
                     "autonom pretest review pack approve", "Approve", "More checks",
                     "Return to dev", "The pre-test is enough; QA takes it from here.",
                     "No network", "blocked", "Slow network skipped", "2 of 3 edge cases covered",
                     "Wall time", "Automatic findings", "Not covered: chk-4", "Blocked: chk-3",
                     "Generated by Autonom", "autonom report verify <pack>"):
            self.assertIn(fact, text)

    def test_check_rows_open_by_verdict_and_filters(self):
        page = parse(view.render(full_summary()))
        rows = {a["id"]: a for t, a in page.tags if t == "details" and a.get("id", "").startswith("chk-")}
        self.assertNotIn("open", rows["chk-1"])
        self.assertIn("open", rows["chk-2"])
        self.assertIn("open", rows["chk-3"])
        self.assertNotIn("open", rows["chk-4"])
        self.assertEqual([rows[k]["data-v"] for k in ("chk-1", "chk-2", "chk-3", "chk-4")],
                         ["pass", "fail", "blocked", "nc"])
        bar = [a for t, a in page.tags if a.get("id") == "check-filters"][0]
        self.assertIn("hidden", bar)  # shown only by the script
        chips = [a for t, a in page.tags if t == "button" and a.get("data-f")]
        self.assertEqual([c["data-f"] for c in chips], ["all", "fail", "blocked", "nc", "pass"])
        self.assertEqual([c["aria-pressed"] for c in chips], ["true"] + ["false"] * 4)

    def test_copy_buttons_hidden_without_script_and_live(self):
        document = view.render(full_summary())
        page = parse(document)
        buttons = [a for t, a in page.tags if t == "button" and "copy" in (a.get("class") or "")]
        self.assertEqual(len(buttons), 3)
        ids = page.ids()
        for button in buttons:
            self.assertIn("hidden", button)
            self.assertIn(button["data-for"], ids)
        self.assertEqual(sum(1 for t, a in page.tags if a.get("aria-live") == "polite"), 3)

    def test_finding_links_to_its_check(self):
        page = parse(view.render(full_summary()))
        self.assertIn("#chk-2", [a.get("href") for t, a in page.tags if t == "a"])

    def test_confirmed_findings_sorted_by_severity(self):
        summary = full_summary()
        summary["findings"].insert(0, {
            "finding_id": "f-9", "check_id": "chk-1", "origin": "agent", "severity": "minor",
            "title": "Minor typo", "actual": "Ctalog", "confirmed": True, "evidence": []})
        summary["findings"].append({
            "finding_id": "f-10", "check_id": "chk-2", "origin": "agent", "severity": "blocker",
            "title": "App crashes", "actual": "Crash", "confirmed": True, "evidence": []})
        text = parse(view.render(summary)).visible()
        self.assertLess(text.index("App crashes"), text.index("No retry after 500"))
        self.assertLess(text.index("No retry after 500"), text.index("Minor typo"))

    def test_child_pack_shows_parent_without_host_path(self):
        summary = full_summary()
        summary["kind"] = "more-checks"
        summary["parent_pack"] = "/Users/someone/.autonom/sessions/s_1/pretests/pt_20261009T090000_ffffff/pack"
        summary["checks"][0]["before"] = {"verdict": "fail", "evidence": ["e-2"]}
        summary["checks"][0]["after"] = {"verdict": "pass", "evidence": ["e-1"]}
        document = view.render(summary)
        text = parse(document).visible()
        self.assertIn("Follows pack pt_20261009T090000_ffffff", text)
        self.assertNotIn("/Users/", document.split('id="pretest-data"')[0])
        self.assertRegex(text, r"Before Fail \u2192 After Pass")
        summary["kind"] = "rerun"
        self.assertIn("Rerun of pack pt_20261009T090000_ffffff", parse(view.render(summary)).visible())

    def test_every_verdict_word(self):
        for verdict, word, cls in (("pass", "Pass", "s-pass"), ("fail", "Fail", "s-fail"),
                                   ("blocked", "Blocked", "s-blocked"),
                                   ("not_covered", "Not covered", "s-nc"),
                                   ("inconclusive", "Inconclusive", "s-inc"),
                                   ("weird_state", "Weird state", "s-nc")):
            summary = full_summary()
            summary["verdict"] = verdict
            page = parse(view.render(summary))
            pills = [a for t, a in page.tags if t == "span" and "pill" in (a.get("class") or "").split()]
            self.assertEqual(len(pills), 1)
            self.assertIn(cls, pills[0]["class"])
            self.assertIn(f"Ticket verdict: {word}", page.visible())

    def test_deterministic(self):
        summary = full_summary()
        review = {"decision": "approve", "author": "qa", "at": "2026-10-10T11:00:00Z"}
        first = view.render(summary, review=review)
        self.assertEqual(first, view.render(copy.deepcopy(summary), review=copy.deepcopy(review)))
        self.assertEqual(first, view.render(summary, review=review))
        self.assertEqual(summary, full_summary())  # the input is not modified


class FixRound3ViewTests(ViewTestBase):
    """Review commands with the real pack path, the verdict reason with the
    check title, mock rules in steps (fix round 3, items 1 and 6)."""

    REAL_COMMANDS = {
        "approve": "autonom pretest review <pack> approve",
        "more_checks": 'autonom pretest review <pack> more-checks --check "<title>"',
        "return_to_dev": 'autonom pretest review <pack> return-to-dev --finding f-1 '
                         '--note "<why>"',
    }

    def summary(self) -> dict:
        summary = full_summary()
        summary["review_commands"] = dict(self.REAL_COMMANDS)
        return summary

    def test_commands_keep_the_placeholder_without_script(self):
        document = view.render(self.summary())
        page = self.assert_safe(document)
        commands = [a for t, a in page.tags if t == "code" and "cmd" in (a.get("class") or "")]
        self.assertEqual(len(commands), 3)
        self.assertTrue(all("data-pack" in attrs for attrs in commands))
        self.assertIn("autonom pretest review <pack> approve", page.visible())

    def test_reason_names_the_check_title(self):
        summary = self.summary()
        summary["verdict_reasons"] = ["check chk-2 failed", "check chk-3 is blocked"]
        page = parse(view.render(summary))
        reason = [text for text in page.text if "failed" in text]
        self.assertIn("chk-2 failed: Server error shows retry", reason)
        summary["verdict_reasons"] = ["check chk-9 failed"]
        self.assertIn("chk-9 failed", parse(view.render(summary)).visible())
        summary["verdict_reasons"] = ["finding f-1 is a confirmed major"]
        self.assertIn("finding f-1 is a confirmed major", parse(view.render(summary)).visible())

    def test_mock_rules_in_steps(self):
        summary = self.summary()
        summary["checks"][1]["repro"] = [{
            "seq": 4, "verb": "ui tap", "selector": {"text": "Catalog"}, "origin": "agent",
            "mocks_active": [{"id": "m_2", "match": "*/api/catalog", "method": "GET",
                              "status": 404, "enabled": True},
                             {"url": "https://api.example.test/x", "status": 500}]}]
        text = parse(view.render(summary)).visible()
        self.assertIn("mocks: m_2 GET */api/catalog \u2192 404, "
                      "https://api.example.test/x \u2192 500", text)

    def run_page_script(self, protocol: str, pathname: str, command: str) -> str:
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not on PATH")
        page = parse(view.render(self.summary()))
        (_attrs, code), = [s for s in page.scripts if s[0].get("type") is None]
        harness = (
            "var cmds=[{textContent:" + json.dumps(command) + "}];\n"
            "globalThis.location={protocol:" + json.dumps(protocol) + ",pathname:"
            + json.dumps(pathname) + "};\n"
            "globalThis.window={addEventListener:function(){},matchMedia:function(){"
            "return {matches:false};},innerHeight:800};\n"
            "globalThis.document={getElementById:function(){return null;},"
            "querySelectorAll:function(sel){return sel==='code.cmd[data-pack]'?cmds:[];}};\n"
            + code + "\nprocess.stdout.write(cmds[0].textContent);\n")
        script = Path(self._tmp.name) / "page.js"
        script.write_text(harness, encoding="utf-8")
        done = subprocess.run([node, str(script)], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_page_script_writes_the_real_pack_directory(self):
        import subprocess

        command = self.REAL_COMMANDS["approve"]
        out = self.run_page_script("file:", "/tmp/my%20packs/it's%20here/index.html", command)
        self.assertEqual(out, "autonom pretest review '/tmp/my packs/it'\\''s here' approve")
        # the quoted word is one shell word that reads back as the directory
        word = out[len("autonom pretest review "):-len(" approve")]
        echoed = subprocess.run(["sh", "-c", "printf %s " + word],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(echoed.stdout, "/tmp/my packs/it's here")
        # not a file: URL, not an index.html path, or a broken escape: placeholder stays
        for protocol, pathname in (("http:", "/p/index.html"), ("file:", "/p/other.html"),
                                   ("file:", "/index.html"), ("file:", "/p/%E0%A4%A/index.html")):
            self.assertEqual(self.run_page_script(protocol, pathname, command), command,
                             (protocol, pathname))


class EmptyStateTests(ViewTestBase):
    def minimal(self) -> dict:
        return {"schema": "autonom.pretest-summary/v1", "pretest_id": "pt_x", "title": "T",
                "verdict": "not_covered", "checks": [], "findings": [], "kit": [],
                "evidence": [], "changed_files": [], "health": {}, "timing": {},
                "review_commands": {}}

    def test_no_checks_no_findings_nothing_captured(self):
        page = self.assert_safe(view.render(self.minimal()))
        text = page.visible()
        self.assertIn("No checks were planned \u2014 the ticket is not covered.", text)
        self.assertIn("No findings.", text)
        self.assertIn("Nothing was captured.", text)
        self.assertIn("No edge-case kit coverage was recorded.", text)
        self.assertIn("No changed files were recorded.", text)
        self.assertIn("Command not recorded.", text)
        self.assertIn("none planned", text)
        self.assertIn("0 confirmed \u00b7 0 automatic", text)
        for section in SECTIONS:
            self.assertIn(section, page.ids())
        self.assertNotIn("check-filters", page.ids())

    def test_schema_only_summary(self):
        page = self.assert_safe(view.render({"schema": "autonom.pretest-summary/v1"}))
        text = page.visible()
        self.assertIn("Untitled pre-test", text)
        self.assertIn("Unknown", text)
        for section in SECTIONS:
            self.assertIn(section, page.ids())

    def test_unavailable_and_missing_health_parts(self):
        summary = full_summary()
        summary["health"] = {
            "crashes": {"state": "unavailable", "reason": "adb logcat -b crash refused"},
            "logs": {"state": "available", "groups": []},
            "requests": {"state": "unavailable"},
        }
        text = parse(view.render(summary)).visible()
        self.assertIn("Unavailable: adb logcat -b crash refused", text)
        self.assertIn("Unavailable: no reason recorded", text)
        self.assertIn("Not captured for this pre-test.", text)  # metrics missing
        self.assertIn("Error logs None since start", text)
        self.assertNotRegex(text, r"Error logs 0\b")

    def test_truncated_diff_note(self):
        summary = full_summary()
        summary["diff_truncated"] = True
        self.assertIn("this list may be incomplete", parse(view.render(summary)).visible())

    def test_malformed_items_are_skipped_not_fatal(self):
        summary = full_summary()
        summary["checks"].append("not a check")
        summary["checks"].append({"title": "no id"})
        summary["checks"].append({"check_id": "chk-1", "title": "duplicate id", "verdict": "pass"})
        summary["findings"].append(None)
        summary["findings"].append({"finding_id": "f-x", "severity": ["odd"], "source": {"x": 1},
                                    "origin": "auto", "title": 5, "evidence": "e-1"})
        summary["evidence"].append({"id": "e-9", "kind": "screenshot", "path": None})
        summary["findings"][0]["evidence"] = ["e-2", "e-404", "e-9"]
        summary["kit"] = "nope"
        summary["timing"] = {"wall_s": "soon", "actions": True, "checks_s": {"chk-1": -3}}
        summary["target"] = "android"
        summary["verdict_reasons"] = [{"message": "From a dict"}, 7, None]
        summary["health"]["metrics"]["leads"].append({"metric": "Janky frames", "delta": -2.5})
        page = self.assert_safe(view.render(summary))
        text = page.visible()
        self.assertIn("duplicate id", text)
        self.assertIn("e-404 (not in this pack)", text)
        self.assertIn("Screenshot: e-9", text)
        self.assertIn("From a dict", text)
        self.assertIn("Janky frames -2.5 (lead, not a leak)", text)
        ids = page.ids()
        self.assertEqual(len(ids), len(set(ids)))

    def test_durations(self):
        cases = {None: None, -1: None, 0: "0s", 0.4: "<1s", 5: "5s", 59.6: "1m 00s",
                 760: "12m 40s", 3725: "1h 02m", "12": None, True: None}
        for value, expected in cases.items():
            self.assertEqual(view._dur(value), expected, value)


class ReviewStateTests(ViewTestBase):
    def hero_state(self, review) -> str:
        page = parse(view.render(full_summary(), review=review))
        self.assertIn("review", page.ids())
        return page.visible()

    def test_no_review_awaits(self):
        self.assertIn("Awaiting review", self.hero_state(None))
        self.assertIn("Awaiting review", self.hero_state({}))

    def test_each_decision(self):
        for decision, word in (("approve", "Approved"), ("more-checks", "More checks requested"),
                               ("return-to-dev", "Returned to dev")):
            text = self.hero_state({"decision": decision, "author": "anna",
                                    "at": "2026-10-10T11:05:00Z", "note": "Looks right"})
            self.assertIn(f"{word} by anna \u00b7 2026-10-10 11:05 UTC", text)
            self.assertIn("Looks right", text)

    def test_latest_and_history(self):
        review = {
            "latest": {"decision": "approve", "author": "anna", "created_at": "2026-10-10T12:00:00Z"},
            "decisions": [
                {"decision": "return-to-dev", "author": "boris", "created_at": "2026-10-10T11:00:00Z",
                 "note": "500 path missing"},
                {"decision": "approve", "author": "anna", "created_at": "2026-10-10T12:00:00Z"},
            ],
        }
        text = self.hero_state(review)
        self.assertIn("Approved by anna", text)
        self.assertIn("History", text)
        self.assertIn("Returned to dev \u00b7 boris \u00b7 2026-10-10 11:00 UTC \u00b7 500 path missing", text)
        self.assertEqual(text.count("Approved by anna"), 2)  # hero chip and the review panel

    def test_decisions_list_only(self):
        review = {"decisions": [{"decision": "more-checks", "author": "qa", "ts": "2026-10-10T09:00:00Z"}]}
        text = self.hero_state(review)
        self.assertIn("More checks requested by qa", text)
        self.assertNotIn("History", text)

    def test_confirmed_and_dismissed_automatic_findings(self):
        for review in (
            {"decision": "return-to-dev", "confirmed": ["f-2"], "dismissed": [
                {"finding_id": "f-3", "author": "anna"}]},
            {"decision": "return-to-dev", "confirmed": {"f-2": "review"},
             "dismissed": {"f-3": {"author": "anna"}}},
        ):
            document = view.render(full_summary(), review=review)
            page = parse(document)
            text = page.visible()
            self.assertIn("confirmed by review", text)
            self.assertIn("dismissed by anna", text)
            self.assertIn("2 confirmed \u00b7 1 automatic", text)
            # The confirmed signal moved up among the cards, before the automatic list.
            self.assertLess(text.index("GET /api/v1/catalog answered 500"),
                            text.index("Automatic signals"))
            self.assertIn("<s>E/flutter: unhandled exception</s>", document)

    def test_review_on_unknown_finding_ids_is_harmless(self):
        text = self.hero_state({"confirmed": ["f-404"], "dismissed": ["f-1"]})
        self.assertIn("1 confirmed \u00b7 2 automatic", text)  # f-1 is an agent finding
        self.assertNotIn("dismissed by", text)


if __name__ == "__main__":
    unittest.main()
