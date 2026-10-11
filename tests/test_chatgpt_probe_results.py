"""ChatGPT probe results checker: results rules, profiles, doc agreement, template, stub.

Contract 7.1 and 8 (harness). The checker runs in process for the rule tests
and as a subprocess for exit codes and refusal payloads. Every file lives in a
temporary directory; ``AUTONOM_HOME`` is sandboxed.
"""
from __future__ import annotations

import copy
import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from env_isolation import EnvSandboxMixin  # discover -s tests / run_parallel
except ImportError:  # python3 -m unittest tests.test_chatgpt_probe_results
    from tests.env_isolation import EnvSandboxMixin

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "integrations" / "chatgpt" / "scripts" / "probe_results.py"
CORE = ROOT / "integrations" / "chatgpt" / "autonom-chatgpt-probe" / "ui" / "probe-core.mjs"
DOC = ROOT / "docs" / "plans" / "CHATGPT_P0_RESULTS.md"
# The empty template exactly as first committed (git blob 0fd904e8749b7cbc779414c2a822bc050c16ff78);
# the real doc above is now filled from live results.
TEMPLATE_BLOB = "0fd904e8749b7cbc779414c2a822bc050c16ff78"
TEMPLATE = r"""# ChatGPT P0 results: desktop host contract

**Status:** template. Every `pending` cell and every "Pending" line below is filled from observed
results only, after the live run (contract section 10). A cell never claims more than its results file
says. Check the filled doc with:

```
python3 integrations/chatgpt/scripts/probe_results.py check --profile live --doc docs/plans/CHATGPT_P0_RESULTS.md \
  --results harness-chromium=EV/harness/results-chromium.json \
  --results harness-webkit=EV/harness/results-webkit.json \
  --results harness-devices=EV/live/devices-harness/results-chromium.json \
  --results harness-forwarded=EV/live/forwarded/results-chromium.json \
  --results chatgpt-global=EV/live/results-chatgpt-global.json \
  --results chatgpt-thread=EV/live/results-chatgpt-thread.json \
  --results chatgpt-devices=EV/live/results-chatgpt-devices.json
```

`EV` is the run's evidence directory. The empty template is checked with
`probe_results.py check-template --doc docs/plans/CHATGPT_P0_RESULTS.md`.

## 1. Environment

| Item | Value |
| --- | --- |
| ChatGPT desktop build (`CFBundleShortVersionString`, `CFBundleVersion`) | Pending |
| macOS version (`sw_vers`) | Pending |
| Mode | Pending (expected: Work mode) |
| Account surface | Pending |
| Node version | Pending |
| Python version (`/usr/bin/python3`) | Pending |
| Probe version | 0.1.0 |
| Date | Pending |

## 2. Capability matrix

One row per check of the probe page (27 checks), one column per results file. Each cell is `pass`,
`fail` or `not exercised`, optionally followed by ` · <value>`. Columns:

- `harness-chromium`, `harness-webkit`: the fake MCP Apps host harness with the in-process proxy as the
  second origin (`tests/live/chatgpt_probe_host_live.mjs --browser both --remote-proxy`).
- `harness-devices`: the harness in Chromium with the two allowed test devices (`--device`).
- `harness-forwarded`: the harness in Chromium with a real `ssh -L` forward (`--ssh-forward localhost`).
- `chatgpt-global`, `chatgpt-thread`: ChatGPT desktop, opened from the sidebar and from a conversation.
- `chatgpt-devices`: ChatGPT desktop, *Run device checks*.

| Check | harness-chromium | harness-webkit | harness-devices | harness-forwarded | chatgpt-global | chatgpt-thread | chatgpt-devices |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `net.fetch.local` | pending | pending | pending | pending | pending | pending | pending |
| `net.fetch.remote` | pending | pending | pending | pending | pending | pending | pending |
| `net.ws.local` | pending | pending | pending | pending | pending | pending | pending |
| `net.ws.remote` | pending | pending | pending | pending | pending | pending | pending |
| `input.rtt.local` | pending | pending | pending | pending | pending | pending | pending |
| `input.rtt.remote` | pending | pending | pending | pending | pending | pending | pending |
| `media.decode.local` | pending | pending | pending | pending | pending | pending | pending |
| `media.decode.remote` | pending | pending | pending | pending | pending | pending | pending |
| `net.reconnect.local` | pending | pending | pending | pending | pending | pending | pending |
| `net.reconnect.remote` | pending | pending | pending | pending | pending | pending | pending |
| `net.csp.local` | pending | pending | pending | pending | pending | pending | pending |
| `net.csp.remote` | pending | pending | pending | pending | pending | pending | pending |
| `host.init` | pending | pending | pending | pending | pending | pending | pending |
| `host.entry` | pending | pending | pending | pending | pending | pending | pending |
| `host.theme` | pending | pending | pending | pending | pending | pending | pending |
| `host.size` | pending | pending | pending | pending | pending | pending | pending |
| `bridge.read` | pending | pending | pending | pending | pending | pending | pending |
| `bridge.write` | pending | pending | pending | pending | pending | pending | pending |
| `bridge.error` | pending | pending | pending | pending | pending | pending | pending |
| `bridge.stale` | pending | pending | pending | pending | pending | pending | pending |
| `bridge.context` | pending | pending | pending | pending | pending | pending | pending |
| `bridge.message` | pending | pending | pending | pending | pending | pending | pending |
| `bridge.media` | pending | pending | pending | pending | pending | pending | pending |
| `model.tool` | pending | pending | pending | pending | pending | pending | pending |
| `media.webcodecs` | pending | pending | pending | pending | pending | pending | pending |
| `device.android.local` | pending | pending | pending | pending | pending | pending | pending |
| `device.ios.remote` | pending | pending | pending | pending | pending | pending | pending |

## 3. Routes

Each route has two results: the command path (tool calls through the host bridge) and the media path
(fetch, WebSocket and decoded video from the page).

| Route | Origin | Command path | Media path | Matrix rows |
| --- | --- | --- | --- | --- |
| Local loopback | `http://127.0.0.1:47811` | pending | pending | `net.*.local`, `input.rtt.local`, `media.decode.local` |
| Bridge only | none (tool calls only) | pending | pending | `bridge.*`, `bridge.media` |
| Forwarded endpoint | `http://127.0.0.1:47812` | pending | pending | `*.remote` in `harness-forwarded` |
| Remote HTTPS host | none | pending | pending | none |

Pending: the capability each failing route needs (for example a real page origin, a relay, or an HTTPS
origin declared in the CSP plus a token exchange).

## 4. Transport and topology decision for P3 and P4

Pending: one chosen topology for P3 and P4, with the matrix rows that justify it.

## 5. Installation requirements

Pending: what installing the probe needed in practice (marketplace entry, restart, Work mode enablement,
allowed origins), and any extra observation outside Work mode.

## 6. Revised estimates

Pending: P1 to P4 estimates revised from the observed results.

## 7. Evidence

Pending: the evidence index (results files with their `shasum -a 256`, screenshots, host logs, install
and uninstall outputs). A results file written with `probe_results.py stub` is named here as a stub.

## 8. Not exercised and why

Pending: every `not exercised` cell with its reason from the results file.
"""
NODE = shutil.which("node")


def load_checker():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("probe_results_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


PR = load_checker()

MANUAL = {"host.entry", "model.tool"}
DEVICES = {"device.android.local", "device.ios.remote"}


def valid_results(remote_selected: bool = True) -> Dict[str, Any]:
    """A results object as a good harness run produces it: harness profile passes."""
    checks: List[Dict[str, Any]] = []
    for cid, group, route, deps in PR.CHECKS:
        record: Dict[str, Any] = {
            "id": cid, "group": group, "route": route, "status": "pass", "observed_by": "probe",
            "value": 12, "unit": "ms", "error": None, "reason": None, "depends_on": list(deps),
            "started_at": "2026-10-10T14:22:34Z", "duration_ms": 12,
        }
        if cid in MANUAL or cid in DEVICES or (route == "remote" and not remote_selected):
            record.update(status="not_exercised", value=None, unit=None, reason="Needs the operator",
                          started_at=None, duration_ms=None)
        if cid == "bridge.context":
            record.update(observed_by="operator", value="probe-7f3a-lime", unit=None)
        checks.append(record)
    counts = {status: sum(1 for c in checks if c["status"] == status) for status in PR.STATUSES}
    return {
        "schema_version": 1, "probe_version": "0.1.0", "run_id": "probe-20261010-142233-7f3a",
        "started_at": "2026-10-10T14:22:33Z", "finished_at": "2026-10-10T14:23:04Z",
        "host": {"name": "autonom-fake-host", "version": "0.1.0", "protocol": "2026-01-26", "entry": "unknown",
                 "display_mode": "fullscreen", "theme": "light", "container": {"width": 432, "height": 780},
                 "platform": "desktop", "user_agent": "x", "token_path": "tool-result",
                 "mcp": {"protocol_version": "2025-11-25", "client": {"name": "autonom-fake-host", "version": "0.1.0"},
                         "client_ui_extension": True, "methods_seen": ["initialize"]}},
        "routes": {"local": {"origin": "http://127.0.0.1:47811"},
                   "remote": ({"origin": "http://127.0.0.1:47812", "label": "proxy", "selected": True}
                              if remote_selected else {"origin": None, "label": None, "selected": False})},
        "checks": checks,
        "summary": counts,
    }


def index_of(data: Dict[str, Any], cid: str) -> int:
    return [c["id"] for c in data["checks"]].index(cid)


def recount(data: Dict[str, Any]) -> Dict[str, Any]:
    data["summary"] = {s: sum(1 for c in data["checks"] if c.get("status") == s) for s in PR.STATUSES}
    return data


def filled_doc(results: Dict[str, Dict[str, Any]], values: bool = False) -> str:
    """The template with each labelled column filled from its results file."""
    text = TEMPLATE
    lines = text.splitlines()
    out = []
    for line in lines:
        cells = PR.split_row(line) if line.startswith("| `") else None
        if cells and cells[0].strip("`") in PR.CHECK_BY_ID:
            cid = cells[0].strip("`")
            row = [cells[0]]
            for label, cell in zip(PR.DOC_LABELS, cells[1:]):
                data = results.get(label)
                if data is None:
                    row.append(cell)
                    continue
                status = next(c["status"] for c in data["checks"] if c["id"] == cid)
                word = PR.STATUS_WORDS[status]
                row.append(word + (" · 12 ms" if values and status == "pass" else ""))
            line = "| " + " | ".join(row) + " |"
        out.append(line)
    return "\n".join(out) + "\n"


class CheckerCase(EnvSandboxMixin, unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.sandbox_home()
        self.tmp = Path(tempfile.mkdtemp(prefix="probe-results-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name: str, data: Any) -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def run_cli(self, *args: str) -> Tuple[int, str, str]:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, env=env, timeout=60, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    def run_json(self, *args: str) -> Tuple[int, Dict[str, Any]]:
        code, out, err = self.run_cli(*args, "--json")
        self.assertEqual(err, "", err)
        return code, json.loads(out)

    def assert_refusal(self, payload: Dict[str, Any], code: str) -> None:
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error_code"], code)
        for key in ("error", "hint"):
            self.assertIsInstance(payload[key], str)
            self.assertTrue(payload[key], key)

    def paths(self, problems: List[Dict[str, str]]) -> List[str]:
        return [p["path"] for p in problems]


# ====================================================================== base rules

class ResultsRuleTests(CheckerCase):
    def test_valid_files_pass_base_harness_and_live(self) -> None:
        good = self.write("good.json", valid_results())
        for profile in ([], ["--profile", "harness"], ["--profile", "live"]):
            with self.subTest(profile=profile):
                code, payload = self.run_json("check", "--results", "harness-chromium=%s" % good, *profile)
                self.assertEqual(code, 0, payload)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["problems"], [])
                self.assertEqual(payload["files"][0]["label"], "harness-chromium")
                self.assertEqual(payload["profile"], profile[1] if profile else "base")

    def test_each_rule_violated_once_names_the_path(self) -> None:
        def mutate(fn):
            data = valid_results()
            fn(data)
            return data

        i_ws = index_of(valid_results(), "net.ws.local")
        i_entry = index_of(valid_results(), "host.entry")
        cases = [
            ("schema", lambda d: d.update(schema_version=2), "schema_version"),
            ("schema bool", lambda d: d.update(schema_version=True), "schema_version"),
            ("probe_version", lambda d: d.update(probe_version=""), "probe_version"),
            ("run_id", lambda d: d.update(run_id="probe-2026-x"), "run_id"),
            ("started_at", lambda d: d.update(started_at="2026-10-10 14:22:33"), "started_at"),
            ("started_at date", lambda d: d.update(started_at="2026-13-40T14:22:33Z"), "started_at"),
            ("finished_at", lambda d: d.update(finished_at=None), "finished_at"),
            ("finished before", lambda d: d.update(finished_at="2026-10-10T14:00:00Z"), "finished_at"),
            ("host", lambda d: d.update(host=[]), "host"),
            ("host.entry", lambda d: d["host"].update(entry="sidebar"), "host.entry"),
            ("host.theme", lambda d: d["host"].update(theme="blue"), "host.theme"),
            ("routes", lambda d: d.update(routes=None), "routes"),
            ("route local", lambda d: d["routes"].update(local="x"), "routes.local"),
            ("origin path", lambda d: d["routes"]["local"].update(origin="http://127.0.0.1:47811/probe"),
             "routes.local.origin"),
            ("origin case", lambda d: d["routes"]["local"].update(origin="HTTP://127.0.0.1:47811"),
             "routes.local.origin"),
            ("origin port", lambda d: d["routes"]["local"].update(origin="http://127.0.0.1:70000"),
             "routes.local.origin"),
            ("selected type", lambda d: d["routes"]["remote"].update(selected="yes"), "routes.remote.selected"),
            ("selected without origin", lambda d: d["routes"]["remote"].update(origin=None), "routes.remote.origin"),
            ("checks", lambda d: d.update(checks={}), "checks"),
            ("check object", lambda d: d["checks"].__setitem__(0, "x"), "checks[0]"),
            ("unknown id", lambda d: d["checks"][i_ws].update(id="net.ws.moon"), "checks[%d].id" % i_ws),
            ("id not a string", lambda d: d["checks"][i_ws].update(id=["net.ws.local"]), "checks[%d].id" % i_ws),
            ("group", lambda d: d["checks"][i_ws].update(group="host"), "checks[%d].group" % i_ws),
            ("route", lambda d: d["checks"][i_ws].update(route="remote"), "checks[%d].route" % i_ws),
            ("status", lambda d: d["checks"][i_ws].update(status="running"), "checks[%d].status" % i_ws),
            ("observed_by", lambda d: d["checks"][i_ws].update(observed_by="model"),
             "checks[%d].observed_by" % i_ws),
            ("reason", lambda d: d["checks"][i_entry].update(reason=" "), "checks[%d].reason" % i_entry),
            ("error code", lambda d: d["checks"][i_ws].update(status="fail", error={"message": "x"}),
             "checks[%d].error.code" % i_ws),
            ("depends_on", lambda d: d["checks"][i_ws].update(depends_on="net.fetch.local"),
             "checks[%d].depends_on" % i_ws),
            ("check started_at", lambda d: d["checks"][i_ws].update(started_at="yesterday"),
             "checks[%d].started_at" % i_ws),
            ("duration", lambda d: d["checks"][i_ws].update(duration_ms=-1), "checks[%d].duration_ms" % i_ws),
            ("duration float", lambda d: d["checks"][i_ws].update(duration_ms=1.5), "checks[%d].duration_ms" % i_ws),
            ("summary", lambda d: d.update(summary=[]), "summary"),
            ("summary count", lambda d: d["summary"].update({"pass": 99}), "summary.pass"),
            ("summary type", lambda d: d["summary"].update(fail="0"), "summary.fail"),
            ("bearer text", lambda d: d["checks"][i_ws].update(status="fail", error={
                "code": "x", "message": "sent Bearer.abc"}), "checks[%d].error.message" % i_ws),
            ("token key", lambda d: d["host"].update(media_token="abc"), "host.media_token"),
            ("token key nested", lambda d: d["host"]["mcp"].update(Token="abc"), "host.mcp.Token"),
            ("authorization key", lambda d: d["checks"][0].update(value={"authorization": "x"}),
             "checks[0].value.authorization"),
        ]
        for name, fn, path in cases:
            with self.subTest(rule=name):
                data = mutate(fn)
                if name not in ("summary count", "summary type", "summary"):
                    if isinstance(data.get("checks"), list) and all(isinstance(c, dict) for c in data["checks"]):
                        recount(data)
                problems = PR.check_results(data, "f.json")
                self.assertIn(path, self.paths(problems), problems)
                self.assertTrue(all(p["file"] == "f.json" for p in problems))

    def test_top_level_must_be_an_object(self) -> None:
        self.assertEqual(self.paths(PR.check_results([1], "f.json")), ["$"])
        data = valid_results()
        data["checks"][0]["id"] = {"nested": True}
        self.assertEqual(PR.status_by_id(data).get("net.fetch.local"), None)
        self.assertNotIn("net.fetch.local", PR.index_by_id(data))
        self.assertEqual(PR.check_harness_profile(data, "f"), [])

    def test_missing_and_duplicate_ids(self) -> None:
        data = valid_results()
        dropped = data["checks"].pop(index_of(data, "bridge.media"))
        problems = PR.check_results(recount(data), "f.json")
        self.assertIn({"file": "f.json", "path": "checks", "problem": "missing check bridge.media"}, problems)

        data["checks"].append(copy.deepcopy(data["checks"][0]))
        data["checks"].append(dropped)
        problems = PR.check_results(recount(data), "f.json")
        dup = [p for p in problems if p["path"] == "checks[%d].id" % (len(data["checks"]) - 2)]
        self.assertEqual(len(dup), 1, problems)
        self.assertIn("duplicate id net.fetch.local (first at checks[0])", dup[0]["problem"])

    def test_cli_reports_problems_with_exit_1_text_and_json(self) -> None:
        data = valid_results()
        data["run_id"] = "bad"
        path = self.write("bad.json", data)
        code, payload = self.run_json("check", "--results", str(path))
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["problems"], [{"file": str(path), "path": "run_id",
                                                "problem": "must match probe-YYYYMMDD-HHMMSS-xxxx"}])
        self.assertEqual(payload["files"][0]["label"], "bad")  # default label = file stem
        code, out, _err = self.run_cli("check", "--results", str(path))
        self.assertEqual(code, 1)
        self.assertIn("%s: run_id: must match" % path, out)
        self.assertIn("FAILED", out)

    def test_several_results_in_one_flag_and_repeated_flags(self) -> None:
        a = self.write("a.json", valid_results())
        b = self.write("b.json", valid_results())
        c = self.write("c.json", valid_results())
        code, payload = self.run_json("check", "--results", "x=%s" % a, "y=%s" % b, "--results", str(c))
        self.assertEqual(code, 0, payload)
        self.assertEqual([f["label"] for f in payload["files"]], ["x", "y", "c"])


# ====================================================================== profiles

class ProfileTests(CheckerCase):
    def test_harness_profile_requires_pass_for_each_listed_check(self) -> None:
        for cid in PR.HARNESS_REQUIRED:
            with self.subTest(check=cid):
                data = valid_results()
                i = index_of(data, cid)
                data["checks"][i].update(status="not_exercised", reason="Needs x", value=None)
                recount(data)
                self.assertEqual(PR.check_results(data, "f"), [])
                problems = PR.check_harness_profile(data, "f")
                self.assertEqual(problems, [{"file": "f", "path": "checks[%d].status" % i,
                                             "problem": "harness profile requires pass for %s, got not_exercised"
                                             % cid}])

    def test_harness_profile_remote_rows_only_when_selected(self) -> None:
        data = valid_results()
        i = index_of(data, "net.ws.remote")
        data["checks"][i].update(status="fail", error={"code": "ws_closed_before_open", "message": "x", "clue": None})
        recount(data)
        self.assertEqual(self.paths(PR.check_harness_profile(data, "f")), ["checks[%d].status" % i])
        data = valid_results(remote_selected=False)
        self.assertEqual(PR.check_results(data, "f"), [])
        self.assertEqual(PR.check_harness_profile(data, "f"), [])
        # Manual and device rows are never required by the harness profile.
        self.assertFalse(set(PR.HARNESS_REQUIRED) & (MANUAL | DEVICES | {"bridge.context"}))

    def test_harness_profile_through_cli(self) -> None:
        data = valid_results()
        i = index_of(data, "media.decode.local")
        data["checks"][i].update(status="fail", error={"code": "decode_timeout", "message": "x"})
        path = self.write("r.json", recount(data))
        code, payload = self.run_json("check", "--profile", "harness", "--results", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(self.paths(payload["problems"]), ["checks[%d].status" % i])
        code, payload = self.run_json("check", "--results", str(path))
        self.assertEqual(code, 0, payload)

    def test_live_profile_screenshots_in_evidence_dir(self) -> None:
        good = self.write("live/results.json", valid_results())
        evidence = self.tmp / "live"
        code, payload = self.run_json("check", "--profile", "live", "--results", str(good),
                                      "--evidence-dir", str(evidence))
        self.assertEqual(code, 1)
        self.assertEqual(payload["problems"], [{"file": str(evidence), "path": "screenshots",
                                                "problem": "directory missing"}])
        shots = evidence / "screenshots"
        shots.mkdir()
        (shots / "chatgpt-global-narrow.png").write_bytes(b"png")
        (shots / "chatgpt-thread.txt").write_bytes(b"not a png")
        code, payload = self.run_json("check", "--profile", "live", "--results", str(good),
                                      "--evidence-dir", str(evidence))
        self.assertEqual(code, 1)
        self.assertEqual([p["problem"] for p in payload["problems"]],
                         ["no *.png whose name contains 'thread'"])
        (shots / "chatgpt-thread-wide.png").write_bytes(b"png")
        code, payload = self.run_json("check", "--profile", "live", "--results", str(good),
                                      "--evidence-dir", str(evidence))
        self.assertEqual(code, 0, payload)


# ====================================================================== doc and template

class DocTests(CheckerCase):
    def labelled(self, files: Dict[str, Dict[str, Any]]) -> List[str]:
        args: List[str] = []
        for label, data in files.items():
            args += ["--results", "%s=%s" % (label, self.write("%s.json" % label, data))]
        return args

    def all_labels(self) -> Dict[str, Dict[str, Any]]:
        results = {label: valid_results() for label in PR.DOC_LABELS}
        failing = results["chatgpt-global"]
        i = index_of(failing, "net.ws.local")
        failing["checks"][i].update(status="fail", error={"code": "ws_closed_before_open", "message": "x"})
        recount(failing)
        return results

    def test_doc_that_matches_every_file_passes(self) -> None:
        results = self.all_labels()
        doc = self.write("doc.md", filled_doc(results, values=True))
        code, payload = self.run_json("check", "--profile", "live", "--doc", str(doc), *self.labelled(results))
        self.assertEqual(code, 0, payload)
        self.assertEqual([f["label"] for f in payload["files"]], list(PR.DOC_LABELS))
        self.assertEqual(payload["doc"], str(doc))

    def test_doc_status_mismatch_names_id_and_label(self) -> None:
        results = self.all_labels()
        text = filled_doc(results)
        wrong = dict(results)
        data = valid_results()  # chatgpt-global file says pass for net.ws.local; the doc says fail
        wrong["chatgpt-global"] = data
        problems = PR.check_doc(text, "doc.md", [(k, k, v) for k, v in wrong.items()])
        self.assertEqual(problems, [{"file": "doc.md", "path": "matrix[net.ws.local][chatgpt-global]",
                                     "problem": "doc says 'fail' but the results file says 'pass'"}])

    def test_doc_missing_row_duplicate_row_pending_and_bad_cell(self) -> None:
        results = self.all_labels()
        text = filled_doc(results)
        triples = [(k, k, v) for k, v in results.items()]
        lines = text.splitlines()
        row = next(n for n, line in enumerate(lines) if line.startswith("| `bridge.media`"))

        missing = "\n".join(lines[:row] + lines[row + 1:])
        self.assertIn({"file": "doc.md", "path": "matrix", "problem": "missing row for bridge.media"},
                      PR.check_doc(missing, "doc.md", triples))

        duplicate = "\n".join(lines[:row + 1] + [lines[row]] + lines[row + 1:])
        self.assertIn("matrix[bridge.media]", self.paths(PR.check_doc(duplicate, "doc.md", triples)))

        pending = text.replace(lines[row], lines[row].replace("| pass |", "| pending |", 1))
        problems = PR.check_doc(pending, "doc.md", triples)
        self.assertEqual([p["problem"] for p in problems], ["cell is still pending"])
        self.assertTrue(problems[0]["path"].startswith("matrix[bridge.media]["))

        # A column without a results file must not stay pending either.
        partial = PR.check_doc(text, "doc.md", triples[:1])
        self.assertEqual(partial, [])
        self.assertTrue(PR.check_doc(TEMPLATE, "doc.md", triples[:1]))

        bad = text.replace(lines[row], lines[row].replace("| pass |", "| passed |", 1))
        self.assertIn("must be pass, fail, not exercised or pending",
                      PR.check_doc(bad, "doc.md", triples)[0]["problem"])

    def test_doc_label_column_heading_and_secret_rules(self) -> None:
        results = self.all_labels()
        text = filled_doc(results)
        triples = [(k, k, v) for k, v in results.items()]
        problems = PR.check_doc(text, "doc.md", triples + [("results-chromium", "f", valid_results())])
        self.assertEqual(problems, [{"file": "doc.md", "path": "matrix.header",
                                     "problem": "no column for results label 'results-chromium'"}])
        renamed = text.replace("## 5. Installation requirements", "## 5. Install")
        self.assertEqual(self.paths(PR.check_doc(renamed, "doc.md", triples)), ["headings[5]"])
        extra = text + "\n## 9. Extra\n"
        self.assertEqual(self.paths(PR.check_doc(extra, "doc.md", triples)), ["headings[9]"])
        fenced = text + "\n```\n## 9. Inside a code fence\n```\n"
        self.assertEqual(PR.check_doc(fenced, "doc.md", triples), [])
        no_matrix = text.replace("| Check |", "Check |", 1)
        self.assertIn("matrix", self.paths(PR.check_doc(no_matrix, "doc.md", triples))[0])
        leaked = text.replace("## 6. Revised estimates\n", "## 6. Revised estimates\n\nbearer.abc\n")
        self.assertEqual(self.paths(PR.check_doc(leaked, "doc.md", triples)), ["line %d" % (
            leaked.splitlines().index("bearer.abc") + 1)])
        relabelled = text.replace("| harness-webkit |", "| webkit |", 1)
        self.assertIn("matrix.header", self.paths(PR.check_doc(relabelled, "doc.md", triples)))

    def test_doc_through_cli_with_mismatch_exit_1(self) -> None:
        results = self.all_labels()
        doc = self.write("doc.md", filled_doc(results))
        args = ["--results", "chatgpt-global=%s" % self.write("g.json", valid_results())]
        code, payload = self.run_json("check", "--doc", str(doc), *args)
        self.assertEqual(code, 1)
        self.assertEqual(self.paths(payload["problems"]), ["matrix[net.ws.local][chatgpt-global]"])

    def test_template_constant_is_the_committed_template(self) -> None:
        data = TEMPLATE.encode("utf-8")
        blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        self.assertEqual(blob, TEMPLATE_BLOB)

    def test_template_fixture_passes_check_template(self) -> None:
        template = self.write("template.md", TEMPLATE)
        code, payload = self.run_json("check-template", "--doc", str(template))
        self.assertEqual(code, 0, payload)
        code, out, _ = self.run_cli("check-template", "--doc", str(template))
        self.assertEqual(code, 0)
        self.assertIn("OK: template", out)
        text = TEMPLATE
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("| `")), 27)
        for cid in PR.CHECK_IDS:
            self.assertEqual(text.count("| `%s` |" % cid), 1, cid)

    def test_filled_doc_keeps_the_template_layout(self) -> None:
        # The committed doc is filled, so it is no longer a template, but it keeps every heading and row.
        problems = PR.check_template(DOC.read_text(encoding="utf-8"), "doc.md")
        self.assertTrue(problems)
        self.assertTrue(all("template cell must be pending" in p["problem"] for p in problems), problems)

    def test_template_rules(self) -> None:
        text = TEMPLATE
        filled = filled_doc({label: valid_results() for label in PR.DOC_LABELS})
        problems = PR.check_template(filled, "t.md")
        self.assertEqual(len(problems), 27 * 7)
        self.assertTrue(all("template cell must be pending" in p["problem"] for p in problems))
        lines = text.splitlines()
        row = next(n for n, line in enumerate(lines) if line.startswith("| `host.init`"))
        missing = "\n".join(lines[:row] + lines[row + 1:])
        self.assertEqual(PR.check_template(missing, "t.md"),
                         [{"file": "t.md", "path": "matrix", "problem": "missing row for host.init"}])
        wrong_labels = text.replace("| chatgpt-devices |", "| chatgpt-extra |", 1)
        self.assertEqual(self.paths(PR.check_template(wrong_labels, "t.md")), ["matrix.header"])
        no_heading = text.replace("## 8. Not exercised and why", "## 8. Not exercised")
        self.assertEqual(self.paths(PR.check_template(no_heading, "t.md")), ["headings[8]"])
        path = self.write("t.md", missing)
        code, payload = self.run_json("check-template", "--doc", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(self.paths(payload["problems"]), ["matrix"])


# ====================================================================== stub

class StubTests(CheckerCase):
    def test_stub_passes_check_and_refuses_to_overwrite(self) -> None:
        out = self.tmp / "results-chatgpt-global.json"
        code, payload = self.run_json("stub", "--reason", "The probe never loaded in ChatGPT", "--out", str(out),
                                      "--host-name", "ChatGPT", "--host-version", "26.930.31730")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["summary"], {"pass": 0, "fail": 0, "not_exercised": 27})
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertRegex(data["run_id"], r"^probe-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
        self.assertEqual(data["run_id"], payload["run_id"])
        self.assertEqual(data["host"]["name"], "ChatGPT")
        self.assertEqual([c["id"] for c in data["checks"]], list(PR.CHECK_IDS))
        self.assertTrue(all(c["status"] == "not_exercised" and c["observed_by"] == "probe"
                            and c["reason"] == "The probe never loaded in ChatGPT" for c in data["checks"]))
        code, check = self.run_json("check", "--profile", "live", "--results", "chatgpt-global=%s" % out)
        self.assertEqual(code, 0, check)
        code, check = self.run_json("check", "--profile", "harness", "--results", str(out))
        self.assertEqual(code, 1)  # a stub never satisfies the harness profile

        before = out.read_bytes()
        code, refused = self.run_json("stub", "--reason", "again", "--out", str(out))
        self.assertEqual(code, 1)
        self.assert_refusal(refused, "out_exists")
        self.assertEqual(refused["path"], str(out))
        self.assertEqual(out.read_bytes(), before)
        code, text_out, err = self.run_cli("stub", "--reason", "again", "--out", str(out))
        self.assertEqual((code, text_out), (1, ""))
        self.assertIn("(out_exists)", err)
        self.assertIn("hint:", err)

    def test_stub_refusals(self) -> None:
        out = self.tmp / "s.json"
        code, payload = self.run_json("stub", "--reason", "  ", "--out", str(out))
        self.assertEqual(code, 2)
        self.assert_refusal(payload, "invalid_arguments")
        code, payload = self.run_json("stub", "--reason", "x" * 301, "--out", str(out))
        self.assertEqual(code, 2)
        self.assert_refusal(payload, "invalid_arguments")
        code, payload = self.run_json("stub", "--reason", "sent bearer.abc", "--out", str(out))
        self.assertEqual(code, 1)
        self.assert_refusal(payload, "reason_contains_secret")
        code, payload = self.run_json("stub", "--reason", "ok", "--host-name", "Bearer.x", "--out", str(out))
        self.assertEqual(code, 1)
        self.assert_refusal(payload, "reason_contains_secret")
        self.assertFalse(out.exists())
        missing_dir = self.tmp / "nope" / "s.json"
        code, payload = self.run_json("stub", "--reason", "ok", "--out", str(missing_dir))
        self.assertEqual(code, 1)
        self.assert_refusal(payload, "out_unwritable")
        link = self.tmp / "link.json"
        link.symlink_to(self.tmp / "target-that-does-not-exist.json")
        code, payload = self.run_json("stub", "--reason", "ok", "--out", str(link))
        self.assertEqual(code, 1)
        self.assert_refusal(payload, "out_exists")
        self.assertFalse((self.tmp / "target-that-does-not-exist.json").exists())


# ====================================================================== usage errors

class UsageTests(CheckerCase):
    def test_unreadable_input_and_usage_errors_exit_2(self) -> None:
        good = self.write("good.json", valid_results())
        notjson = self.write("bad.json", "{not json")
        deep = self.write("deep.json", "[" * 200000)
        cases = [
            (("check", "--results", str(deep)), "results_unreadable"),
            (("check", "--results", str(self.tmp / "missing.json")), "results_unreadable"),
            (("check", "--results", str(notjson)), "results_unreadable"),
            (("check", "--results", str(good), "--doc", str(self.tmp / "missing.md")), "doc_unreadable"),
            (("check", "--results", str(good), "--evidence-dir", str(self.tmp)), "invalid_arguments"),
            (("check", "--profile", "harness", "--results", str(good), "--evidence-dir", str(self.tmp)),
             "invalid_arguments"),
            (("check", "--results", "a=%s" % good, "--results", "a=%s" % good), "invalid_arguments"),
            (("check", "--results", "a="), "invalid_arguments"),
            (("check",), "invalid_arguments"),
            (("check-template", "--doc", str(self.tmp / "missing.md")), "doc_unreadable"),
        ]
        for args, error_code in cases:
            with self.subTest(args=args):
                code, payload = self.run_json(*args)
                self.assertEqual(code, 2, payload)
                self.assert_refusal(payload, error_code)
        code, out, err = self.run_cli("check", "--results", str(self.tmp / "missing.json"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("(results_unreadable)", err)
        for args in (("check", "--profile", "ci", "--results", str(good)), (), ("stub", "--out", "x"),
                     ("check-template",)):
            with self.subTest(argparse=args):
                code, _out, err = self.run_cli(*args)
                self.assertEqual(code, 2)
                self.assertIn("usage:", err)

    def test_label_split(self) -> None:
        self.assertEqual(PR.split_label("harness-chromium=/x/r.json"), ("harness-chromium", "/x/r.json"))
        self.assertEqual(PR.split_label("/x/results-webkit.json"), ("results-webkit", "/x/results-webkit.json"))
        self.assertEqual(PR.split_label("/tmp/a=b/r.json"), ("r", "/tmp/a=b/r.json"))


# ====================================================================== drift against the page

@unittest.skipIf(NODE is None, "node is not on PATH")
class CoreAgreementTests(CheckerCase):
    def test_check_table_and_words_match_probe_core(self) -> None:
        script = (
            "const core = await import(process.argv[1]);"
            "console.log(JSON.stringify({checks: core.CHECKS.map((c) => [c.id, c.group, c.route, c.depends_on]),"
            " words: core.STATUS_WORDS, run: core.RUN_ID_PATTERN.source}));"
        )
        proc = subprocess.run([NODE, "--input-type=module", "-e", script, CORE.as_uri()], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, check=True)
        data = json.loads(proc.stdout)
        self.assertEqual([tuple(row[:3]) + (tuple(row[3]),) for row in data["checks"]], list(PR.CHECKS))
        for status in PR.STATUSES:
            self.assertEqual(data["words"][status], PR.STATUS_WORDS[status])
        self.assertEqual(data["run"], PR.RUN_ID.pattern)


if __name__ == "__main__":
    unittest.main()
