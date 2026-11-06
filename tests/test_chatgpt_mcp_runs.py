"""Autonom MCP adapter flow runs and artifacts (contract section 4, cases of 4.5).

The server runs as a real subprocess over stdio with the fake CLI
(``tests/fakes/fake_mcp_cli.py`` through ``AUTONOM_CLI``) and
``AUTONOM_MCP_PARTIAL_CATALOG=1``; the session hook case builds the same
server in process because the tool that calls the hook (``stop_session``)
belongs to another module. ``HOME``, ``AUTONOM_HOME`` and ``XDG_STATE_HOME``
are temp dirs for the server and every child; no test touches a device or a
host service. The harness (server process, temp dirs, envelope checks) is the
one of ``tests/test_chatgpt_mcp_core.py``.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_chatgpt_mcp_core as core  # noqa: E402 - the shared adapter harness

FAKE = core.FAKE
IOS_UDID = core.IOS_UDID
OTHER_HOST = core.OTHER_HOST
RUN_ID = r"^run_[0-9a-f]{16}$"
ARTIFACT_ID = r"^art_[0-9a-f]{24}$"
SECRET_VALUE = "alice-very-secret-value"


def import_runs() -> Any:
    core.import_server_modules()
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import mcp_runs
    finally:
        sys.dont_write_bytecode = previous
    return mcp_runs


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not group_alive(pgid):
            return True
        time.sleep(0.05)
    return not group_alive(pgid)


def _tempdir() -> Any:
    return tempfile.TemporaryDirectory(prefix="autonom-mcp-runs-")


def strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in strings(key) + strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in strings(child)]
    return []


# Event lines in the shape the merged executor prints with ``flow run --events``
# (``scripts/autonom_lib/flow/executor.py`` ``writer.emit`` calls and
# ``flow/events.py`` ``EventWriter.emit``), trimmed to the keys the adapter reads
# plus the envelope it ignores. They pin the adapter's progress mapping.
def _event(kind: str, sequence: int, payload: Dict[str, Any]) -> str:
    return json.dumps({
        "schema": "autonom.event/v1", "schema_version": 1, "event_id": "evt_%012x" % sequence,
        "run_id": "fr_1a2b3c4d5e", "attempt_id": "attempt_0123456789abcdef", "step_id": payload.get("step_id"),
        "sequence": sequence, "monotonic_ns": 1000 + sequence, "wall_time": "2026-10-20T10:00:%02d.000Z" % sequence,
        "source": "executor", "redaction": {"applied": True, "sensitive": False, "policy": "pre-persistence"},
        "session_id": "s_0a1b2c3d4e", "flow_id": "login", "timestamp": "2026-10-20T10:00:%02d.000Z" % sequence,
        "kind": kind, "platform": "android", "target_id": "emulator-5584", "sensitive": False,
        "serial": "emulator-5584", "payload": payload})


RECORDED_PASSED = [
    _event("flow.run.started", 1, {"flow": "/flows/login.yaml", "name": "login", "app_id": "com.example",
                                   "tags": [], "steps": 2, "dry_run": False}),
    _event("flow.step.started", 2, {"step_index": 1, "command": "launchApp", "line": 3, "step_id": "st_1"}),
    _event("flow.step.finished", 3, {"step_index": 1, "command": "launchApp", "status": "passed",
                                     "duration_ms": 900, "attempts": 1, "step_id": "st_1"}),
    _event("flow.step.started", 4, {"step_index": 2, "command": "runFlow", "file": "/flows/login.yaml",
                                    "child": "/flows/sub/form.yaml", "step_id": "st_2"}),
    _event("flow.step.started", 5, {"step_index": 3, "command": "tapOn", "line": 2, "step_id": "st_3"}),
    _event("flow.step.finished", 6, {"step_index": 3, "command": "tapOn", "status": "passed", "duration_ms": 120}),
    _event("flow.step.finished", 7, {"step_index": 2, "command": "runFlow", "status": "passed", "duration_ms": 300}),
    _event("flow.run.finished", 8, {"status": "passed", "steps": 3, "failure": None, "replay_target": None}),
]
RECORDED_FAILED = RECORDED_PASSED[:5] + [
    _event("flow.step.finished", 6, {"step_index": 3, "command": "tapOn", "status": "failed",
                                     "error_code": "flow_assertion_timeout", "duration_ms": 5000}),
    _event("flow.step.finished", 7, {"step_index": 2, "command": "runFlow", "status": "failed"}),
    _event("flow.run.finished", 8, {"status": "failed", "steps": 3, "replay_target": None,
                                    "failure": {"step_index": 3, "command": "tapOn", "line": 2,
                                                "flow": "/flows/sub/form.yaml",
                                                "error_code": "flow_assertion_timeout",
                                                "failure_class": "assertion", "error": "Text 'Welcome' never appeared"}}),
]
RECORDED_INTERRUPTED = RECORDED_PASSED[:3] + [
    _event("flow.step.started", 4, {"step_index": 2, "command": "inputText", "line": 5, "step_id": "st_2"}),
    _event("flow.step.refused", 5, {"step_index": 2, "command": "inputText", "error_code": "control_held",
                                    "holder": {"kind": "human", "id": "canvas:7:1", "label": "Canvas"},
                                    "generation": 4}),
    _event("flow.run.finished", 6, {"status": "interrupted", "steps": 2, "replay_target": None,
                                    "failure": {"step_index": 2, "command": "inputText", "error_code": "control_held",
                                                "failure_class": "control_lost", "error": "controlled by another"}}),
]


class RunsCase(core.CoreCase):
    """Core harness plus flow roots, flows and run helpers."""

    def setUp(self) -> None:
        super().setUp()
        self.flows_root = self.tmp / "flows"
        (self.flows_root / "sub").mkdir(parents=True)
        self.write_flow("login.yaml", 3)
        self.write_flow("sub/checkout.yml", 2)

    def write_flow(self, name: str, steps: int) -> Path:
        path = self.flows_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("appId: com.example\n---\n" + "".join("- tapOn: \"Item %d\"\n" % index
                                                               for index in range(steps)), encoding="utf-8")
        return path

    def configure(self, **values: Any) -> None:
        config: Dict[str, Any] = {"flow_roots": [str(self.flows_root)]}
        config.update(values)
        self.write_config(config)

    def flow_id(self, server: core.ServerProcess, name: str = "login.yaml") -> str:
        flows = self.call_ok(server, "list_flows")["flows"]
        return next(item["flow_id"] for item in flows if item["name"] == name)

    def start_run(self, server: core.ServerProcess, host: str, session_id: str,
                  flow: str = "login.yaml", env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"host_id": host, "session_id": session_id, "flow_id": self.flow_id(server, flow)}
        if env is not None:
            arguments["env"] = env
        return self.call_ok(server, "start_flow_run", arguments)

    def get_run(self, server: core.ServerProcess, host: str, run_id: str) -> Dict[str, Any]:
        return self.call_ok(server, "get_run", {"host_id": host, "run_id": run_id})["run"]

    def wait_run(self, server: core.ServerProcess, host: str, run_id: str, done: Any,
                 timeout: float = 30.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(server, host, run_id)
            if done(run):
                return run
            if time.monotonic() > deadline:
                self.fail("run %s never reached the wanted state: %r" % (run_id, run))
            time.sleep(0.1)

    def wait_terminal(self, server: core.ServerProcess, host: str, run_id: str,
                      timeout: float = 30.0) -> Dict[str, Any]:
        return self.wait_run(server, host, run_id, lambda run: run["status"] not in ("pending", "running"),
                             timeout)

    def wait_step(self, server: core.ServerProcess, host: str, run_id: str) -> Dict[str, Any]:
        return self.wait_run(server, host, run_id,
                             lambda run: run["status"] == "running" and run["current_step"] is not None)

    def record(self, run_id: str) -> Dict[str, Any]:
        return json.loads((self.mcp_dir / "runs" / ("%s.json" % run_id)).read_text(encoding="utf-8"))

    def wait_record(self, run_id: str, done: Any, timeout: float = 20.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                record = self.record(run_id)
            except (OSError, ValueError):
                record = None
            if record is not None and done(record):
                return record
            if time.monotonic() > deadline:
                self.fail("record %s never reached the wanted state: %r" % (run_id, record))
            time.sleep(0.1)

    def wait_calls(self, key: str, count: int, timeout: float = 10.0) -> List[Dict[str, Any]]:
        """The CLI log lines of ``key`` once there are ``count`` (a spawned
        child logs itself only after its interpreter started)."""
        deadline = time.monotonic() + timeout
        while True:
            calls = self.cli_calls(key)
            if len(calls) >= count or time.monotonic() > deadline:
                self.assertEqual(len(calls), count, calls)
                return calls
            time.sleep(0.05)

    def in_process_server(self, **env: str) -> Any:
        """The server built in process (its services are reachable) and a
        ``call(name, arguments)`` that checks every result against its schema."""
        modules = core.import_server_modules()
        server = modules["server"].McpServer(self.server_env(**env), self.home, io.BytesIO(), io.BytesIO(),
                                             io.StringIO())
        self.addCleanup(server.shutdown)
        common = modules["common"]

        def call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            structured = server.call_tool(server.tools[name], arguments, threading.Event())["structuredContent"]
            self.assertEqual(common.validate(server.schemas[name], structured), [], structured)
            return structured

        return server, call

    def assert_no_paths(self, structured: Dict[str, Any]) -> None:
        roots = {str(self.tmp), os.path.realpath(str(self.tmp)), str(self.home)}
        for text in strings(structured):
            for root in roots:
                self.assertNotIn(root, text, structured)

    @staticmethod
    def temp_labelled(path: str) -> str:
        """``path`` as free text shows it when it lies in the temp folder (DEC-027)."""
        for root in sorted({tempfile.gettempdir().rstrip("/"), os.path.realpath(tempfile.gettempdir())},
                           key=len, reverse=True):
            if path.startswith(root + "/"):
                return "<tmp>" + path[len(root):]
        return path


# ====================================================================== flows

class FlowListTests(RunsCase):
    def test_no_roots_warns_and_lists_nothing(self) -> None:
        server = self.started()
        structured = self.call_ok(server, "list_flows")
        self.assertEqual(structured["flows"], [])
        self.assertEqual([item["code"] for item in structured["warnings"]], ["no_flow_roots"])
        self.call_error(server, "list_flows", {"host_id": OTHER_HOST}, "host_not_found")

    def test_flow_ids_names_and_the_walk_limits(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "elsewhere.yaml").write_text("- tapOn: x\n", encoding="utf-8")
        os.symlink(str(outside), str(self.flows_root / "linked-dir"))
        os.symlink(str(outside / "elsewhere.yaml"), str(self.flows_root / "linked.yaml"))
        self.write_flow("a/b/c/d/four.yaml", 1)      # 4 levels below the root: listed
        self.write_flow("a/b/c/d/e/five.yaml", 1)    # 5 levels: beyond the walk
        self.write_flow(".maestro/smoke.yaml", 1)    # the usual Maestro folder: listed like any other
        (self.flows_root / "notes.md").write_text("x", encoding="utf-8")
        self.configure(flow_roots=[str(self.flows_root), str(self.tmp / "missing-root")])
        server = self.started()
        structured = self.call_ok(server, "list_flows", {"host_id": self.host_id(server)})
        names = sorted(item["name"] for item in structured["flows"])
        self.assertEqual(names, [".maestro/smoke.yaml", "a/b/c/d/four.yaml", "login.yaml", "sub/checkout.yml"])
        for item in structured["flows"]:
            real = os.path.realpath(str(self.flows_root / item["name"]))
            self.assertEqual(item["flow_id"], "fl_" + hashlib.sha256(real.encode("utf-8")).hexdigest()[:16])
            self.assertEqual(item["root"], "flows")
        # A missing root is a warning naming its folder name only, never its path.
        self.assertEqual(structured["warnings"], [{"code": "flow_root_unavailable",
                                                   "message": "Flow root missing-root is not a directory"}])
        self.assert_no_paths(structured)
        again = self.call_ok(server, "list_flows")["flows"]
        self.assertEqual(again, structured["flows"])

    def test_a_flow_in_the_maestro_folder_runs_by_id(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.write_flow(".maestro/smoke.yaml", 2)
        self.configure()
        server = self.started()
        host = self.host_id(server)
        run = self.start_run(server, host, session_id, flow=".maestro/smoke.yaml")["run"]
        self.assertEqual(run["flow_name"], ".maestro/smoke.yaml")
        final = self.wait_terminal(server, host, run["run_id"])
        self.assertEqual(final["status"], "passed", final)
        argv = self.wait_calls("flow_run", 1)[0]["argv"]
        self.assertEqual(argv[4], os.path.realpath(str(self.flows_root / ".maestro" / "smoke.yaml")))


# ====================================================================== event mapping

class EventMappingTests(unittest.TestCase):
    def progress(self, lines: List[str]) -> Any:
        runs = import_runs()
        progress = runs.Progress()
        for line in lines:
            progress.feed(json.loads(line))
        return runs, progress

    def test_recorded_events_map_to_progress_and_the_final_status(self) -> None:
        runs, progress = self.progress(RECORDED_PASSED[:5])
        self.assertEqual(progress.steps(), {"total": 2, "done": 1, "failed": 0})
        self.assertEqual(progress.current(), {"index": 3, "command": "tapOn",
                                              "started_at": "2026-10-20T10:00:05.000Z"})
        self.assertEqual(progress.cli_run_id, "fr_1a2b3c4d5e")
        runs, progress = self.progress(RECORDED_PASSED)
        self.assertEqual(progress.steps(), {"total": 3, "done": 3, "failed": 0})
        self.assertIsNone(progress.current())
        self.assertEqual(runs.final_status(progress, 0, None, None), ("passed", None, None))

        runs, progress = self.progress(RECORDED_FAILED)
        self.assertEqual(progress.steps(), {"total": 3, "done": 3, "failed": 2})
        status, code, error = runs.final_status(progress, 1, None, None)
        self.assertEqual((status, code), ("failed", "flow_assertion_timeout"))
        self.assertEqual(error, "The flow ended failed at step 3 (tapOn): flow_assertion_timeout")
        self.assertNotIn("Welcome", error)  # failure text from the flow never passes through

        runs, progress = self.progress(RECORDED_INTERRUPTED)
        self.assertEqual(runs.final_status(progress, 1, None, None)[:2], ("interrupted", "control_held"))
        self.assertIsNone(progress.current())

    def test_exit_codes_cancel_requests_and_cli_errors(self) -> None:
        runs, started = self.progress(RECORDED_PASSED[:2])
        self.assertEqual(runs.final_status(started, 130, "cancel_requested", None)[0], "cancelled")
        self.assertEqual(runs.final_status(started, -9, "cancel_requested", None)[0], "cancelled")
        self.assertEqual(runs.final_status(started, 130, None, None)[0], "cancelled")
        # A run that reported its end before the cancel took effect keeps that end.
        runs, finished = self.progress(RECORDED_PASSED)
        self.assertEqual(runs.final_status(finished, 0, "cancel_requested", None)[0], "passed")
        refused = {"ok": False, "error_code": "flow_parse_error", "error": "line 3: bad indentation"}
        self.assertEqual(runs.final_status(runs.Progress(), 2, None, refused),
                         ("failed", "flow_parse_error", "line 3: bad indentation"))
        self.assertEqual(runs.final_status(started, 1, None, None)[0], "outcome_unknown")
        self.assertEqual(runs.final_status(started, -11, None, None)[0], "outcome_unknown")
        self.assertEqual(runs.final_status(runs.Progress(), 0, None, None)[0], "passed")

    def test_a_retry_notice_and_an_unmatched_finish_keep_the_open_steps(self) -> None:
        # executor.py _execute_retry with allowMutations: the block's finish is
        # emitted twice, first as a ``warning`` notice, then at its real end.
        lines = [
            _event("flow.run.started", 1, {"steps": 2}),
            _event("flow.step.started", 2, {"step_index": 1, "command": "group"}),
            _event("flow.step.started", 3, {"step_index": 2, "command": "retry"}),
            _event("flow.step.finished", 4, {"step_index": 2, "command": "retry", "status": "warning",
                                             "warning": "allowMutations: may act more than once"}),
            _event("flow.step.started", 5, {"step_index": 3, "command": "tapOn"}),
            _event("flow.step.finished", 6, {"step_index": 3, "command": "tapOn", "status": "passed"}),
        ]
        runs, progress = self.progress(lines)
        self.assertEqual(progress.current()["command"], "retry")
        self.assertEqual(progress.steps(), {"total": 2, "done": 1, "failed": 0})
        progress.feed(json.loads(_event("flow.step.finished", 7, {"step_index": 2, "command": "retry",
                                                                  "status": "passed"})))
        self.assertEqual(progress.current()["command"], "group")
        self.assertEqual(progress.steps(), {"total": 2, "done": 2, "failed": 0})
        # A finish of no open step neither ends the enclosing step nor counts.
        progress.feed(json.loads(_event("flow.step.finished", 8, {"step_index": 9, "command": "tapOn",
                                                                  "status": "failed"})))
        self.assertEqual(progress.current()["command"], "group")
        self.assertEqual(progress.steps(), {"total": 2, "done": 2, "failed": 0})
        progress.feed(json.loads(_event("flow.step.finished", 9, {"step_index": 1, "command": "group",
                                                                  "status": "passed"})))
        self.assertIsNone(progress.current())
        self.assertEqual(progress.steps(), {"total": 3, "done": 3, "failed": 0})

    def test_known_root_stops_at_a_path_boundary(self) -> None:
        # A root that prefixes a sibling folder must not eat part of it: the
        # other folder falls back to the generic /Users/<user> rule.
        runs = import_runs()
        scrubber = runs.RootScrubber([("/Users/al", "~"), ("/tmp", "<tmp>")])
        self.assertEqual(scrubber.sub("open /Users/alice/Documents/x.png"), "open /Users/<user>/Documents/x.png")
        self.assertEqual(scrubber.sub("/Users/al/x and /Users/al"), "~/x and ~")
        self.assertEqual(scrubber.sub("/tmpfoo/a /tmp.d/b /tmp/c"), "/tmpfoo/a /tmp.d/b <tmp>/c")
        common = core.import_server_modules()["common"]
        services = SimpleNamespace(scrub=common.PathScrubber([("/Users/al", "~")]), env={}, config={})
        self.assertEqual(runs.scrub_text(services, "see /Users/alice/Documents/x.png and /Users/al/y"),
                         "see /Users/<user>/Documents/x.png and ~/y")

    def test_free_text_replaces_every_known_root_and_nothing_else(self) -> None:
        # DEC-027: every occurrence of a known local root becomes its label,
        # wherever it is (glued to a flag, after file://, '>' or '-', a '//'
        # start, after a redacted value); other absolute paths and base64 text
        # stay as they are.
        runs = import_runs()
        common = core.import_server_modules()["common"]
        services = SimpleNamespace(
            scrub=common.PathScrubber([("/Users/alice", "~"), ("/Users/alice/.local/state/autonom", "<state>")]),
            env={"TMPDIR": "/var/folders/ab/cdef0123/T/"}, config={"flow_roots": ["/srv/app/flows"]})
        cases = {
            "/Users/alice/flows/bad.yaml:3:3: 'notACommand' is outside": "~/flows/bad.yaml:3:3: 'notACommand' is outside",
            "<state>/runs and /Users/alice/.local/state/autonom/runs/r.json": "<state>/runs and <state>/runs/r.json",
            "/opt/flows/login.yaml: not UTF-8": "/opt/flows/login.yaml: not UTF-8",
            "cannot read '/etc/hosts' (see /tmp/x/)": "cannot read '/etc/hosts' (see <tmp>/x/)",
            "see https://example.com/docs/flow and 1/2 and/or ./a/b": "see https://example.com/docs/flow and 1/2 and/or ./a/b",
            "bearer abc.def /Users/bob/f.yaml": "bearer <redacted> /Users/<user>/f.yaml",
            "<code>/opt/x</code> file:///tmp/y -o/private/tmp/z --out=/tmp/w": (
                "<code>/opt/x</code> file://<tmp>/y -o<tmp>/z --out=<tmp>/w"),
            ">/var/folders/ab/cdef0123/T/a.log -/private/var/folders/zz/qq/T/b //tmp/c /var/folders/zz/qq/C/d": (
                "><tmp>/a.log -<tmp>/b /<tmp>/c <tmp>/d"),
            "/srv/app/flows/sub/f.yaml and /Volumes/Work/x": "<flow-root>/sub/f.yaml and /Volumes/Work/x",
            "data:image/png;base64,iVBORw0KGgo/AAAA+/x/9j==": "data:image/png;base64,iVBORw0KGgo/AAAA+/x/9j==",
            "/Users/<user>/x <tmp> ~/y": "/Users/<user>/x <tmp> ~/y",
        }
        for text, expected in cases.items():
            self.assertEqual(runs.scrub_text(services, text), expected, text)
        # A redacted value is no path label: a root right after it is still replaced.
        self.assertEqual(runs.scrub_text(services, "KEY=hunter22/Users/bob/x", values=["hunter22"]),
                         "KEY=<redacted>/Users/<user>/x")
        # A value that holds a root is redacted as it reads after the roots.
        self.assertEqual(runs.scrub_text(services, "pw /Users/alice/pw123 ok", values=["/Users/alice/pw123"]),
                         "pw <redacted> ok")
        # A value that is a bare root never redacts the label.
        self.assertEqual(runs.scrub_text(services, "in /tmp/a", values=["/tmp"]), "in <tmp>/a")
        self.assertEqual(runs.scrub_text(services, "/srv/app/flows/x.yaml failed with hunter22",
                                         [("/srv/app/flows/x.yaml", "x.yaml")], ["hunter22"]),
                         "x.yaml failed with <redacted>")
        # The longest root wins; the resolved form of a root is a root too.
        self.assertEqual(runs.scrub_text(SimpleNamespace(env={}), "/tmp/a %s/b" % os.path.realpath("/tmp")),
                         "<tmp>/a <tmp>/b")

    def test_record_view_survives_a_damaged_record(self) -> None:
        runs = import_runs()
        common = core.import_server_modules()["common"]
        view = runs.record_view({"run_id": "run_0123456789abcdef", "host_id": "h_" + "a" * 26,
                                 "session_id": "s_abc", "flow_id": "fl_0123456789abcdef", "status": "weird",
                                 "steps": {"total": -1, "done": "x"}, "current_step": {"command": "tap On x"},
                                 "report_artifacts": ["../etc", "art_" + "0" * 24], "env_keys": ["A", 7, "b c"],
                                 "error_code": "bad code!", "operation_id": "op_0123456789abcdef",
                                 "pid": 123, "holder_id": "chatgpt-agent:x", "generation": True})
        self.assertEqual(view["status"], "outcome_unknown")
        self.assertEqual(view["steps"], {"total": 0, "done": 0, "failed": 0})
        self.assertEqual(view["current_step"], {"index": None, "command": None, "started_at": None})
        self.assertEqual(view["report_artifacts"], ["art_" + "0" * 24])
        self.assertEqual(view["env_keys"], ["A"])
        self.assertIsNone(view["error_code"])
        self.assertIsNone(view["generation"])
        self.assertNotIn("pid", view)
        self.assertNotIn("holder_id", view)
        self.assertEqual(common.validate(runs.RUN_VIEW, view), [])

    def test_a_late_progress_write_never_replaces_the_terminal_record(self) -> None:
        runs = import_runs()
        written: List[str] = []
        first_started = threading.Event()
        release_first = threading.Event()

        class Store:
            runs_dir = Path("/nonexistent")
            services = type("Services", (), {"config": {"run_max_s": 60}})()

            def write_record(self, record: Dict[str, Any]) -> None:
                if not written and not first_started.is_set():
                    first_started.set()
                    release_first.wait(5)  # a slow disk under the progress write
                written.append(record["status"])

        job = runs.Job(Store(), {"run_id": "run_" + "0" * 16, "session_id": "s_abc", "status": "running"},
                       {}, "/flows/x.yaml", {})
        progress = threading.Thread(target=job.write, kwargs={"force": True})
        progress.start()
        self.assertTrue(first_started.wait(5))

        def finish() -> None:
            with job.lock:
                job.record["status"] = "passed"
            job.write(force=True)

        terminal = threading.Thread(target=finish)
        terminal.start()
        time.sleep(0.2)  # the terminal write is waiting behind the progress write
        release_first.set()
        progress.join(5)
        terminal.join(5)
        self.assertEqual(written, ["running", "passed"])

    def test_a_throttled_progress_write_is_flushed_by_the_monitor_tick(self) -> None:
        runs = import_runs()
        written: List[Any] = []

        class Store:
            runs_dir = Path("/nonexistent")
            services = type("Services", (), {"config": {"run_max_s": 60}})()

            def write_record(self, record: Dict[str, Any]) -> None:
                written.append(record["current_step"])

        job = runs.Job(Store(), {"run_id": "run_" + "0" * 16, "session_id": "s_abc", "status": "running",
                                 "current_step": None}, {}, "/flows/x.yaml", {})
        job.write(force=True)  # pending -> running
        with job.lock:
            job.record["current_step"] = {"index": 0, "command": "tapOn", "started_at": "t"}
        job.write()  # the step started inside the 0.5 s window: throttled
        self.assertEqual(written, [None])
        self.assertTrue(job.write_pending)

        class Proc:
            pid = 999999
            ticks = 0

            def wait(self, timeout: Optional[float] = None) -> int:
                Proc.ticks += 1
                if written[-1] is None and Proc.ticks < 50:
                    time.sleep(timeout or 0)
                    raise subprocess.TimeoutExpired("flow", timeout or 0)
                return 0

        began = time.monotonic()
        job._supervise(Proc())  # the monitor loop: its tick writes the pending step
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(len(written), 2, written)
        self.assertEqual(written[-1]["command"], "tapOn")
        self.assertFalse(job.write_pending)

    def test_process_start_time_and_owner_check_ignore_the_servers_time_zone(self) -> None:
        # Two servers with different TZ must read one pid's start time alike,
        # or the second calls a live owner gone and stops its flow (contract 4.3).
        child = (
            "import json, sys, types\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import mcp_runs\n"
            "pid = int(sys.argv[2])\n"
            "alive, started = mcp_runs.process_info(pid)\n"
            "me = types.SimpleNamespace(server_pid=-1, server_started_at=None)\n"
            "def owner(stored):\n"
            "    return mcp_runs.RunStore.owner_alive(me, {'server_pid': pid, 'server_started_at': stored})\n"
            "print(json.dumps({'alive': alive, 'started': started,\n"
            "                  'owner': [owner(value) for value in sys.argv[3:]]}))\n")
        with _tempdir() as tmp:
            home = Path(tmp)
            base = {key: value for key, value in os.environ.items()
                    if not key.startswith(("AUTONOM_", "FAKE_MCP_", "PYTHON")) and key not in ("TZ", "XDG_STATE_HOME")}
            base.update({"HOME": str(home), "AUTONOM_HOME": str(home / "autonom"),
                         "XDG_STATE_HOME": str(home / "xdg"), "PYTHONDONTWRITEBYTECODE": "1"})
            before = time.time()
            target = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
                                      env=base, stdin=subprocess.DEVNULL)
            self.addCleanup(target.wait)
            self.addCleanup(target.kill)
            after = time.time()

            def probe(zone: Optional[str], *stored: str) -> Dict[str, Any]:
                env = dict(base)
                if zone is not None:
                    env["TZ"] = zone
                out = subprocess.run([sys.executable, "-I", "-B", "-c", child, str(core.SERVER_DIR),
                                      str(target.pid)] + list(stored), env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, timeout=30, check=True)
                return json.loads(out.stdout.decode("utf-8"))

            zones = ["UTC", "Europe/Amsterdam", None, "Pacific/Kiritimati"]
            first = probe("UTC")
            self.assertTrue(first["alive"])
            started = first["started"]
            self.assertRegex(started, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            epoch = calendar.timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ"))
            self.assertGreaterEqual(epoch, int(before) - 2)
            self.assertLessEqual(epoch, after + 2)
            wrong = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch - 7200))
            for zone in zones:
                seen = probe(zone, started, wrong)
                self.assertEqual((seen["alive"], seen["started"]), (True, started), zone)
                self.assertEqual(seen["owner"], [True, False], zone)


class RecoveryLockTests(unittest.TestCase):
    """Two servers recovering one gone server's run (contract 4.1, 4.3)."""

    @staticmethod
    def gone_record(run_id: str) -> Dict[str, Any]:
        """An active record whose owning server (a reaped child) is gone."""
        gone = subprocess.Popen([sys.executable, "-I", "-B", "-c", "pass"], stdin=subprocess.DEVNULL)
        gone.wait(timeout=30)
        return {
            "schema": 1, "run_id": run_id, "host_id": "h_" + "a" * 26, "session_id": "s_abc",
            "flow_id": "fl_" + "1" * 16, "flow_name": "login.yaml", "status": "running",
            "created_at": "2026-10-20T10:00:00.000Z", "started_at": "2026-10-20T10:00:00.000Z",
            "ended_at": None, "pid": None, "server_pid": gone.pid, "server_started_at": "2026-10-20T09:59:59Z",
            "operation_id": "op_" + "2" * 16, "holder_id": None, "generation": None, "env_keys": [],
            "steps": {"total": 3, "done": 1, "failed": 0},
            "current_step": {"index": 1, "command": "tapOn", "started_at": "2026-10-20T10:00:01.000Z"},
            "exit_code": None, "error_code": None, "error": None, "reason": None, "report_artifacts": [],
            "events_dropped": 0, "forced": False, "last_step_outcome": None}

    def test_two_stores_recovering_one_run_write_its_terminal_record_once(self) -> None:
        runs = import_runs()
        with _tempdir() as tmp:
            logs: List[str] = []
            services = SimpleNamespace(state_dir=str(Path(tmp) / "mcp"), log=logs.append, config={})
            run_id = "run_" + "ab" * 8
            record = self.gone_record(run_id)
            stores = [runs.RunStore(services), runs.RunStore(services)]
            stores[0].write_record(record)
            # Both stores pass their first checks before either reloads: only
            # the cross-process record lock can keep the second from writing.
            barrier = threading.Barrier(2, timeout=10)
            writes: List[str] = []
            for store in stores:
                store.orphan_of = lambda _record: (barrier.wait(), None)[1]  # type: ignore[assignment]
                real = store.write_record

                def counted(item: Dict[str, Any], real: Any = real) -> None:
                    writes.append(item["status"])
                    time.sleep(0.2)  # a slow disk keeps the window open
                    real(item)

                store.write_record = counted  # type: ignore[assignment]
            results: List[Any] = [None, None]

            def recover(index: int) -> None:
                results[index] = stores[index].recover_run(run_id)

            threads = [threading.Thread(target=recover, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
            self.assertEqual(writes, ["outcome_unknown"], results)
            outcomes = sorted(results, key=lambda item: item[0])
            self.assertEqual([item[0] for item in outcomes], ["ended", "recovered"], results)
            ended, recovered = outcomes[0][1], outcomes[1][1]
            self.assertEqual((recovered["status"], recovered["reason"]), ("outcome_unknown", "server_restarted"))
            self.assertEqual(recovered["last_step_outcome"], "unknown")
            self.assertEqual(ended["ended_at"], recovered["ended_at"])
            on_disk = json.loads((Path(services.state_dir) / "runs" / ("%s.json" % run_id)).read_text("utf-8"))
            self.assertEqual(on_disk["ended_at"], recovered["ended_at"])
            lock = Path(services.state_dir) / "runs" / ("%s.lock" % run_id)
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            # A later recovery (or a cancel) reads the end back: run_not_running.
            self.assertEqual(stores[1].recover_run(run_id), ("ended", on_disk))
            self.assertEqual(writes, ["outcome_unknown"])

    def test_a_lock_held_elsewhere_leaves_the_record_active(self) -> None:
        runs = import_runs()
        import fcntl
        with _tempdir() as tmp:
            logs: List[str] = []
            services = SimpleNamespace(state_dir=str(Path(tmp) / "mcp"), log=logs.append, config={})
            store = runs.RunStore(services)
            run_id = "run_" + "cd" * 8
            store.write_record(self.gone_record(run_id))
            holder = os.open(str(store.runs_dir / ("%s.lock" % run_id)), os.O_RDWR | os.O_CREAT, 0o600)
            self.addCleanup(os.close, holder)
            fcntl.flock(holder, fcntl.LOCK_EX)
            previous = runs.RECOVERY_LOCK_WAIT_S
            runs.RECOVERY_LOCK_WAIT_S = 0.3
            self.addCleanup(setattr, runs, "RECOVERY_LOCK_WAIT_S", previous)
            self.assertEqual(store._claim_record(run_id), (False, None))
            outcome, seen = store.recover_run(run_id)  # cancel_run answers cancel_unconfirmed
            self.assertEqual((outcome, seen["status"]), ("still_running", "running"))
            self.assertEqual(store.load(run_id)["status"], "running")
            self.assertTrue(any("recovered by another server" in line for line in logs), logs)
            fcntl.flock(holder, fcntl.LOCK_UN)
            outcome, seen = store.recover_run(run_id)
            self.assertEqual((outcome, seen["status"]), ("recovered", "outcome_unknown"))
            self.assertEqual(store.load(run_id)["status"], "outcome_unknown")


# ====================================================================== runs

class RunTests(RunsCase):
    def test_start_returns_fast_progress_advances_and_the_run_passes(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        server = self.started(FAKE_MCP_STEP_MS="400")
        host = self.host_id(server)
        flow = self.flow_id(server)
        began = time.monotonic()
        structured = self.call_ok(server, "start_flow_run", {
            "host_id": host, "session_id": session_id, "flow_id": flow, "env": {"USER_NAME": SECRET_VALUE}})
        self.assertLess(time.monotonic() - began, 2.0)
        run = structured["run"]
        self.assertRegex(run["run_id"], RUN_ID)
        self.assertIn(run["status"], ("pending", "running"))
        self.assertEqual(run["operation_id"], structured["operation_id"])
        self.assertEqual(structured["session"], {"host_id": host, "session_id": session_id})
        self.assertEqual(run["env_keys"], ["USER_NAME"])
        self.assertEqual((run["flow_id"], run["flow_name"]), (flow, "login.yaml"))
        running = self.wait_step(server, host, run["run_id"])
        self.assertEqual(running["current_step"]["command"], "tapOn")
        self.assertEqual(running["steps"]["total"], 3)
        self.wait_run(server, host, run["run_id"], lambda item: item["steps"]["done"] >= 1)
        final = self.wait_terminal(server, host, run["run_id"])
        self.assertEqual(final["status"], "passed", final)
        self.assertEqual(final["steps"], {"total": 3, "done": 3, "failed": 0})
        self.assertEqual(final["exit_code"], 0)
        self.assertIsNone(final["current_step"])
        self.assertTrue(final["ended_at"])
        # One spawn with the claim, the operation id before the path, the env pair.
        calls = self.wait_calls("flow_run", 1)
        argv = calls[0]["argv"]
        real = os.path.realpath(str(self.flows_root / "login.yaml"))
        self.assertEqual(argv[:5], ["flow", "run", "--operation-id", run["operation_id"], real])
        self.assertEqual(argv[5:], ["--session-id", session_id, "--events", "--env", "USER_NAME=" + SECRET_VALUE])
        self.assertEqual(calls[0]["claim"], {"AUTONOM_CONTROL_HOLDER": "chatgpt-agent:" + host,
                                             "AUTONOM_CONTROL_GENERATION": str(final["generation"])})
        self.assertEqual(len(self.cli_calls("control_acquire")), 1)
        # The report the final event names is an artifact of the session.
        self.assertEqual(len(final["report_artifacts"]), 1)
        report = self.call_ok(server, "get_artifact", {"host_id": host, "session_id": session_id,
                                                       "artifact_id": final["report_artifacts"][0]})
        self.assertEqual((report["artifact"]["kind"], report["artifact"]["name"]), ("report", "report.html"))
        self.assertIn("fake report", report["artifact"]["text"])
        # Durable, private record and events; the env value is nowhere.
        record_path = self.mcp_dir / "runs" / ("%s.json" % run["run_id"])
        events_path = self.mcp_dir / "runs" / ("%s.events.ndjson" % run["run_id"])
        self.assert_mode(record_path, 0o600)
        self.assert_mode(events_path, 0o600)
        self.assert_mode(self.mcp_dir / "runs", 0o700)
        record = self.record(run["run_id"])
        self.assertEqual(record["schema"], 1)
        self.assertEqual(record["server_pid"], server.proc.pid)
        self.assertTrue(record["server_started_at"])
        self.assertEqual(record["holder_id"], "chatgpt-agent:" + host)
        kinds = [json.loads(line)["kind"] for line in events_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kinds[0], "flow.run.started")
        self.assertEqual(kinds[-1], "flow.run.finished")
        self.assertEqual(final["events_dropped"], 0)
        self.assertNotIn(SECRET_VALUE, record_path.read_text(encoding="utf-8"))
        self.assertNotIn(SECRET_VALUE, server.stderr_text())
        for item in (structured, running, final, report):
            self.assertNotIn(SECRET_VALUE, json.dumps(item))
            self.assert_no_paths(item)
        # The guard was released: a mutation on the session runs again.
        self.call_ok(server, "test_slow_mutation", {"host_id": host, "session_id": session_id})

    def test_failed_and_interrupted_runs(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        server = self.started(FAKE_MCP_FLOW_STATUS="failed")
        host = self.host_id(server)
        run = self.start_run(server, host, session_id)["run"]
        final = self.wait_terminal(server, host, run["run_id"])
        self.assertEqual((final["status"], final["error_code"], final["exit_code"]),
                         ("failed", "flow_assertion_timeout", 1))
        self.assertEqual(final["steps"], {"total": 3, "done": 3, "failed": 1})
        self.assertEqual(self.wait_record(run["run_id"], lambda item: item["status"] != "running")["status"],
                         "failed")

        other = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        slow = self.started(FAKE_MCP_STEP_MS="700")
        host = self.host_id(slow)
        run = self.start_run(slow, host, other)["run"]
        self.wait_step(slow, host, run["run_id"])
        # A person takes the device over: the next step is refused by the lease.
        FAKE.set_lease(self.fake_state, "ios", IOS_UDID, generation=40,
                       holder={"kind": "human", "id": "canvas:9:9", "label": "Canvas"})
        final = self.wait_terminal(slow, host, run["run_id"])
        self.assertEqual((final["status"], final["error_code"]), ("interrupted", "control_held"), final)
        self.assertEqual(final["exit_code"], 1)
        self.assertEqual(self.cli_calls("control_acquire")[-1]["argv"].count("--takeover"), 0)

    def test_a_cli_error_before_the_flow_runs_names_no_local_path(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        real = os.path.realpath(str(self.flows_root / "login.yaml"))
        elsewhere = str(self.tmp / "elsewhere" / "a.yaml")
        message = "%s: not UTF-8; see %s and %s, /Users/someone/b.yaml or /opt/c.yaml (%s)" % (
            real, self.home / "notes.txt", elsewhere, SECRET_VALUE)
        server = self.started(FAKE_MCP_CLI_MODE="fail:flow_run:flow_parse_error", FAKE_MCP_FAIL_ERROR=message)
        host = self.host_id(server)
        started = self.start_run(server, host, session_id, env={"USER_NAME": SECRET_VALUE})
        final = self.wait_terminal(server, host, started["run"]["run_id"])
        self.assertEqual((final["status"], final["error_code"], final["exit_code"]),
                         ("failed", "flow_parse_error", 2), final)
        # The known roots become labels (DEC-027); /opt is no known root and stays.
        self.assertEqual(final["error"], "login.yaml: not UTF-8; see ~/notes.txt and %s, /Users/<user>/b.yaml "
                                         "or /opt/c.yaml (<redacted>)" % self.temp_labelled(elsewhere))
        record = self.record(final["run_id"])
        self.assertEqual(record["error"], final["error"])
        cancelled = self.call_error(server, "cancel_run", {"host_id": host, "run_id": final["run_id"]},
                                    "run_not_running")
        for item in (started, final, record, cancelled):
            self.assert_no_paths(item)
            self.assertNotIn(SECRET_VALUE, json.dumps(item))
            for text in strings(item):
                self.assertNotRegex(text, r"/Users/(?!<user>)|/var/folders|/private/", text)

    def test_cancel_interrupts_the_process_group(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        server = self.started(FAKE_MCP_STEP_MS="30000")
        host = self.host_id(server)
        run = self.start_run(server, host, session_id)["run"]
        self.wait_step(server, host, run["run_id"])
        pid = self.record(run["run_id"])["pid"]
        self.assertTrue(group_alive(pid))
        began = time.monotonic()
        structured = self.call_ok(server, "cancel_run", {"host_id": host, "run_id": run["run_id"]})
        self.assertLess(time.monotonic() - began, 12.0)
        cancelled = structured["run"]
        self.assertEqual(cancelled["status"], "cancelled", cancelled)
        self.assertEqual(cancelled["exit_code"], 130)
        self.assertIs(cancelled["forced"], False)
        self.assertEqual(cancelled["last_step_outcome"], "unknown")
        self.assertEqual(cancelled["reason"], "cancel_requested")
        self.assertTrue(wait_group_gone(pid, 2.0))
        self.assertEqual(self.record(run["run_id"])["status"], "cancelled")
        again = self.call_error(server, "cancel_run", {"host_id": host, "run_id": run["run_id"]}, "run_not_running")
        self.assertEqual(again["run"]["status"], "cancelled")
        self.call_error(server, "get_run", {"host_id": host, "run_id": "run_" + "0" * 16}, "run_not_found")
        # A damaged or foreign record reads as missing.
        for damaged in ({"schema": 1, "run_id": "run_" + "1" * 16, "host_id": host, "session_id": "bad id"},
                        dict(self.record(run["run_id"]), run_id="run_" + "2" * 16, host_id=OTHER_HOST)):
            (self.mcp_dir / "runs" / ("%s.json" % damaged["run_id"])).write_text(json.dumps(damaged),
                                                                               encoding="utf-8")
            self.call_error(server, "get_run", {"host_id": host, "run_id": damaged["run_id"]}, "run_not_found")
            self.call_error(server, "cancel_run", {"host_id": host, "run_id": damaged["run_id"]}, "run_not_found")
        self.call_error(server, "get_run", {"host_id": OTHER_HOST, "run_id": run["run_id"]}, "host_not_found")
        # The session is free again.
        self.call_ok(server, "test_slow_mutation", {"host_id": host, "session_id": session_id})

    def test_cancel_forces_a_flow_that_ignores_sigint(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        server = self.started(FAKE_MCP_STEP_MS="30000", FAKE_MCP_FLOW_IGNORE_SIGINT="1")
        host = self.host_id(server)
        run = self.start_run(server, host, session_id)["run"]
        self.wait_step(server, host, run["run_id"])
        pid = self.record(run["run_id"])["pid"]
        began = time.monotonic()
        cancelled = self.call_ok(server, "cancel_run", {"host_id": host, "run_id": run["run_id"]})["run"]
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, 9.5)
        self.assertLess(elapsed, 12.0)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIs(cancelled["forced"], True)
        self.assertEqual(cancelled["last_step_outcome"], "unknown")
        self.assertEqual(cancelled["exit_code"], -signal.SIGKILL)
        self.assertTrue(wait_group_gone(pid, 2.0))

    def test_refusals_run_limit_and_session_busy(self) -> None:
        first = FAKE.add_session(self.fake_state)
        second = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        third = FAKE.add_session(self.fake_state, "android", "emulator-5586")
        stopped = FAKE.add_session(self.fake_state, "android", "emulator-5588", live=False)
        self.configure(max_runs=2)
        server = self.started(FAKE_MCP_STEP_MS="30000")
        host = self.host_id(server)
        flow = self.flow_id(server)
        base = {"host_id": host, "flow_id": flow}
        for env, field in (({"lower": "x"}, "env"), ({"OK": "bad\nvalue"}, "env.OK"),
                           ({"K%d" % index: "v" for index in range(17)}, "env"),
                           ({"OK": "x" * 501}, "env.OK")):
            refused = self.call_error(server, "start_flow_run", dict(base, session_id=first, env=env),
                                      "invalid_arguments")
            self.assertEqual(refused["field"], field)
        self.call_error(server, "start_flow_run", dict(base, session_id=first, flow_id="fl_" + "0" * 16),
                        "flow_not_found")
        self.call_error(server, "start_flow_run", dict(base, session_id=stopped), "session_stopped")
        self.call_error(server, "start_flow_run", dict(base, session_id="s_nosuch1234"), "session_not_found")
        self.assertEqual(self.cli_calls("flow_run"), [])
        run = self.call_ok(server, "start_flow_run", dict(base, session_id=first))["run"]
        busy = self.call_error(server, "start_flow_run", dict(base, session_id=first), "session_busy")
        self.assertEqual(busy["busy_with"], {"run_id": run["run_id"]})
        busy = self.call_error(server, "test_slow_mutation", {"host_id": host, "session_id": first}, "session_busy")
        self.assertEqual(busy["busy_with"], {"run_id": run["run_id"]})
        self.assertIs(busy["retry_safe"], True)
        other = self.call_ok(server, "start_flow_run", dict(base, session_id=second))["run"]
        limited = self.call_error(server, "start_flow_run", dict(base, session_id=third), "run_limit")
        self.assertIs(limited["retry_safe"], True)
        self.wait_calls("flow_run", 2)
        for item in (run, other):
            self.assertEqual(self.call_ok(server, "cancel_run", {"host_id": host, "run_id": item["run_id"]})
                             ["run"]["status"], "cancelled")
        self.call_ok(server, "start_flow_run", dict(base, session_id=third))

    def test_a_long_run_renews_the_agent_lease(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure(agent_lease_ttl_s=5)
        server = self.started(FAKE_MCP_STEP_MS="1000", FAKE_MCP_FLOW_STEPS="7")
        host = self.host_id(server)
        run = self.start_run(server, host, session_id)["run"]
        final = self.wait_terminal(server, host, run["run_id"], timeout=40)
        # Seven 1 s steps outlive the 5 s lease three renew periods over: without
        # the keep-alive the last steps would be refused and the run interrupted.
        self.assertEqual(final["status"], "passed", final)
        renews = self.cli_calls("control_renew")
        self.assertGreaterEqual(len(renews), 3)
        for call in renews:
            argv = call["argv"]
            self.assertEqual(argv[argv.index("--generation") + 1], str(final["generation"]))
            self.assertEqual(argv[argv.index("--holder") + 1], "chatgpt-agent:" + host)
        for call in self.cli_calls():
            self.assertNotIn("--takeover", call["argv"])
        self.assertIsNone(final["reason"])

    def test_a_run_on_a_reused_nearly_expired_lease_renews_it_at_once(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure(agent_lease_ttl_s=45)  # renew period 15 s
        server, call = self.in_process_server(FAKE_MCP_STEP_MS="1000", FAKE_MCP_FLOW_STEPS="14")
        host = call("list_hosts", {})["hosts"][0]["host_id"]
        flow = next(item["flow_id"] for item in call("list_flows", {})["flows"] if item["name"] == "login.yaml")
        keeper = server.services.keeper
        ref = server.services.refs.resolve(host, session_id)
        claim = keeper.ensure(ref)  # an earlier agent action took the lease
        # ...long enough ago that only 12 s are left: more than the 10 s reuse
        # margin, less than one renew period.
        FAKE.set_lease(self.fake_state, "android", "emulator-5584", generation=claim["generation"], ttl_s=12,
                       holder={"kind": "agent", "id": claim["holder"], "label": "ChatGPT agent"})
        with keeper._lock:
            keeper._cache[("android", "emulator-5584")]["expires_at"] = time.time() + 12
        began = time.time()
        run = call("start_flow_run", {"host_id": host, "session_id": session_id, "flow_id": flow})["run"]
        self.assertEqual(run["generation"], claim["generation"])
        self.assertEqual(len(self.cli_calls("control_acquire")), 1)  # the cached lease was reused
        deadline = time.monotonic() + 40
        while call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]["status"] in ("pending", "running"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.2)
        final = call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]
        # Fourteen 1 s steps outlive the 12 s left: the first renew comes at once,
        # not one period (15 s) later when the lease would already be gone.
        self.assertEqual(final["status"], "passed", final)
        renews = self.cli_calls("control_renew")
        self.assertTrue(renews)
        self.assertLess(renews[0]["start"] - began, 6.0, renews[0])  # one period would be 15 s
        self.assertIsNone(final["reason"])

    def flaky_renew_run(self, faults: Dict[str, List[Any]], steps: int) -> Any:
        """A run under a 5 s agent lease (renew period 1.7 s) whose keeper runner
        answers each call of a key with the next queued fault
        (``key -> [answer(cli_module, forward)]``, ``forward()`` runs the fake CLI)
        while the queue lasts; returns the final run, the keys the keeper ran and
        the server."""
        session_id = FAKE.add_session(self.fake_state)
        self.configure(agent_lease_ttl_s=5)
        server, call = self.in_process_server(FAKE_MCP_STEP_MS="1000", FAKE_MCP_FLOW_STEPS=str(steps))
        cli = core.import_server_modules()["cli"]
        keeper = server.services.keeper
        inner = keeper.runner
        seen: List[str] = []

        class Runner:
            def run(self, key: str, params: Dict[str, Any], **kwargs: Any) -> Any:
                seen.append(key)
                queue = faults.get(key)
                if queue:
                    return queue.pop(0)(cli, lambda: inner.run(key, params, **kwargs))
                return inner.run(key, params, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

        keeper.runner = Runner()
        host = call("list_hosts", {})["hosts"][0]["host_id"]
        flow = next(item["flow_id"] for item in call("list_flows", {})["flows"] if item["name"] == "login.yaml")
        run = call("start_flow_run", {"host_id": host, "session_id": session_id, "flow_id": flow})["run"]
        deadline = time.monotonic() + 40
        while call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]["status"] in ("pending", "running"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.2)
        final = call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]
        self.assertEqual(final["generation"], run["generation"])
        for item in self.cli_calls():
            self.assertNotIn("--takeover", item["argv"])
        return final, seen, server

    def test_a_renew_that_times_out_is_retried_and_the_run_keeps_its_lease(self) -> None:
        # The keeper drops its cached lease after the timeout; the retry about a
        # second later renews the run's own generation directly (never an
        # acquire), puts it back into the cache and renewing goes on.
        final, seen, server = self.flaky_renew_run(
            {"control_renew": [lambda cli, forward: cli.CliResult("control_renew", "timeout")]}, steps=8)
        self.assertEqual(final["status"], "passed", final)  # without renewing, steps after 5 s are refused
        self.assertIsNone(final["reason"])
        first = seen.index("control_renew")
        self.assertEqual(seen[first + 1], "control_renew", seen)
        self.assertGreaterEqual(seen.count("control_renew"), 3, seen)  # renewing went on after the retry
        self.assertEqual(seen.count("control_acquire"), 1, seen)  # only the run's start
        self.assertEqual(len(self.cli_calls("control_acquire")), 1)
        self.assertEqual(self.cli_calls("control_release"), [])
        for item in self.cli_calls("control_renew"):
            argv = item["argv"]
            self.assertEqual(argv[argv.index("--generation") + 1], str(final["generation"]))
        lease = FAKE.get_lease(self.fake_state, "android", "emulator-5584")
        self.assertEqual(lease["generation"], final["generation"])
        cached = server.services.keeper.cached("android", "emulator-5584")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["generation"], final["generation"])

    def test_a_refused_renew_stops_renewing_and_names_the_refusal(self) -> None:
        refusal = {"ok": False, "error_code": "control_held", "error": "held by a person",
                   "holder": {"kind": "human", "id": "canvas:1:2", "label": "Canvas 1"}, "generation": 9}
        final, seen, _server = self.flaky_renew_run(
            {"control_renew": [lambda cli, forward: cli.CliResult("control_renew", "cli_error", exit_code=2,
                                                                  error=dict(refusal))]}, steps=8)
        self.assertEqual(seen.count("control_renew"), 1, seen)
        self.assertEqual(seen.count("control_acquire"), 1, seen)
        self.assertEqual(final["reason"], "lease_renew_refused: control_held", final)
        self.assertEqual(final["status"], "interrupted", final)  # the lease ran out under the flow

    def test_a_retry_never_touches_a_lease_granted_to_another_server(self) -> None:
        moved: Dict[str, Any] = {}

        def lost(cli: Any, forward: Any) -> Any:
            # Before the retry the run's lease ran out and another adapter
            # server of this host (the same agent holder id) was granted the
            # target at the next generation.
            lease = FAKE.get_lease(self.fake_state, "android", "emulator-5584")
            moved["generation"] = lease["generation"] + 1
            FAKE.set_lease(self.fake_state, "android", "emulator-5584", holder=dict(lease["holder"]),
                           generation=moved["generation"], ttl_s=60)
            return forward()

        final, seen, server = self.flaky_renew_run(
            {"control_renew": [lambda cli, forward: cli.CliResult("control_renew", "timeout"), lost]}, steps=6)
        self.assertEqual(final["reason"], "lease_renew_refused: control_generation_stale", final)
        self.assertNotEqual(final["status"], "passed", final)
        self.assertEqual(seen.count("control_acquire"), 1, seen)  # only the run's start: no acquire renews it
        self.assertEqual(seen.count("control_renew"), 2, seen)  # the retry was refused and renewing stopped
        self.assertEqual(self.cli_calls("control_release"), [])  # the other server's lease is never released
        argv = self.cli_calls("control_renew")[-1]["argv"]
        self.assertEqual(argv[argv.index("--generation") + 1], str(final["generation"]))
        lease = FAKE.get_lease(self.fake_state, "android", "emulator-5584")
        self.assertEqual(lease["generation"], moved["generation"])
        self.assertIsNotNone(lease["holder"])
        self.assertIsNone(server.services.keeper.cached("android", "emulator-5584"))

    def test_a_lease_renewed_at_another_servers_generation_is_never_kept_or_released(self) -> None:
        # Two adapter servers of one host share the agent holder id. After the
        # run's lease of server A ran out, server B was granted the target (the
        # next generation); A's retried renew is answered as renewed in place at
        # B's generation. A must not cache that lease (so its shutdown never
        # releases it), stops renewing and names the reason.
        self.configure(agent_lease_ttl_s=5)
        second, _second_call = self.in_process_server()
        other_session = FAKE.add_session(self.fake_state)  # B's session on the same target
        granted: Dict[str, Any] = {}

        def renewed_in_place(cli: Any, forward: Any) -> Any:
            lease = FAKE.get_lease(self.fake_state, "android", "emulator-5584")
            FAKE.set_lease(self.fake_state, "android", "emulator-5584", holder=dict(lease["holder"]),
                           generation=lease["generation"], ttl_s=-1)  # A's lease ran out
            claim = second.services.keeper.ensure({"platform": "android", "target_id": "emulator-5584",
                                                   "session_id": other_session})
            granted["generation"] = claim["generation"]
            granted["holder"] = claim["holder"]
            current = FAKE.get_lease(self.fake_state, "android", "emulator-5584")
            return cli.CliResult("control_renew", "ok", exit_code=0, data={
                "ok": True, "acquired": True, "renewed": True, "taken_over": False, "lease": current,
                "generation": current["generation"], "state": "held"})

        final, seen, first = self.flaky_renew_run(
            {"control_renew": [lambda cli, forward: cli.CliResult("control_renew", "timeout"), renewed_in_place]},
            steps=6)
        self.assertEqual(granted["generation"], final["generation"] + 1, granted)
        self.assertEqual(granted["holder"], first.services.keeper.holder_id())  # the shared holder id
        self.assertEqual(final["reason"], "lease_renew_refused: control_generation_changed", final)
        self.assertEqual(seen.count("control_renew"), 2, seen)  # renewing stopped after that answer
        self.assertEqual(seen.count("control_acquire"), 1, seen)  # A never acquired again
        self.assertIsNone(first.services.keeper.cached("android", "emulator-5584"))
        self.assertEqual(second.services.keeper.cached("android", "emulator-5584")["generation"],
                         granted["generation"])
        first.shutdown()  # releases what A's keeper holds: nothing of B's
        self.assertEqual(self.cli_calls("control_release"), [])
        lease = FAKE.get_lease(self.fake_state, "android", "emulator-5584")
        self.assertEqual(lease["generation"], granted["generation"])
        self.assertEqual(lease["holder"]["id"], granted["holder"])
        self.assertIsNotNone(lease["expires_at"])
        # B still renews its own lease.
        renewed = second.services.keeper.renew({"platform": "android", "target_id": "emulator-5584",
                                                "session_id": other_session})
        self.assertEqual(renewed["generation"], granted["generation"])

    def test_hooks_see_and_cancel_a_run_that_is_still_launching(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        other = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        self.configure()
        server, call = self.in_process_server(FAKE_MCP_STEP_MS="30000",
                                              FAKE_MCP_CLI_MODE="slow:control_acquire:1500")
        host = call("list_hosts", {})["hosts"][0]["host_id"]
        flow = next(item["flow_id"] for item in call("list_flows", {})["flows"] if item["name"] == "login.yaml")
        store = server.services.extensions["mcp_runs"]

        def launching(session: str) -> Any:
            results: List[Dict[str, Any]] = []
            thread = threading.Thread(target=lambda: results.append(call("start_flow_run", {
                "host_id": host, "session_id": session, "flow_id": flow})))
            thread.start()
            deadline = time.monotonic() + 5
            while server.services.guard.busy(session) is None:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            time.sleep(0.4)  # inside the slow control acquire
            job = store.active_job(session)
            self.assertIsNotNone(job)
            self.assertEqual(server.services.guard.busy(session), {"run_id": job.run_id})
            return thread, results, job.run_id

        # stop_session's hook cancels the launch: the flow is never spawned.
        thread, results, run_id = launching(session_id)
        hooked = next(item for item in server.services.hooks.stopping(session_id) if item["name"] == "runs")
        self.assertEqual(hooked, {"name": "runs", "ok": True, "detail": "cancelled %s" % run_id})
        thread.join(10)
        self.assertEqual((results[0]["ok"], results[0]["error_code"]), (False, "session_stopped"), results)
        self.assertIsNone(server.services.guard.busy(session_id))
        self.assertEqual(call("get_run", {"host_id": host, "run_id": run_id})["error_code"], "run_not_found")
        # The shutdown hook sees a launch too, and no run starts after it.
        thread, results, run_id = launching(other)
        self.assertEqual(store.shutdown(), [run_id])
        thread.join(10)
        self.assertEqual((results[0]["ok"], results[0]["error_code"]), (False, "server_stopping"), results)
        self.assertEqual(self.cli_calls("flow_run"), [])
        self.assertIsNone(server.services.guard.busy(other))
        self.assertEqual(call("start_flow_run", {"host_id": host, "session_id": session_id, "flow_id": flow})
                         ["error_code"], "server_stopping")

    def test_a_cancel_during_the_spawn_interrupts_the_new_child(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        server, call = self.in_process_server(FAKE_MCP_STEP_MS="30000")
        host = call("list_hosts", {})["hosts"][0]["host_id"]
        flow = next(item["flow_id"] for item in call("list_flows", {})["flows"] if item["name"] == "login.yaml")
        store = server.services.extensions["mcp_runs"]
        runner = server.services.runner
        spawn = runner.spawn

        def spawn_then_stop(*args: Any, **kwargs: Any) -> Any:
            proc = spawn(*args, **kwargs)
            job = store.active_job(session_id)
            threading.Thread(target=store.session_stopping, args=(session_id,), daemon=True).start()
            deadline = time.monotonic() + 5
            while job.cancel_reason is None:  # the hook reached the job before launch() returned
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            return proc

        runner.spawn = spawn_then_stop
        run = call("start_flow_run", {"host_id": host, "session_id": session_id, "flow_id": flow})["run"]
        runner.spawn = spawn
        deadline = time.monotonic() + 15
        while call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]["status"] in ("pending", "running"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)
        final = call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]
        self.assertEqual((final["status"], final["reason"]), ("cancelled", "session_stopped"), final)
        self.assertIs(final["forced"], False)
        pid = self.record(run["run_id"])["pid"]
        self.assertTrue(wait_group_gone(pid, 2.0))
        # The terminal record comes first, the guard is released right after it.
        self.assertTrue(store.get_job(run["run_id"]).done.wait(5))
        self.assertIsNone(server.services.guard.busy(session_id))

    def test_restart_marks_outcome_unknown_and_stops_the_orphan(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        first = self.started(FAKE_MCP_STEP_MS="60000")
        host = self.host_id(first)
        run = self.start_run(first, host, session_id)["run"]
        self.wait_step(first, host, run["run_id"])
        # The step started right after the run did (inside the 0.5 s write
        # throttle) and lasts 60 s: the record on disk still shows it soon.
        began = time.monotonic()
        on_disk = self.wait_record(run["run_id"], lambda item: item["current_step"] is not None, timeout=1.2)
        self.assertLess(time.monotonic() - began, 1.2)
        self.assertEqual(on_disk["current_step"]["command"], "tapOn")
        pid = on_disk["pid"]
        first.proc.kill()  # no shutdown hooks: the flow child is orphaned
        first.proc.wait(timeout=10)
        self.assertTrue(core.pid_alive(pid))
        second = self.started(FAKE_MCP_STEP_MS="60000")
        record = self.wait_record(run["run_id"], lambda item: item["status"] != "running")
        self.assertEqual((record["status"], record["reason"]), ("outcome_unknown", "server_restarted"))
        self.assertEqual(record["last_step_outcome"], "unknown")
        self.assertIsNone(record["current_step"])
        self.assertTrue(core.wait_dead(pid, 15.0), "the orphan flow kept running")
        self.assertTrue(wait_group_gone(pid, 5.0))
        view = self.get_run(second, host, run["run_id"])
        self.assertEqual((view["status"], view["reason"]), ("outcome_unknown", "server_restarted"))
        self.assertEqual(view["last_step_outcome"], "unknown")
        self.call_error(second, "cancel_run", {"host_id": host, "run_id": run["run_id"]}, "run_not_running")
        self.assertIn("orphaned flow run", second.stderr_text())

    def test_an_orphan_record_stays_active_until_its_group_is_gone(self) -> None:
        # The orphan ignores SIGINT. A recovering server killed inside the 10 s
        # grace leaves the record active, so the next server tries again; that
        # server stopping at once still kills the orphan before it exits, and
        # only then writes the terminal record.
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        first = self.started(FAKE_MCP_STEP_MS="60000", FAKE_MCP_FLOW_IGNORE_SIGINT="1")
        host = self.host_id(first)
        run = self.start_run(first, host, session_id)["run"]
        self.wait_step(first, host, run["run_id"])
        pid = self.record(run["run_id"])["pid"]
        first.proc.kill()
        first.proc.wait(timeout=10)
        second = self.started()
        deadline = time.monotonic() + 10
        while "orphaned flow run" not in second.stderr_text():
            self.assertLess(time.monotonic(), deadline, second.stderr_text())
            time.sleep(0.05)
        self.assertEqual(self.record(run["run_id"])["status"], "running")
        second.proc.kill()  # inside the grace: no SIGKILL was sent yet
        second.proc.wait(timeout=10)
        self.assertTrue(core.pid_alive(pid))
        self.assertEqual(self.record(run["run_id"])["status"], "running")
        third = self.started()
        began = time.monotonic()
        self.assertEqual(third.close(timeout=20), 0)
        self.assertLess(time.monotonic() - began, 12.0)
        self.assertTrue(wait_group_gone(pid, 2.0), "the orphan outlived the server that stopped it")
        record = self.record(run["run_id"])
        self.assertEqual((record["status"], record["reason"]), ("outcome_unknown", "server_restarted"))
        self.assertIs(record["forced"], True)
        self.assertIsNone(record["current_step"])

    def test_concurrent_cancels_of_a_gone_servers_run_recover_it_once(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        first = self.started(FAKE_MCP_STEP_MS="60000")
        host = self.host_id(first)
        run = self.start_run(first, host, session_id)["run"]
        self.wait_step(first, host, run["run_id"])
        pid = self.record(run["run_id"])["pid"]
        second = self.started()  # the owner is alive: startup leaves the run alone
        self.assertEqual(self.get_run(second, host, run["run_id"])["status"], "running")
        first.proc.kill()
        first.proc.wait(timeout=10)
        tool_defs = self.schemas(second)
        common = core.import_server_modules()["common"]
        ids = []
        for _ in range(2):
            msg_id = second.new_id()
            ids.append(msg_id)
            second.send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                         "params": {"name": "cancel_run", "arguments": {"host_id": host, "run_id": run["run_id"]}}})
        replies: Dict[Any, Dict[str, Any]] = {}
        deadline = time.monotonic() + 30
        while len(replies) < 2:
            reply = second.recv(timeout=max(0.1, deadline - time.monotonic()))
            self.assertIsNotNone(reply)
            if isinstance(reply, dict) and reply.get("id") in ids and "method" not in reply:
                replies[reply["id"]] = reply
        results = []
        for msg_id in ids:
            structured = self.check_envelope(replies[msg_id]["result"], "cancel_run")
            self.assertEqual(common.validate(tool_defs["cancel_run"]["outputSchema"], structured), [], structured)
            results.append(structured)
        done = [item for item in results if item["ok"]]
        refused = [item for item in results if not item["ok"]]
        self.assertEqual((len(done), len(refused)), (1, 1), results)
        self.assertEqual((done[0]["run"]["status"], done[0]["run"]["reason"]), ("outcome_unknown", "server_restarted"))
        self.assertEqual(refused[0]["error_code"], "run_not_running", refused[0])
        self.assertEqual(refused[0]["run"]["status"], "outcome_unknown")
        # One terminal write: both answers and the record carry the same end.
        record = self.record(run["run_id"])
        self.assertEqual(record["status"], "outcome_unknown")
        self.assertEqual(refused[0]["run"]["ended_at"], done[0]["run"]["ended_at"])
        self.assertEqual(record["ended_at"], done[0]["run"]["ended_at"])
        self.assertTrue(wait_group_gone(pid, 5.0))

    def test_a_second_live_server_leaves_the_first_servers_run_alone(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        first = self.started(FAKE_MCP_STEP_MS="30000")
        host = self.host_id(first)
        run = self.start_run(first, host, session_id)["run"]
        self.wait_step(first, host, run["run_id"])
        pid = self.record(run["run_id"])["pid"]
        second = self.started()
        self.assertEqual(self.get_run(second, host, run["run_id"])["status"], "running")
        refused = self.call_error(second, "cancel_run", {"host_id": host, "run_id": run["run_id"]}, "run_not_owned")
        self.assertEqual(refused["run"]["status"], "running")
        self.assertIn("conversation that started it", refused["hint"])
        time.sleep(0.5)
        self.assertEqual(self.record(run["run_id"])["status"], "running")
        self.assertTrue(core.pid_alive(pid))
        cancelled = self.call_ok(first, "cancel_run", {"host_id": host, "run_id": run["run_id"]})["run"]
        self.assertEqual(cancelled["status"], "cancelled")

    def test_server_shutdown_cancels_running_runs(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        self.configure()
        server = self.started(FAKE_MCP_STEP_MS="30000")
        host = self.host_id(server)
        run = self.start_run(server, host, session_id)["run"]
        self.wait_step(server, host, run["run_id"])
        pid = self.record(run["run_id"])["pid"]
        self.assertEqual(server.close(timeout=20), 0)
        record = self.record(run["run_id"])
        self.assertEqual((record["status"], record["reason"]), ("cancelled", "server_stopped"))
        self.assertTrue(wait_group_gone(pid, 2.0))

    def test_the_session_hook_cancels_the_sessions_run(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        idle = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        self.configure()
        modules = core.import_server_modules()
        env = self.server_env(FAKE_MCP_STEP_MS="30000")
        server = modules["server"].McpServer(env, self.home, io.BytesIO(), io.BytesIO(), io.StringIO())
        self.addCleanup(server.shutdown)
        common = modules["common"]

        def call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            tool = server.tools[name]
            structured = server.call_tool(tool, arguments, threading.Event())["structuredContent"]
            self.assertEqual(common.validate(server.schemas[name], structured), [], structured)
            return structured

        host = call("list_hosts", {})["hosts"][0]["host_id"]
        flow = next(item["flow_id"] for item in call("list_flows", {})["flows"] if item["name"] == "login.yaml")
        run = call("start_flow_run", {"host_id": host, "session_id": session_id, "flow_id": flow})["run"]
        deadline = time.monotonic() + 15
        while call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]["current_step"] is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)
        def runs_hook(results: List[Dict[str, Any]]) -> Dict[str, Any]:
            return next(item for item in results if item["name"] == "runs")

        self.assertEqual(runs_hook(server.services.hooks.stopping(idle)), {"name": "runs", "ok": True, "detail": None})
        results = server.services.hooks.stopping(session_id)
        self.assertEqual(runs_hook(results), {"name": "runs", "ok": True, "detail": "cancelled %s" % run["run_id"]})
        view = call("get_run", {"host_id": host, "run_id": run["run_id"]})["run"]
        self.assertEqual((view["status"], view["reason"]), ("cancelled", "session_stopped"))
        self.assertIsNone(server.services.guard.busy(session_id))
        # run_max_s: a run past its deadline is cancelled with reason run_timeout.
        timed = call("start_flow_run", {"host_id": host, "session_id": idle, "flow_id": flow})["run"]
        server.services.extensions["mcp_runs"].get_job(timed["run_id"]).deadline = time.monotonic()
        deadline = time.monotonic() + 15
        while call("get_run", {"host_id": host, "run_id": timed["run_id"]})["run"]["status"] in ("pending", "running"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)
        view = call("get_run", {"host_id": host, "run_id": timed["run_id"]})["run"]
        self.assertEqual((view["status"], view["reason"]), ("cancelled", "run_timeout"))


# ====================================================================== artifacts

class ArtifactTests(RunsCase):
    PNG = FAKE.png_bytes(2, 2)

    def fixture(self, session_id: str, other: str) -> Path:
        """The traversal fixture: only ``screenshots/shot.png`` may be listed or served."""
        base = self.fake_state / "sessions" / session_id
        (base / "screenshots").mkdir(parents=True)
        (base / "screenshots" / "shot.png").write_bytes(self.PNG)
        (base / "network").mkdir()
        (base / "network" / "capture.json").write_text("{}", encoding="utf-8")
        (base / "logs").mkdir()
        (base / "logs" / "network").mkdir()
        (base / "logs" / "network" / "flows.txt").write_text("x", encoding="utf-8")
        for name in (".env", "secrets.log", "cookies.json", "token.txt", "mitm.log", "trace.har.json",
                     "notes.md", "data.bin"):
            (base / name).write_text("SECRET=1\n", encoding="utf-8")
        os.symlink("/etc", str(base / "etc"))
        os.symlink("/etc/hosts", str(base / "hosts.txt"))
        os.symlink(str(self.fake_state / "sessions" / other), str(base / "other-session"))
        with open(str(base / "huge.log"), "wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
        deep = base / "a" / "b" / "c" / "d" / "e" / "f" / "g"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("deep", encoding="utf-8")
        return base

    def key(self) -> bytes:
        return (self.mcp_dir / "artifact-key").read_bytes()

    def test_ids_exclusions_and_crafted_ids(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        other = FAKE.add_session(self.fake_state, "ios", IOS_UDID)
        (self.fake_state / "sessions" / other / "screenshots").mkdir(parents=True)
        (self.fake_state / "sessions" / other / "screenshots" / "shot.png").write_bytes(self.PNG)
        self.fixture(session_id, other)
        server = self.started()
        host = self.host_id(server)
        ref = {"host_id": host, "session_id": session_id}
        listed = self.call_ok(server, "list_artifacts", ref)
        self.assertEqual([(item["name"], item["kind"], item["group"]) for item in listed["artifacts"]],
                         [("shot.png", "image", "screenshots")])
        entry = listed["artifacts"][0]
        self.assertEqual((entry["size"], entry["mime"]), (len(self.PNG), "image/png"))
        self.assertIs(listed["truncated"], False)
        refs = core.import_server_modules()["refs"]
        key = self.key()
        self.assertEqual(entry["artifact_id"], refs.artifact_id(key, host, session_id, "screenshots/shot.png"))
        self.assertEqual(self.call_ok(server, "list_artifacts", ref)["artifacts"], listed["artifacts"])
        other_listed = self.call_ok(server, "list_artifacts", {"host_id": host, "session_id": other})["artifacts"]
        self.assertEqual(len(other_listed), 1)
        self.assertNotEqual(other_listed[0]["artifact_id"], entry["artifact_id"])
        served = self.call_ok(server, "get_artifact", dict(ref, artifact_id=entry["artifact_id"]))
        self.assertEqual(served["artifact"]["name"], "shot.png")
        # Crafted ids: excluded files, symlinks, deep files and another session's file.
        crafted = ["network/capture.json", "logs/network/flows.txt", ".env", "secrets.log", "cookies.json",
                   "token.txt", "mitm.log", "trace.har.json", "notes.md", "data.bin", "etc/hosts", "hosts.txt",
                   "other-session/screenshots/shot.png", "huge.log", "a/b/c/d/e/f/g/deep.txt", "session.json",
                   "../%s/screenshots/shot.png" % other]
        for relpath in crafted:
            self.call_error(server, "get_artifact", dict(ref, artifact_id=refs.artifact_id(key, host, session_id,
                                                                                         relpath)),
                            "artifact_not_found")
        self.call_error(server, "get_artifact", {"host_id": host, "session_id": other,
                                                 "artifact_id": entry["artifact_id"]}, "artifact_not_found")
        for structured in (listed, served):
            self.assert_no_paths(structured)
        # Ids survive a server restart (the key is kept 0600).
        self.assert_mode(self.mcp_dir / "artifact-key", 0o600)
        restarted = self.started()
        self.assertEqual(self.call_ok(restarted, "list_artifacts", ref)["artifacts"][0]["artifact_id"],
                         entry["artifact_id"])

    def page_through(self, server: core.ServerProcess, ref: Dict[str, Any], artifact_id: str,
                     max_bytes: int) -> List[Dict[str, Any]]:
        """Every page of a text artifact from offset 0 along ``next_offset``."""
        offset: Optional[int] = 0
        pages: List[Dict[str, Any]] = []
        while offset is not None:
            result = server.call("get_artifact", dict(ref, artifact_id=artifact_id, offset=offset,
                                                      max_bytes=max_bytes))
            self.assertIs(result["structuredContent"]["ok"], True, result)
            self.assert_no_paths(result)
            page = result["structuredContent"]["artifact"]
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["length"], len(page["text"].encode("utf-8")), page)
            self.assertGreater(page["length"], 0, page)
            self.assertLessEqual(page["length"], max(max_bytes, 4), page)  # one whole character at least
            pages.append(page)
            offset = page["next_offset"]
            if offset is not None:
                self.assertEqual(offset, page["offset"] + page["length"])
        return pages

    def test_text_artifacts_name_no_local_path_on_any_page(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        base = self.fake_state / "sessions" / session_id
        (base / "screenshots").mkdir(parents=True)
        (base / "screenshots" / "shot.png").write_bytes(self.PNG)
        shot = base / "screenshots" / "shot.png"
        report = str(self.tmp / "else" / "r.html")
        # The CLI journal keeps a screenshot's absolute path in its result summary.
        lines = [
            json.dumps({"verb": "ui screenshot", "ok": True, "result": {"path": str(shot)}}),
            json.dumps({"verb": "ui screenshot", "ok": True, "result": {"path": os.path.realpath(str(shot))}}),
            json.dumps({"verb": "flow run", "ok": True, "result": {"path": str(self.home / "flows" / "a.yaml"),
                                                                   "report": report}}),
            "Authorization: Bearer %s" % SECRET_VALUE,
            json.dumps({"a": str(self.home / "one.png"), "msg": "two paths on one line",
                        "b": str(self.home / "two.png"), "c": "/Users/someone/three.png"}),
        ]
        text = "\n".join(lines) + "\n"
        (base / "journal.ndjson").write_text(text, encoding="utf-8")
        server = self.started()
        host = self.host_id(server)
        ref = {"host_id": host, "session_id": session_id}
        journal = self.call_ok(server, "list_artifacts", dict(ref, kind="journal"))["artifacts"][0]
        whole = server.call("get_artifact", dict(ref, artifact_id=journal["artifact_id"]))
        self.assert_no_paths(whole)
        self.assertNotIn(SECRET_VALUE, json.dumps(whole))
        artifact = whole["structuredContent"]["artifact"]
        cleaned = artifact["text"].splitlines()
        self.assertEqual(json.loads(cleaned[0])["result"]["path"], "<session>/screenshots/shot.png")
        self.assertEqual(json.loads(cleaned[1])["result"]["path"], "<session>/screenshots/shot.png")
        self.assertEqual(json.loads(cleaned[2])["result"], {"path": "~/flows/a.yaml",
                                                            "report": self.temp_labelled(report)})
        self.assertEqual(json.loads(cleaned[4]), {"a": "~/one.png", "msg": "two paths on one line", "b": "~/two.png",
                                                  "c": "/Users/<user>/three.png"})
        # Offsets and lengths count the cleaned text; the size is the file's.
        self.assertEqual(artifact["length"], len(artifact["text"].encode("utf-8")))
        self.assertEqual((artifact["offset"], artifact["next_offset"]), (0, None))
        self.assertEqual(artifact["size"], len(text.encode("utf-8")))
        fragments = {SECRET_VALUE[:6], SECRET_VALUE[-6:], "/var/folders", "/private/"}
        for size in (1, 7, 23, 61):
            pages = self.page_through(server, ref, journal["artifact_id"], size)
            for page in pages:
                for fragment in fragments:
                    self.assertNotIn(fragment, page["text"], (size, page))
            self.assertEqual("".join(page["text"] for page in pages), artifact["text"], size)

    def test_pages_join_to_the_whole_cleaned_text(self) -> None:
        # Paging is over the cleaned text: for every page size the pages join
        # to exactly the whole read, multi-byte characters are never cut, and
        # a root is never split across pages (so no page shows part of one).
        session_id = FAKE.add_session(self.fake_state)
        base = self.fake_state / "sessions" / session_id
        (base / "logs").mkdir(parents=True)
        text = ("é %s/a.log😀%s/b €\n/Users/someone/x ünï %s/flows/c.yaml\n"
                % (self.home, os.path.realpath(str(base)), self.home))
        (base / "logs" / "run.log").write_text(text, encoding="utf-8")
        server = self.started()
        host = self.host_id(server)
        ref = {"host_id": host, "session_id": session_id}
        item = self.call_ok(server, "list_artifacts", dict(ref, kind="log"))["artifacts"][0]
        whole = self.call_ok(server, "get_artifact", dict(ref, artifact_id=item["artifact_id"]))["artifact"]
        expected = "é ~/a.log😀<session>/b €\n/Users/<user>/x ünï ~/flows/c.yaml\n"
        self.assertEqual(whole["text"], expected)
        self.assertEqual((whole["length"], whole["next_offset"]), (len(expected.encode("utf-8")), None))
        for size in (1, 2, 3, 5, 11, 64, 1048576):
            pages = self.page_through(server, ref, item["artifact_id"], size)
            self.assertEqual("".join(page["text"] for page in pages), whole["text"], size)
            self.assertEqual(sum(page["length"] for page in pages), whole["length"], size)
        # An offset inside a character starts at the next whole one.
        middle = len("é".encode("utf-8")) - 1
        page = self.call_ok(server, "get_artifact", dict(ref, artifact_id=item["artifact_id"], offset=middle,
                                                         max_bytes=1))["artifact"]
        self.assertEqual((page["offset"], page["text"]), (middle + 1, " "))

    def test_paths_inside_markup_urls_and_flags_are_scrubbed(self) -> None:
        session_id = FAKE.add_session(self.fake_state)
        base = self.fake_state / "sessions" / session_id
        outside = str(self.tmp / "T" / "autonom" / "shot.png")
        shown = self.temp_labelled(outside)
        (base / "logs").mkdir(parents=True)
        (base / "logs" / "sim.txt").write_text(
            "opened file://%s\nout -o%s and --out=%s\nsee http://example.com/a/b and a/b ./c ~/d\n"
            "img data:image/png;base64,iVBORw0KGgo/AAAA+/x/9j==\n" % (outside, outside, outside), encoding="utf-8")
        (base / "report.html").write_text(
            "<p><code>/Volumes/Work/flows/login.yaml</code></p><table><tr><td>/opt/run/x.log</td></tr></table>"
            "<pre>/private/var/folders/ab/T/step.log\n</pre><p>failed<br>%s: no file</p>"
            "<a href=\"file:///Users/someone/Desktop/a.png\">a</a>" % outside,
            encoding="utf-8")
        server = self.started()
        host = self.host_id(server)
        ref = {"host_id": host, "session_id": session_id}
        texts = {}
        for kind in ("log", "report"):
            item = self.call_ok(server, "list_artifacts", dict(ref, kind=kind))["artifacts"][0]
            result = server.call("get_artifact", dict(ref, artifact_id=item["artifact_id"]))
            self.assert_no_paths(result)
            for value in strings(result):
                for absolute in ("/private/", "/var/folders", "/Users/someone"):
                    self.assertNotIn(absolute, value, result)
            texts[kind] = result["structuredContent"]["artifact"]["text"]
        self.assertEqual(texts["log"], "opened file://%s\nout -o%s and --out=%s\n"
                                       "see http://example.com/a/b and a/b ./c ~/d\n"
                                       "img data:image/png;base64,iVBORw0KGgo/AAAA+/x/9j==\n" % (shown, shown, shown))
        # Paths outside the known roots (/Volumes, /opt) are kept as they are (DEC-027).
        self.assertEqual(texts["report"], "<p><code>/Volumes/Work/flows/login.yaml</code></p><table><tr><td>"
                                          "/opt/run/x.log</td></tr></table><pre><tmp>/step.log\n</pre><p>failed<br>"
                                          "%s: no file</p><a href=\"file:///Users/<user>/Desktop/a.png\">a</a>" % shown)

    def test_paging_text_images_filters_and_stopped_sessions(self) -> None:
        session_id = FAKE.add_session(self.fake_state, live=False)
        base = self.fake_state / "sessions" / session_id
        (base / "screenshots").mkdir()
        (base / "screenshots" / "shot.png").write_bytes(self.PNG)
        text = "".join("line %04d é\n" % index for index in range(200))
        (base / "logs").mkdir()
        (base / "logs" / "run.log").write_text(text, encoding="utf-8")
        (base / "journal.ndjson").write_text('{"verb": "ui tap"}\n', encoding="utf-8")
        (base / "report.html").write_text("<p>report</p>", encoding="utf-8")
        old = time.time() - 100
        os.utime(str(base / "journal.ndjson"), (old, old))
        server = self.started()
        host = self.host_id(server)
        ref = {"host_id": host, "session_id": session_id}
        listed = self.call_ok(server, "list_artifacts", ref)["artifacts"]
        self.assertEqual(listed[-1]["name"], "journal.ndjson")  # newest first
        self.assertEqual(sorted((item["name"], item["kind"]) for item in listed),
                         [("journal.ndjson", "journal"), ("report.html", "report"), ("run.log", "log"),
                          ("shot.png", "image")])
        only = self.call_ok(server, "list_artifacts", dict(ref, kind="log"))["artifacts"]
        self.assertEqual([item["name"] for item in only], ["run.log"])
        limited = self.call_ok(server, "list_artifacts", dict(ref, limit=1))
        self.assertEqual((len(limited["artifacts"]), limited["truncated"]), (1, True))
        log_id = only[0]["artifact_id"]
        data = text.encode("utf-8")
        offset, chunks = 0, []
        while offset is not None:
            page = self.call_ok(server, "get_artifact", dict(ref, artifact_id=log_id, offset=offset,
                                                             max_bytes=1000))["artifact"]
            self.assertEqual((page["offset"], page["size"], page["mime"]), (offset, len(data), "text/plain"))
            # A page ends before a character it would cut (the 2-byte é).
            self.assertIn(page["length"], (min(1000, len(data) - offset), 999), page["length"])
            self.assertEqual(page["text"].encode("utf-8"), data[offset:offset + page["length"]])
            chunks.append(page["text"])
            offset = page["next_offset"]
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), text)
        whole = self.call_ok(server, "get_artifact", dict(ref, artifact_id=log_id))["artifact"]
        self.assertEqual((whole["text"], whole["next_offset"]), (text, None))
        beyond = self.call_ok(server, "get_artifact", dict(ref, artifact_id=log_id, offset=len(data) + 5))
        self.assertEqual((beyond["artifact"]["length"], beyond["artifact"]["next_offset"]), (0, None))
        self.call_error(server, "get_artifact", dict(ref, artifact_id=log_id, max_bytes=0), "invalid_arguments")
        shot = next(item for item in listed if item["kind"] == "image")
        result = server.call("get_artifact", dict(ref, artifact_id=shot["artifact_id"]))
        self.assertEqual(base64.b64decode(result["_meta"]["autonom/artifact"]["data_b64"]), self.PNG)
        self.assertEqual(result["content"][1], {"type": "image", "data": base64.b64encode(self.PNG).decode("ascii"),
                                                "mimeType": "image/png"})
        self.assertNotIn("text", result["structuredContent"]["artifact"])
        part = server.call("get_artifact", dict(ref, artifact_id=shot["artifact_id"], offset=4))
        self.assertEqual(len(part["content"]), 1)  # a partial image is never an image block
        self.assertEqual(base64.b64decode(part["_meta"]["autonom/artifact"]["data_b64"]), self.PNG[4:])
        self.assertEqual(stat.S_IMODE((self.mcp_dir / "artifact-key").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
