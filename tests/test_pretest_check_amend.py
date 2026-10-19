"""`pretest check amend` (A.9) and the race count (A.12 / RISK-038).

The fold and validator are tested on hand-made events; the verbs run through
the real CLI on the fake adb (the harness of test_pretest_cli), so every
refusal is proven to append nothing to ``checks.ndjson``.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pretest_cli import ROOT, PretestCliCase  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))

from autonom_lib import errors  # noqa: E402
from autonom_lib.pretest import checks as checks_mod  # noqa: E402

RACE_HINT = ("count the requests (network requests) and pass --observed-count, or end "
             "with --verdict inconclusive")


def stamp(n: int) -> dict:
    return {"ts": f"2026-10-17T10:00:{n:02d}.000Z", "journal_seq": n}


def add(check_id: str = "chk-1", case: str | None = None) -> dict:
    return {"event": "add", "check_id": check_id, "title": "T", "kind": "edge",
            "case": case, "why": None, "flow": None, **stamp(1)}


def begin(check_id: str = "chk-1") -> dict:
    return {"event": "begin", "check_id": check_id, **stamp(2)}


def end(verdict: str, note: str | None = None, check_id: str = "chk-1", **extra) -> dict:
    return {"event": "end", "check_id": check_id, "verdict": verdict, "note": note,
            "run_id": None, **stamp(3), **extra}


def amended(old: str, new: str, note: str, check_id: str = "chk-1", n: int = 4,
            **extra) -> dict:
    return {"event": "amended", "check_id": check_id, "old_verdict": old,
            "new_verdict": new, "note": note, "at": stamp(n)["ts"], **stamp(n), **extra}


class FoldAndSchemaTests(unittest.TestCase):
    def test_amended_event_validates_and_bad_ones_fail(self) -> None:
        self.assertEqual(checks_mod.event_failures(amended("fail", "pass", "why")), [])
        self.assertEqual(checks_mod.event_failures(
            amended("fail", "pass", "why", observed_count=0)), [])
        cases = {
            "old_verdict": amended("nope", "pass", "why"),
            "new_verdict": amended("fail", "great", "why"),
            "note": amended("fail", "pass", ""),
            "at": {**amended("fail", "pass", "why"), "at": "yesterday"},
            "observed_count": amended("fail", "pass", "why", observed_count=-1),
        }
        for key, event in cases.items():
            with self.subTest(key=key):
                failures = checks_mod.event_failures(event)
                self.assertTrue(any(key in item for item in failures), failures)
        for bad in (-1, 1.5, True, "2", None):
            with self.subTest(observed_count=bad):
                self.assertIn("observed_count must be an integer >= 0",
                              checks_mod.event_failures(end("pass", observed_count=bad)))
        with self.assertRaises(errors.AutonomError):
            checks_mod.validate_event(amended("fail", "pass", ""))

    def test_fold_takes_the_latest_verdict_and_keeps_the_history(self) -> None:
        state = checks_mod.fold_events([
            add(), begin(), end("fail", "500 not handled"),
            amended("fail", "blocked", "test backend down", n=4),
            amended("blocked", "pass", "rerun on a good backend", n=5)])[0]
        self.assertEqual((state["state"], state["verdict"]), ("ended", "pass"))
        self.assertEqual(state["note"], "amended from blocked; rerun on a good backend")
        self.assertEqual([(item["old_verdict"], item["new_verdict"], item["previous_note"])
                          for item in state["amendments"]],
                         [("fail", "blocked", "500 not handled"),
                          ("blocked", "pass", "test backend down")])
        self.assertEqual(state["amendments"][0]["at"], stamp(4)["ts"])
        self.assertEqual(state["ended_at"], stamp(3)["ts"])  # the end time never moves

    def test_amended_before_end_is_ignored(self) -> None:
        state = checks_mod.fold_events([add(), begin(),
                                        amended("fail", "pass", "too early")])[0]
        self.assertEqual((state["state"], state["verdict"]), ("open", None))
        self.assertNotIn("amendments", state)

    def test_count_leads_the_note_and_only_new_keys_appear_when_used(self) -> None:
        counted = checks_mod.fold_events([add(case="double-tap"), begin(),
                                          end("pass", "one order", observed_count=1)])[0]
        self.assertEqual(counted["observed_count"], 1)
        self.assertEqual(counted["note"], "observed requests: 1; one order")
        bare = checks_mod.fold_events([add(), begin(), end("pass", observed_count=0)])[0]
        self.assertEqual(bare["note"], "observed requests: 0")
        plain = checks_mod.fold_events([add(), begin(), end("fail", "x")])[0]
        # unchanged shape for a pre-test without counts or amendments (pack digests)
        self.assertNotIn("observed_count", plain)
        self.assertNotIn("amendments", plain)
        self.assertEqual(plain["note"], "x")
        recount = checks_mod.fold_events([
            add(case="double-tap"), begin(), end("inconclusive", "slow taps"),
            amended("inconclusive", "pass", "counted later", observed_count=2)])[0]
        self.assertEqual(recount["observed_count"], 2)
        self.assertEqual(recount["note"],
                         "observed requests: 2; amended from inconclusive; counted later")

    def test_schema_file_names_the_amended_event_and_the_count(self) -> None:
        schema = json.loads((ROOT / "schemas/pretest-check-v1.schema.json").read_text())
        self.assertEqual(schema["properties"]["event"]["enum"], list(checks_mod.EVENTS))
        amended_rule = next(rule for rule in schema["allOf"]
                            if rule["if"]["properties"]["event"]["const"] == "amended")
        self.assertEqual(amended_rule["then"]["required"],
                         ["old_verdict", "new_verdict", "note", "at"])
        end_rule = next(rule for rule in schema["allOf"]
                        if rule["if"]["properties"]["event"]["const"] == "end")
        self.assertIn("observed_count", end_rule["then"]["properties"])
        state = schema["$defs"]["state"]
        self.assertIn("observed_count", state["properties"])
        self.assertIn("amendments", state["properties"])
        self.assertNotIn("observed_count", state["required"])
        self.assertEqual(schema["$defs"]["observed_count"]["minimum"], 0)

    def test_race_cases_are_kit_ids(self) -> None:
        from autonom_lib.pretest import kit as kit_mod
        self.assertTrue(set(checks_mod.RACE_CASES) <= set(kit_mod.ids()))
        self.assertEqual(checks_mod.RACE_HINT, RACE_HINT)


class ObservedCountParseTests(unittest.TestCase):
    """``--observed-count``: at most 9 ASCII digits, never a raw ValueError."""

    def test_digit_limit(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("autonom_cli_count_test",
                                                      ROOT / "scripts/autonom.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.assertIsNone(cli._observed_count(None))  # noqa: SLF001
        self.assertEqual(cli._observed_count("0"), 0)  # noqa: SLF001
        self.assertEqual(cli._observed_count(" 123456789 "), 123456789)  # noqa: SLF001
        for bad in ("1234567890", "9" * 5000, "", "1_0", "1e3", "-1", "²"):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(errors.AutonomError) as caught:
                    cli._observed_count(bad)  # noqa: SLF001
                self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
                self.assertIn("--observed-count 1", caught.exception.hint)


class AmendCliTests(PretestCliCase):
    def checks_file(self, directory: Path) -> list[dict]:
        path = directory / "checks.ndjson"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_amend_refusals_append_nothing(self) -> None:
        self.start_session()
        self.refused("no_active_pretest", "pretest", "check", "amend", "chk-1",
                     "--verdict", "pass", "--note", "x")
        directory = Path(self.start_pretest()["dir"])
        self.ok("pretest", "check", "add", "Search shows results", "--kind", "ticket")
        lines = len(self.checks_file(directory))
        payload = self.refused("check_not_ended", "pretest", "check", "amend", "chk-1",
                               "--verdict", "pass", "--note", "x")
        self.assertEqual(payload["state"], "added")
        self.assertIn("check begin chk-1", payload["hint"])
        self.ok("pretest", "check", "begin", "chk-1")
        payload = self.refused("check_not_ended", "pretest", "check", "amend", "chk-1",
                               "--verdict", "pass", "--note", "x")
        self.assertEqual(payload["state"], "open")
        self.assertIn("check end chk-1", payload["hint"])
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "fail", "--note", "500")
        lines = len(self.checks_file(directory))
        self.refused("unknown_check", "pretest", "check", "amend", "chk-9",
                     "--verdict", "pass", "--note", "x")
        self.refused("invalid_verdict", "pretest", "check", "amend", "chk-1",
                     "--verdict", "great", "--note", "x")
        self.refused("invalid_value", "pretest", "check", "amend", "chk-1",
                     "--verdict", "pass", "--note", "  ")
        self.refused("invalid_value", "pretest", "check", "amend", "chk-1",
                     "--verdict", "pass", "--note", "x", "--observed-count", "-1")
        for long_count in ("1234567890", "9" * 5000):
            payload = self.refused("invalid_value", "pretest", "check", "amend", "chk-1",
                                   "--verdict", "pass", "--note", "x",
                                   "--observed-count", long_count)
            self.assertIn("--observed-count 1", payload["hint"])
        usage = self.refused("usage_error", "pretest", "check", "amend", "chk-1",
                             "--verdict", "pass")  # --note is required
        self.assertIn("--note", usage["error"])
        self.assertEqual(len(self.checks_file(directory)), lines)

    def test_amend_changes_the_verdict_list_and_pack(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest()["dir"])
        self.ok("pretest", "check", "add", "A 500 shows Retry", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "fail",
                "--note", "500 not handled")
        amended_state = self.ok("pretest", "check", "amend", "chk-1", "--verdict", "blocked",
                                "--note", "the 500 came from the test backend")
        self.assertEqual((amended_state["state"], amended_state["verdict"]),
                         ("ended", "blocked"))
        self.assertEqual(amended_state["note"],
                         "amended from fail; the 500 came from the test backend")
        self.assertEqual(amended_state["amendments"][0]["previous_note"], "500 not handled")
        event = self.checks_file(directory)[-1]
        self.assertEqual({key: event[key] for key in ("event", "check_id", "old_verdict",
                                                      "new_verdict", "note")},
                         {"event": "amended", "check_id": "chk-1", "old_verdict": "fail",
                          "new_verdict": "blocked",
                          "note": "the 500 came from the test backend"})
        self.assertEqual(event["at"], event["ts"])
        checks_mod.validate_event(event)
        listed = self.ok("pretest", "check", "list")["checks"][0]
        self.assertEqual(listed["verdict"], "blocked")
        # `check end` of the ended check still refuses (amend is the way)
        self.refused("no_open_check", "pretest", "check", "end", "chk-1", "--verdict", "pass")
        packed = self.ok("pretest", "pack")
        pack = Path(packed["pack"])
        self.assertEqual(packed["verdict"], "blocked")
        summary = json.loads((pack / "summary.json").read_text(encoding="utf-8"))
        check = next(item for item in summary["checks"] if item["check_id"] == "chk-1")
        self.assertEqual(check["verdict"], "blocked")
        self.assertIn("amended from fail", check["note"])
        self.assertIn("amended from fail", (pack / "summary.md").read_text(encoding="utf-8"))
        self.ok("report", "verify", str(pack))
        self.refused("pretest_finalized", "pretest", "check", "amend", "chk-1",
                     "--verdict", "pass", "--note", "after the pack")

    def test_amend_after_stop_is_no_active_pretest(self) -> None:
        self.start_session()
        self.start_pretest()
        self.ok("pretest", "check", "add", "x", "--kind", "ticket")
        self.ok("pretest", "check", "begin", "chk-1")
        self.ok("pretest", "check", "end", "chk-1", "--verdict", "fail")
        self.ok("pretest", "stop")
        self.refused("no_active_pretest", "pretest", "check", "amend", "chk-1",
                     "--verdict", "pass", "--note", "x")

    def test_race_case_needs_the_observed_count(self) -> None:
        self.start_session()
        directory = Path(self.start_pretest()["dir"])
        self.ok("pretest", "check", "add", "Double tap sends one order", "--kind", "edge",
                "--case", "double-tap")
        self.ok("pretest", "check", "begin", "chk-1")
        lines = len(self.checks_file(directory))
        payload = self.refused("race_needs_count", "pretest", "check", "end", "chk-1",
                               "--verdict", "pass")
        self.assertEqual(payload["hint"], RACE_HINT)
        self.assertEqual((payload["check_id"], payload["case"]), ("chk-1", "double-tap"))
        for bad in ("many", "-1", "1.5", "²", ""):
            with self.subTest(count=bad):
                refused = self.refused("invalid_value", "pretest", "check", "end", "chk-1",
                                       "--verdict", "pass", f"--observed-count={bad}")
                self.assertEqual(refused["flag"], "--observed-count")
        self.assertEqual(len(self.checks_file(directory)), lines)
        self.assertEqual(self.ok("pretest", "status")["open_check"]["check_id"], "chk-1")
        ended = self.ok("pretest", "check", "end", "chk-1", "--verdict", "pass",
                        "--observed-count", "1", "--note", "one POST /orders")
        self.assertEqual(ended["observed_count"], 1)
        self.assertEqual(ended["note"], "observed requests: 1; one POST /orders")
        self.assertEqual(self.checks_file(directory)[-1]["observed_count"], 1)
        # other verdicts and other cases need no count
        self.ok("pretest", "check", "add", "Double tap again", "--kind", "edge",
                "--case", "double-tap")
        self.ok("pretest", "check", "begin", "chk-2")
        self.ok("pretest", "check", "end", "chk-2", "--verdict", "inconclusive",
                "--note", "taps too slow")
        self.ok("pretest", "check", "add", "Offline", "--kind", "edge", "--case", "offline")
        self.ok("pretest", "check", "begin", "chk-3")
        plain = self.ok("pretest", "check", "end", "chk-3", "--verdict", "pass")
        self.assertNotIn("observed_count", plain)
        # amend to pass: the count must be stored or given
        self.refused("race_needs_count", "pretest", "check", "amend", "chk-2",
                     "--verdict", "pass", "--note", "counted in the requests")
        amended_state = self.ok("pretest", "check", "amend", "chk-2", "--verdict", "pass",
                                "--note", "counted in the requests", "--observed-count", "2")
        self.assertEqual(amended_state["observed_count"], 2)
        # chk-1 already holds a count: an amendment to pass needs no new one
        self.ok("pretest", "check", "amend", "chk-1", "--verdict", "fail", "--note", "two")
        self.ok("pretest", "check", "amend", "chk-1", "--verdict", "pass", "--note", "one")
        packed = self.ok("pretest", "pack")
        pack = Path(packed["pack"])
        summary = json.loads((pack / "summary.json").read_text(encoding="utf-8"))
        notes = {item["check_id"]: item["note"] for item in summary["checks"]}
        self.assertTrue(notes["chk-1"].startswith("observed requests: 1"))
        self.assertTrue(notes["chk-2"].startswith("observed requests: 2"))
        markdown = (pack / "summary.md").read_text(encoding="utf-8")
        self.assertIn("observed requests: 1", markdown)
        self.assertIn("observed requests: 2", markdown)
        self.ok("report", "verify", str(pack))

    def test_help_lists_the_new_verb_and_flags(self) -> None:
        text = " ".join(self.cli("pretest", "check", "--help").stdout.split())
        self.assertIn("amend", text)
        for argv in (("pretest", "check", "amend", "--help"),
                     ("pretest", "check", "end", "--help")):
            self.assertIn("--observed-count", self.cli(*argv).stdout)
        amend_help = self.cli("pretest", "check", "amend", "--help").stdout
        for flag in ("--verdict", "--note"):
            self.assertIn(flag, amend_help)


if __name__ == "__main__":
    unittest.main()
