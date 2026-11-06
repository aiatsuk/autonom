#!/usr/bin/env python3
"""Live check of mock request conditions and response rewrite (VER-003).

Not part of the unit suite. It runs the real mitmdump with this checkout's
addon in front of a loopback HTTP server and drives curl through it:

    python3 tests/live/mock_rewrite_live.py [--evidence-dir DIR] [--keep]

Everything lives in one temporary directory (AUTONOM_HOME, the mock
registry, the flow store, the mitmproxy confdir), so the machine registry and
the user's sessions are never read or written. Only loopback ports are used;
no device and no internet.

Checks (each decided from curl's own output and the flow store):

- query: rule A (`*/items*`, query page=2, status 500) before rule B (status
  200); `?page=2` gets 500 from A and `?page=1` 200 from B.
- header: a rule with `X-Client: ios*` answers `ios-27` only; `android` and
  no header reach the upstream (its hit counter).
- body: a POST rule with `user.role=admin` and `items.0.qty=2` answers the
  numeric body only; `"qty":"2"` and a form body reach the upstream.
- gzip_rewrite: a rewrite rule (status 203, set header, remove ETag, json-set,
  json-remove) on a gzip JSON answer: the client gets 203, the header, no
  ETag, a gzip body whose Content-Length equals its raw length and which
  decodes to the edited JSON.
- text_replace: `--replace Free Pro` on a text answer.
- failed_edit: json-set on an HTML answer leaves it byte-identical, still
  applies the status, and records `rewrite.error: not_json`.
- delay: a rewrite rule with delay 1.5 s holds its answer at least 1.5 s
  longer than the upstream took, while a parallel unmatched request is not
  delayed.
- old_format: a respond rule written in the shape Autonom main ccf98bb
  stores (with a body file) is answered as before.
- rule_gone: a rewrite rule disabled while the upstream is still answering
  leaves the real answer untouched and records `rewrite.error: rule_gone`.

Prints one JSON object (`ok` and every check) and exits 0 when all passed,
1 otherwise. mitmdump and the upstream server are always stopped and the
temporary directory removed (unless --keep).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

PROFILE = {"user": {"plan": "free", "trial_ends": "2026-11-01", "name": "Ada"}}
UPSTREAM_SLOW_S = 1.0
DELAY_S = 1.5


class Upstream(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}
    lock = threading.Lock()
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D401 - keep the output one JSON object
        pass

    def _count(self) -> str:
        path = self.path.split("?", 1)[0]
        with self.lock:
            self.hits[path] = self.hits.get(path, 0) + 1
        return path

    def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self._count()
        if path == "/profile.gz":
            body = gzip.compress(json.dumps(PROFILE).encode())
            self._send(200, body, {"Content-Type": "application/json",
                                   "Content-Encoding": "gzip", "ETag": '"v1"'})
        elif path == "/text":
            self._send(200, b"Free plan. Free!", {"Content-Type": "text/plain"})
        elif path == "/html":
            self._send(200, b"<html><b>Free</b></html>", {"Content-Type": "text/html"})
        elif path == "/slow":
            time.sleep(UPSTREAM_SLOW_S)
            self._send(200, b'{"slow":true}', {"Content-Type": "application/json"})
        else:
            self._send(200, json.dumps({"upstream": True, "path": path}).encode(),
                       {"Content-Type": "application/json"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        path = self._count()
        self._send(200, json.dumps({"upstream": True, "path": path}).encode(),
                   {"Content-Type": "application/json"})


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class Curl:
    def __init__(self, proxy_port: int, upstream_port: int, work: Path) -> None:
        self.proxy = f"http://127.0.0.1:{proxy_port}"
        self.base = f"http://127.0.0.1:{upstream_port}"
        self.work = work
        self.count = 0
        self.env = {key: value for key, value in os.environ.items()
                    if key.lower() not in ("http_proxy", "https_proxy", "all_proxy",
                                           "no_proxy")}

    def argv(self, path: str, *, method: str = "GET", headers=(), data: str | None = None,
             tag: str) -> tuple[list[str], Path, Path]:
        self.count += 1
        body = self.work / f"{tag}-{self.count}.body"
        head = self.work / f"{tag}-{self.count}.head"
        argv = ["curl", "-sS", "--max-time", "20", "--noproxy", "", "-x", self.proxy,
                "-X", method, "-o", str(body), "-D", str(head), "-w", "%{http_code}"]
        for header in headers:
            argv += ["-H", header]
        if data is not None:
            argv += ["--data-binary", data]
        argv.append(self.base + path)
        return argv, body, head

    def run(self, path: str, **kwargs) -> dict:
        argv, body, head = self.argv(path, tag=kwargs.pop("tag", "req"), **kwargs)
        started = time.monotonic()
        done = subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=30)
        return self.result(done.returncode, done.stdout, done.stderr, body, head,
                           time.monotonic() - started)

    @staticmethod
    def result(code: int, stdout: str, stderr: str, body: Path, head: Path,
               seconds: float) -> dict:
        headers: dict[str, list[str]] = {}
        if head.exists():
            for line in head.read_text("latin-1").splitlines()[1:]:
                name, colon, value = line.partition(":")
                if colon:
                    headers.setdefault(name.strip().lower(), []).append(value.strip())
        return {"exit": code, "status": int(stdout) if stdout.strip().isdigit() else None,
                "body": body.read_bytes() if body.exists() else b"",
                "headers": headers, "seconds": round(seconds, 3), "stderr": stderr[-300:]}


def flows(net_dir: Path) -> list[dict]:
    path = net_dir / "flows.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def flow_for(net_dir: Path, path: str, index: int = -1) -> dict:
    matching = [flow for flow in flows(net_dir) if flow.get("path") == path]
    return matching[index] if matching else {}


def run_checks(work: Path) -> dict:
    os.environ["AUTONOM_HOME"] = str(work / "home")
    from autonom_lib.network import mocks, proxy  # noqa: PLC0415

    registry = work / "home" / "mocks"
    registry.mkdir(parents=True)
    net_dir = work / "net"
    net_dir.mkdir()
    rules: dict[str, str] = {}

    def add(name: str, **kwargs) -> None:
        rules[name] = mocks.add(registry=registry, **kwargs)["id"]

    add("A", url_glob="*/items*", query=[mocks.parse_query_arg("page=2")], status=500,
        body_text='{"rule":"A"}')
    add("B", url_glob="*/items*", status=200, body_text='{"rule":"B"}')
    add("header", url_glob="*/hdr", status=299,
        request_headers=[mocks.parse_header_condition("X-Client: ios*")],
        body_text='{"rule":"header"}')
    add("body", url_glob="*/echo", method="POST", status=298,
        body_json=[mocks.parse_path_value("user.role=admin"),
                   mocks.parse_path_value("items.0.qty=2")],
        body_text='{"rule":"body"}')
    add("gzip", url_glob="*/profile.gz", mode="rewrite", status=203,
        headers=mocks.parse_header_args(["X-Edited: yes"]), remove_headers=["ETag"],
        json_set=[mocks.parse_path_value('user.plan="pro"', "--json-set")],
        json_remove=["user.trial_ends"])
    add("text", url_glob="*/text", mode="rewrite", replace=[mocks.parse_replace(("Free", "Pro"))])
    add("failed", url_glob="*/html", mode="rewrite", status=299,
        json_set=[mocks.parse_path_value("a.b=1", "--json-set")])
    add("delay", url_glob="*/delayed", mode="rewrite", delay_s=DELAY_S,
        replace=[mocks.parse_replace(("upstream", "edited"))])
    add("gone", url_glob="*/slow", mode="rewrite", status=503)
    # A respond rule exactly as Autonom main ccf98bb stores it (no new fields).
    legacy_body = mocks.bodies_dir(registry) / "legacy.body"
    legacy_body.write_text('{"legacy":true}', encoding="utf-8")
    stored = mocks.load(registry)
    stored.append({"id": "m_legacy", "match": {"url_glob": "*/legacy", "method": None,
                                               "host": None, "ignore_query": False},
                   "response": {"status": 202, "headers": {"Content-Type": "application/json"},
                                "body_path": str(legacy_body)},
                   "enabled": True, "note": None, "created_at": "2026-10-01T00:00:00Z"})
    mocks.save(stored, registry)
    rules["old"] = "m_legacy"

    upstream_port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", upstream_port), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy_port = free_port()
    mitmdump = shutil.which("mitmdump")
    if not mitmdump:
        server.shutdown()
        return {"ok": False, "error": "mitmdump not on PATH", "checks": []}
    argv = proxy.build_argv(mitmdump, port=proxy_port, directory=net_dir,
                            confdir=work / "ca", capture_bodies=False,
                            mocks_file=mocks.registry_file(registry))
    log = open(work / "mitmdump.log", "wb")  # noqa: SIM115 - closed in finally
    process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    checks: list[dict] = []

    def check(name: str, ok: bool, **detail) -> None:
        checks.append({"name": name, "ok": bool(ok), **detail})

    try:
        if not wait_port(proxy_port, time.monotonic() + 30):
            return {"ok": False, "error": "mitmdump did not start",
                    "log": (work / "mitmdump.log").read_text("utf-8", "replace")[-2000:],
                    "checks": []}
        curl = Curl(proxy_port, upstream_port, work)

        # query
        page2 = curl.run("/items?page=2", tag="query")
        flow2 = flow_for(net_dir, "/items")
        page1 = curl.run("/items?page=1", tag="query")
        flow1 = flow_for(net_dir, "/items")
        check("query", page2["status"] == 500 and page1["status"] == 200
              and flow2.get("mock_id") == rules["A"] and flow1.get("mock_id") == rules["B"],
              statuses=[page2["status"], page1["status"]],
              mock_ids=[flow2.get("mock_id"), flow1.get("mock_id")])

        # header
        answered = curl.run("/hdr", headers=["X-Client: ios-27"], tag="header")
        other = curl.run("/hdr", headers=["X-Client: android"], tag="header")
        missing = curl.run("/hdr", tag="header")
        check("header", answered["status"] == 299 and other["status"] == 200
              and missing["status"] == 200 and Upstream.hits.get("/hdr") == 2
              and b'"upstream": true' in other["body"],
              statuses=[answered["status"], other["status"], missing["status"]],
              upstream_hits=Upstream.hits.get("/hdr", 0))

        # body
        json_header = ["Content-Type: application/json"]
        numeric = curl.run("/echo", method="POST", headers=json_header, tag="body",
                           data='{"user":{"role":"admin"},"items":[{"qty":2}]}')
        textual = curl.run("/echo", method="POST", headers=json_header, tag="body",
                           data='{"user":{"role":"admin"},"items":[{"qty":"2"}]}')
        form = curl.run("/echo", method="POST", tag="body", data="user.role=admin&qty=2")
        check("body", numeric["status"] == 298 and textual["status"] == 200
              and form["status"] == 200 and Upstream.hits.get("/echo") == 2,
              statuses=[numeric["status"], textual["status"], form["status"]],
              upstream_hits=Upstream.hits.get("/echo", 0))

        # gzip rewrite
        edited = curl.run("/profile.gz", headers=["Accept-Encoding: gzip"], tag="gzip")
        decoded = None
        try:
            decoded = json.loads(gzip.decompress(edited["body"]))
        except (OSError, ValueError):
            decoded = None
        length = (edited["headers"].get("content-length") or [None])[-1]
        record = flow_for(net_dir, "/profile.gz")
        check("gzip_rewrite", edited["status"] == 203
              and edited["headers"].get("x-edited") == ["yes"]
              and "etag" not in edited["headers"]
              and edited["headers"].get("content-encoding") == ["gzip"]
              and length is not None and int(length) == len(edited["body"])
              and decoded == {"user": {"plan": "pro", "name": "Ada"}}
              and record.get("upstream_status") == 200 and record.get("mock_kind") == "rewrite"
              and record.get("rewrite") == {"applied": ["status", "remove_headers", "headers",
                                                        "json_set", "json_remove"],
                                            "error": None},
              status=edited["status"], content_length=length, raw_bytes=len(edited["body"]),
              decoded=decoded, headers=sorted(edited["headers"]), record_rewrite=record.get(
                  "rewrite"), upstream_status=record.get("upstream_status"))

        # text replace
        text = curl.run("/text", tag="text")
        check("text_replace", text["status"] == 200 and text["body"] == b"Pro plan. Pro!"
              and (text["headers"].get("content-length") or [""])[-1] == "14",
              status=text["status"], body=text["body"].decode("utf-8", "replace"))

        # failed edit
        failed = curl.run("/html", tag="failed")
        record = flow_for(net_dir, "/html")
        check("failed_edit", failed["status"] == 299
              and failed["body"] == b"<html><b>Free</b></html>"
              and (record.get("rewrite") or {}).get("error") == "not_json"
              and record.get("upstream_status") == 200,
              status=failed["status"], body=failed["body"].decode("utf-8", "replace"),
              record_rewrite=record.get("rewrite"))

        # delay, with a parallel unmatched request
        slow_argv, slow_body, slow_head = curl.argv("/delayed", tag="delay")
        fast_argv, fast_body, fast_head = curl.argv("/fast", tag="delay")
        started = time.monotonic()
        slow_proc = subprocess.Popen(slow_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=curl.env)
        time.sleep(0.1)
        fast_started = time.monotonic()
        fast_proc = subprocess.Popen(fast_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=curl.env)
        fast_out, fast_err = fast_proc.communicate(timeout=30)
        fast_seconds = time.monotonic() - fast_started
        slow_out, slow_err = slow_proc.communicate(timeout=30)
        slow_seconds = time.monotonic() - started
        slow = Curl.result(slow_proc.returncode, slow_out, slow_err, slow_body, slow_head,
                           slow_seconds)
        fast = Curl.result(fast_proc.returncode, fast_out, fast_err, fast_body, fast_head,
                           fast_seconds)
        record = flow_for(net_dir, "/delayed")
        check("delay", slow["status"] == 200 and b"edited" in slow["body"]
              and slow_seconds >= DELAY_S and fast["status"] == 200
              and fast_seconds < 1.0 and fast_seconds < slow_seconds - 1.0
              and (record.get("duration_ms") or 0) >= DELAY_S * 1000,
              delayed_seconds=round(slow_seconds, 3), parallel_seconds=round(fast_seconds, 3),
              delayed_duration_ms=record.get("duration_ms"))

        # old format respond rule
        legacy = curl.run("/legacy", tag="old")
        record = flow_for(net_dir, "/legacy")
        check("old_format", legacy["status"] == 202 and legacy["body"] == b'{"legacy":true}'
              and record.get("mock_id") == "m_legacy" and record.get("mock_kind") == "respond",
              status=legacy["status"], body=legacy["body"].decode("utf-8", "replace"))

        # rule disabled while the upstream answers
        gone_argv, gone_body, gone_head = curl.argv("/slow", tag="gone")
        gone_proc = subprocess.Popen(gone_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=curl.env)
        time.sleep(UPSTREAM_SLOW_S / 3)
        mocks.set_enabled(rules["gone"], False, registry=registry)
        gone_out, gone_err = gone_proc.communicate(timeout=30)
        gone = Curl.result(gone_proc.returncode, gone_out, gone_err, gone_body, gone_head, 0)
        record = flow_for(net_dir, "/slow")
        check("rule_gone", gone["status"] == 200 and gone["body"] == b'{"slow":true}'
              and (record.get("rewrite") or {}).get("error") == "rule_gone",
              status=gone["status"], record_rewrite=record.get("rewrite"))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
        server.shutdown()
        server.server_close()
    stopped = process.poll() is not None
    check("cleanup", stopped, mitmdump_exit=process.returncode)
    return {"ok": all(item["ok"] for item in checks), "checks": checks,
            "mitmdump": mitmdump}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary directory (its path is printed)")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="autonom-mock-rewrite-"))
    try:
        result = run_checks(work)
    except Exception as exc:  # noqa: BLE001 - report, never leave a traceback only
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "checks": []}
    finally:
        if args.keep:
            print(f"kept {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)
    result["temp_removed"] = not work.exists()
    text = json.dumps(result, ensure_ascii=False, default=str)
    if args.evidence_dir:
        args.evidence_dir.mkdir(parents=True, exist_ok=True)
        (args.evidence_dir / "mock-rewrite-live.json").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
