"""Flow tool hardening: Maestro fidelity, fmt comments, compiler confidence,
content-bound Teach approvals, proof selection, and Atlas app scope.

Every test here is hermetic: no device, no backend binary — only temp
directories, a throwaway git repository, and in-process fakes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from autonom_lib import actions, errors, journal, proof, teach  # noqa: E402
from autonom_lib import app_skills  # noqa: E402
from autonom_lib.atlas import fingerprint, graph as atlas_graph  # noqa: E402
from autonom_lib.flow import canonical, compiler, maestro, parser, schema  # noqa: E402
from env_isolation import EnvSandboxMixin  # noqa: E402

_HEAD = "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n---\n"


def _flow(body: str, header: str = _HEAD) -> schema.Flow:
    return schema.build_flow(parser.parse_document(header + body, "t.yaml"))


def _import(text: str) -> schema.Flow:
    return schema.build_flow(parser.parse_document(
        maestro.import_flow(text, "m.yaml"), "imported.yaml"))


# --- FLOW-004: Maestro export is faithful ------------------------------------


class MaestroFidelityTests(unittest.TestCase):
    def test_resume_launch_exports_stop_app_false_and_imports_back(self) -> None:
        out = maestro.export_flow(_flow("- launchApp:\n    resume: true\n"), "t")
        self.assertIn("- launchApp:\n    stopApp: false", out)
        self.assertNotIn("- launchApp\n", out,
                         "a bare launchApp is Maestro's restart, not a resume")
        self.assertTrue(_import(out).steps[0].args.get("resume"))
        # the default (fresh) launch still exports bare and imports fresh
        out = maestro.export_flow(_flow("- launchApp\n"), "t")
        self.assertIn("- launchApp\n", out)
        self.assertNotIn("resume", _import(out).steps[0].args)

    def test_labels_and_scalars_are_quoted_when_yaml_needs_it(self) -> None:
        flow = _flow(
            "- tapOn:\n    selector:\n      text: Promo\n"
            "    label: \"Promo: seen\"\n"
            "- inputText: \"# not a comment\"\n"
            "- openLink:\n    url: \"yes\"\n    label: \"on\"\n",
            header="schema: autonom.dev/flow/v1\nappId: com.example.app\n"
                   "name: \"Checkout: happy path\"\ntags:\n  - \"no\"\n"
                   "env:\n  COUNT: \"1\"\n---\n")
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn("label: 'Promo: seen'", out)
        back = _import(out)  # would raise on `label: Promo: seen`
        self.assertEqual(back.name, "Checkout: happy path")
        self.assertEqual(back.tags, ["no"])
        self.assertEqual(back.env, {"COUNT": "1"})
        self.assertEqual(back.steps[0].args["label"], "Promo: seen")
        self.assertEqual(back.steps[1].args["value"], "# not a comment")
        self.assertEqual(back.steps[2].args["url"], "yes")
        self.assertEqual(back.steps[2].args["label"], "on")

    def test_exact_selector_round_trips_twice_as_exact(self) -> None:
        flow = _flow("- tapOn:\n    selector:\n      visibleText: Sign in\n")
        first = maestro.export_flow(flow, "t.yaml")
        self.assertIn("text: Sign in\n", first)
        second = maestro.export_flow(_import(first), "t.yaml")
        self.assertEqual(first, second)
        selector = _import(second).steps[0].selector
        self.assertEqual(selector.match, "exact")
        self.assertEqual(selector.fields["visible_text"], "Sign in")

    def test_escape_only_patterns_import_as_the_exact_text(self) -> None:
        # what the previous export wrote for exact text
        flow = _import("appId: a.b\n---\n- tapOn:\n    text: Sign\\ in\n"
                       "- tapOn:\n    text: 'Save \\(draft\\)'\n"
                       "- tapOn:\n    text: '\\d+ items'\n")
        self.assertEqual((flow.steps[0].selector.match,
                          flow.steps[0].selector.fields["visible_text"]),
                         ("exact", "Sign in"))
        self.assertEqual(flow.steps[1].selector.fields["visible_text"],
                         "Save (draft)")
        self.assertEqual(flow.steps[1].selector.match, "exact")
        self.assertEqual(flow.steps[2].selector.match, "regex",
                         "a real class escape is still a pattern")

    def test_regex_selectors_do_not_gain_wrappers_every_round(self) -> None:
        source = ("appId: a.b\n---\n- tapOn:\n    text: 'Welcome.*'\n"
                  "- assertVisible:\n    text: '(?:a)|(?:b)'\n")
        imported = maestro.import_flow(source, "m.yaml")
        exported = maestro.export_flow(_import(source), "e.yaml")
        self.assertIn("text: Welcome.*\n", exported,
                      "the importer's own ^(?:...)$ wrapper is stripped")
        again = maestro.import_flow(exported, "m.yaml")
        self.assertEqual(imported, again)
        self.assertEqual(exported,
                         maestro.export_flow(_import(exported), "e.yaml"))
        # an Autonom-native search regex is widened once, then stable
        native = _flow("- tapOn:\n    selector:\n      text: Welc\n"
                       "      match: regex\n")
        first = maestro.export_flow(native, "t.yaml")
        self.assertIn("text: .*(?:Welc).*", first)
        self.assertEqual(first, maestro.export_flow(_import(first), "t.yaml"))

    def test_other_literal_modes_round_trip(self) -> None:
        for mode in ("caseInsensitiveExact", "contains"):
            with self.subTest(mode=mode):
                flow = _flow("- tapOn:\n    selector:\n      text: Sign in\n"
                             f"      match: {mode}\n")
                back = _import(maestro.export_flow(flow, "t.yaml"))
                self.assertEqual(back.steps[0].selector.match, mode)
                self.assertEqual(back.steps[0].selector.fields["visible_text"],
                                 "Sign in")

    def test_mixed_modes_on_one_selector_keep_full_match_meaning(self) -> None:
        flow = _import("appId: a.b\n---\n- tapOn:\n    id: login\n"
                       "    text: 'Welcome.*'\n")
        selector = flow.steps[0].selector
        self.assertEqual(selector.match, "regex")
        self.assertEqual(selector.fields["resource_id"], "^(?:login)$",
                         "an exact id must not turn into a substring search")

    def test_input_text_timeout_refuses_and_require_focus_maps(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.export_flow(_flow("- inputText:\n    value: hi\n"
                                      "    timeoutMs: 500\n"), "t.yaml")
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_FLOW_COMMAND)
        self.assertIn("inputText.timeoutMs", caught.exception.message)
        out = maestro.export_flow(_flow("- inputText:\n    value: hi\n"
                                        "    requireFocus: false\n"), "t.yaml")
        self.assertIn("- inputText: hi", out)

    def test_every_refusal_is_collected_with_its_line(self) -> None:
        flow = _flow("- setOrientation: landscape\n"
                     "- tapOn:\n    selector:\n      text: A\n"
                     "    timeoutMs: 100\n"
                     "- back\n"
                     "- scrollUntilVisible:\n    selector:\n      text: B\n"
                     "- retry:\n    commands:\n      - assertVisible: C\n")
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.export_flow(flow, "t.yaml")
        refusals = caught.exception.extra["refusals"]
        self.assertEqual([item["command"] for item in refusals],
                         ["setOrientation", "scrollUntilVisible", "retry"])
        self.assertEqual([item["line"] for item in refusals], [5, 11, 14])
        self.assertIn("2 more", caught.exception.message)
        self.assertEqual(caught.exception.extra["line"], 5)

    def test_postcondition_refuses_instead_of_dropping(self) -> None:
        flow = _flow("- tapOn:\n    selector:\n      text: Go\n"
                     "    postcondition:\n      id: done\n")
        with self.assertRaises(errors.AutonomError) as caught:
            maestro.export_flow(flow, "t.yaml")
        self.assertIn("postcondition", caught.exception.message)

    def test_hooks_and_properties_export(self) -> None:
        flow = _flow("- back\n", header=(
            "schema: autonom.dev/flow/v1\nappId: com.example.app\nname: t\n"
            "properties:\n  owner: qa\n"
            "onFlowStart:\n  - launchApp\n---\n"))
        out = maestro.export_flow(flow, "t.yaml")
        self.assertIn("onFlowStart:\n  - launchApp\n", out)
        back = _import(out)
        self.assertEqual([s.command for s in back.on_flow_start], ["launchApp"])
        self.assertEqual(back.properties, {"owner": "qa"})


# --- FLOW-008: fmt never silently drops comments ------------------------------


class CommentDetectionTests(unittest.TestCase):
    SOURCE = ("# leading note\n"
              "schema: autonom.dev/flow/v1\nname: t\n---\n"
              "- tapOn:\n    selector:\n      text: \"a # b\"  # trailing\n"
              "- inputText: it's#fine\n"
              "- inputText: 'x # y'\n")

    def test_comments_outside_quotes_are_found(self) -> None:
        found = canonical.find_comments(self.SOURCE)
        self.assertEqual([(item["line"], item["text"]) for item in found],
                         [(1, "# leading note"), (7, "# trailing")])

    def test_canonical_output_would_lose_them(self) -> None:
        flow = schema.build_flow(parser.parse_document(self.SOURCE, "t.yaml"))
        lost = canonical.lost_comments(self.SOURCE, canonical.emit_flow(flow))
        self.assertEqual(len(lost), 2)
        plain = canonical.emit_flow(flow)
        self.assertEqual(canonical.lost_comments(plain, plain), [])


# --- compiler confidence --------------------------------------------------------


class CompilerConfidenceTests(unittest.TestCase):
    def _session(self, directory: str) -> dict:
        return {"session_id": "s1", "artifacts_dir": directory,
                "app_id": "com.example"}

    def test_closing_assertion_selector_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = self._session(directory)
            journal.append(record, {"kind": "action", "verb": "session launch",
                                    "ok": True, "argv": ["session", "launch"],
                                    "result": {}})
            actions.record_detail(record, "find", {
                "kind": "find", "count": 1,
                "selector": {"desc": "Open settings", "mode": "exact",
                             "case_sensitive": True, "index": None}})
            flow, report = compiler.compile_session(record, task="t")
            self.assertEqual(flow.steps[-1].command, "assertVisible")
            self.assertEqual(report["quality"]["selectors"]["recorded"], 1)
            self.assertEqual(report["quality"]["selectors"]["description"], 1)
            self.assertLess(report["confidence"], 1.0)
            self.assertTrue(report["review_required"])

    def test_nothing_proven_is_zero_confidence_and_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = self._session(directory)
            journal.append(record, {"kind": "action", "verb": "session launch",
                                    "ok": True, "argv": ["session", "launch"],
                                    "result": {}})
            _flow, report = compiler.compile_session(record, task="t")
            self.assertEqual(report["confidence"], 0.0)
            self.assertTrue(report["review_required"])


# --- SEC-005: approvals bind flow content ------------------------------------


_TAUGHT = ("schema: autonom.dev/flow/v1\nid: flow_taught\n"
           "appId: com.example.app\nname: taught\n---\n"
           "- assertVisible:\n    selector:\n      text: Settings\n")


class _FakeResult:
    def __init__(self, run_id: str, status: str) -> None:
        self.run_id, self.status, self.failure = run_id, status, None


class _FakeRunner:
    """Stands in for the flow executor: writes a manifest, returns a result."""

    count = 0
    configs: list = []

    def __init__(self, target, session, config) -> None:
        self.session, self.config = session, config
        _FakeRunner.configs.append(config)

    def run(self, flow):
        _FakeRunner.count += 1
        run_id = f"run-{_FakeRunner.count}"
        run_dir = Path(self.session["artifacts_dir"]) / "flows" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "flow_id": flow.flow_id, "status": "passed"}),
            encoding="utf-8")
        return _FakeResult(run_id, "passed")


class TeachBindingTests(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox_home()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".autonom").mkdir()
        self.record = {"session_id": "s1",
                       "artifacts_dir": str(self.root / "artifacts")}
        Path(self.record["artifacts_dir"]).mkdir()
        self.flow = self.root / "taught.yaml"
        self.flow.write_text(_TAUGHT, encoding="utf-8")
        _FakeRunner.count = 0
        _FakeRunner.configs = []

    def _legacy_manifests(self, count: int) -> None:
        for index in range(count):
            run = Path(self.record["artifacts_dir"]) / "flows" / f"old-{index}"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({
                "run_id": f"old-{index}", "flow_id": "flow_taught",
                "status": "passed", "started_at_ms": 1_000_000}),
                encoding="utf-8")

    def _edit(self, text: str, mtime: float) -> None:
        self.flow.write_text(text, encoding="utf-8")
        os.utime(self.flow, (mtime, mtime))

    def test_edit_after_clean_replays_blocks_approval(self) -> None:
        self._legacy_manifests(3)
        os.utime(self.flow, (900, 900))  # written before the runs
        self.assertEqual(len(teach.approve(self.record, self.flow)["clean_replays"]), 3)
        self._edit(_TAUGHT.replace("Settings", "Other"), 2_000)
        with self.assertRaises(errors.AutonomError) as caught:
            teach.approve(self.record, self.flow)
        self.assertEqual(caught.exception.code, errors.TEACH_APPROVAL_BLOCKED)
        self.assertEqual(caught.exception.extra["clean_replays"], 0)
        self.assertEqual(caught.exception.extra["stale_replays"], 1)

    def test_replays_are_bound_by_hash_not_by_timestamps(self) -> None:
        replays = teach.replay(self.record, None, self.flow, runs=3,
                               env={"USER": "qa"}, secrets={"PIN": "1234"},
                               runner_factory=_FakeRunner)
        self.assertEqual([r["status"] for r in replays], ["passed"] * 3)
        self.assertEqual(_FakeRunner.configs[0].env, {"USER": "qa"})
        self.assertEqual(_FakeRunner.configs[0].secrets, {"PIN": "1234"})
        receipt = teach.approve(self.record, self.flow)
        self.assertEqual(receipt["flow_sha256"], teach.file_sha256(self.flow))
        self.assertEqual(receipt["replay_binding"]["sha256"], 3)
        # same-size edit with an old mtime: only the hash can tell
        self._edit(_TAUGHT.replace("Settings", "Settingz"), 1)
        with self.assertRaises(errors.AutonomError) as caught:
            teach.approve(self.record, self.flow)
        self.assertEqual(caught.exception.code, errors.TEACH_APPROVAL_BLOCKED)

    def test_minimum_runs_must_be_positive(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(errors.AutonomError) as caught:
                    teach.approve(self.record, self.flow, minimum_runs=value)
                self.assertEqual(caught.exception.code, errors.USAGE_ERROR)

    def test_promote_refuses_an_edited_approved_flow(self) -> None:
        teach.replay(self.record, None, self.flow, runs=3,
                     runner_factory=_FakeRunner)
        teach.approve(self.record, self.flow)
        promoted = app_skills.promote(self.root, "com.example.app", self.flow)
        self.assertTrue(Path(promoted["promoted"]).is_file())
        self.flow.write_text(_TAUGHT.replace("Settings", "Elsewhere"),
                             encoding="utf-8")
        with self.assertRaises(errors.AutonomError) as caught:
            app_skills.promote(self.root, "com.example.app", self.flow)
        self.assertEqual(caught.exception.code, errors.FLOW_SOURCE_CHANGED)
        # a receipt that never bound content cannot promote either
        receipt = self.flow.with_suffix(".yaml.approved.json")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value.pop("flow_sha256")
        receipt.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(errors.AutonomError) as caught:
            app_skills.promote(self.root, "com.example.app", self.flow)
        self.assertEqual(caught.exception.code, errors.TEACH_APPROVAL_BLOCKED)


# --- FLOW-007: proof follows subflows and new files ---------------------------


_PARENT = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: parent\n"
           "---\n- runFlow: sub/child.yaml\n")
_CHILD = ("schema: autonom.dev/flow/v1\nappId: com.example.app\nname: child\n"
          "---\n- back\n")


class ProofSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.flows = self.repo / ".autonom/flows"
        (self.flows / "sub").mkdir(parents=True)
        (self.flows / "parent.yaml").write_text(_PARENT, encoding="utf-8")
        (self.flows / "sub/child.yaml").write_text(_CHILD, encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src/a.kt").write_text("v1\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True,
                       capture_output=True, text=True)

    def test_changed_subflow_selects_its_parent(self) -> None:
        (self.flows / "sub/child.yaml").write_text(_CHILD + "- back\n",
                                                   encoding="utf-8")
        changed = proof.changed_files(self.repo, "HEAD", None)
        self.assertEqual(changed, [".autonom/flows/sub/child.yaml"])
        suite = proof.select_suite(self.flows, self.repo, changed)
        by_name = {Path(e["path"]).name: e["reasons"] for e in suite["selected"]}
        self.assertIn("parent.yaml", by_name)
        self.assertIn("subflow changed: .autonom/flows/sub/child.yaml",
                      by_name["parent.yaml"])
        self.assertEqual(suite["covered"], [".autonom/flows/sub/child.yaml"])

    def test_untracked_files_are_changes(self) -> None:
        (self.repo / "src/new.kt").write_text("new\n", encoding="utf-8")
        self.assertIn("src/new.kt", proof.changed_files(self.repo, "HEAD", None))

    def test_invalid_flows_are_reported_not_skipped(self) -> None:
        (self.flows / "broken.yaml").write_text("schema: autonom.dev/flow/v1\n"
                                                "name: b\n---\n- tapOnn: X\n",
                                                encoding="utf-8")
        suite = proof.select_suite(self.flows, self.repo, [])
        self.assertEqual([item["flow"] for item in suite["invalid"]],
                         [".autonom/flows/broken.yaml"])
        # the two-value API is unchanged for existing callers
        selected, covered = proof.select_flows(self.flows, self.repo, [])
        self.assertEqual((selected, covered), ([], []))

    def test_pass_with_uncovered_files_is_partial(self) -> None:
        flags = proof.coverage_flags("pass", ["src/a.kt"])
        self.assertTrue(flags["partial"])
        self.assertIn("partial pass", flags["warnings"][0])
        self.assertEqual(proof.coverage_flags("pass", []), {})
        self.assertNotIn("partial", proof.coverage_flags("not_covered",
                                                          ["src/a.kt"]))

    def test_markdown_uses_repo_relative_paths(self) -> None:
        result = {"status": "pass", "base": "HEAD", "head": None,
                  "changed_files": ["src/a.kt"], "changed_areas": ["src/a.kt"],
                  "selected": [{"flow": str(self.flows / "parent.yaml")}],
                  "uncovered_files": ["src/a.kt"],
                  "runs": [{"flow": str(self.flows / "parent.yaml"),
                            "status": "passed"}]}
        markdown = proof.render_markdown(result)
        self.assertIn("`.autonom/flows/parent.yaml`", markdown)
        self.assertNotIn(str(self.repo), markdown)
        self.assertIn("PASS (partial)", markdown)
        self.assertIn("partial pass", markdown)


# --- FLOW-010: Atlas stays inside the app -------------------------------------


def _nodes(package: str, text: str) -> list[dict]:
    return [{"ref": "n1", "role": "text", "depth": 1, "enabled": True,
             "package": package, "text": text,
             "resource_id": f"{package}:id/{text.lower()}"}]


def _step_event(package: str, text: str, when: str, command: str = "tapOn") -> dict:
    return {"kind": "flow.step.finished", "run_id": "run-1",
            "wall_time": when, "timestamp": when,
            "payload": {"command": command, "status": "passed",
                        "screen": fingerprint.fingerprint(_nodes(package, text))}}


class AtlasScopeTests(unittest.TestCase):
    def _graph(self) -> dict:
        return {"schema_version": 1, "app_id": "com.android.settings",
                "screens": {}, "transitions": {}, "updated_at": None}

    def test_foreground_package_is_fingerprinted(self) -> None:
        screen = fingerprint.fingerprint(_nodes("com.android.chrome", "Tabs"))
        self.assertEqual(screen["package"], "com.android.chrome")

    def test_foreign_screens_stay_out_and_no_edge_spans_them(self) -> None:
        graph = self._graph()
        events = [
            _step_event("com.android.settings", "Settings", "2026-09-01T10:00:00.000Z"),
            _step_event("com.android.chrome", "Help", "2026-09-01T10:00:01.000Z",
                        command="back"),
            _step_event("com.android.settings", "About", "2026-09-01T10:00:02.000Z"),
        ]
        atlas_graph.ingest_flow_events(graph, events, "s1")
        packages = {s.get("package") for s in graph["screens"].values()}
        self.assertEqual(packages, {"com.android.settings"})
        self.assertEqual(len(graph["screens"]), 2)
        self.assertEqual(graph["transitions"], {},
                         "an edge must not span the excursion into Chrome")

    def test_last_seen_is_the_observation_time(self) -> None:
        graph = self._graph()
        stamp = "2026-01-02T03:04:05.678Z"
        atlas_graph.ingest_flow_events(
            graph, [_step_event("com.android.settings", "Settings", stamp)], "s1")
        screen = next(iter(graph["screens"].values()))
        self.assertEqual(screen["last_seen"], stamp)
        self.assertEqual(screen["first_seen"], stamp)
        # re-ingesting an older run never moves last_seen backwards
        atlas_graph.ingest_flow_events(
            graph, [_step_event("com.android.settings", "Settings",
                                "2025-12-31T00:00:00.000Z")], "s1")
        self.assertEqual(screen["last_seen"], stamp)
        self.assertEqual(screen["first_seen"], "2025-12-31T00:00:00.000Z")

    def test_manual_details_respect_the_app_scope(self) -> None:
        graph = self._graph()
        details = [{"kind": "tap", "nodes": _nodes("com.android.chrome", "Tabs")},
                   {"kind": "tap", "nodes": _nodes("com.android.settings", "Wi-Fi")}]
        atlas_graph.ingest_action_details(graph, details, "s1")
        self.assertEqual({s.get("package") for s in graph["screens"].values()},
                         {"com.android.settings"})

    def test_diff_refuses_a_file_that_is_not_an_atlas(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            atlas_graph.diff({"hello": "world"}, self._graph())
        self.assertEqual(caught.exception.code, errors.INVALID_VALUE)
        self.assertEqual(atlas_graph.diff(self._graph(), self._graph())
                         ["screens_added"], [])
        with tempfile.TemporaryDirectory() as directory:
            bogus = Path(directory) / "notes.json"
            bogus.write_text("not json", encoding="utf-8")
            with self.assertRaises(errors.AutonomError) as caught:
                atlas_graph.load_snapshot(bogus)
            self.assertEqual(caught.exception.code, errors.INVALID_VALUE)


if __name__ == "__main__":
    unittest.main()
