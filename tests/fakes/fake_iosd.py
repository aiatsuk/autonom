#!/usr/bin/env python3
"""Deterministic stand-in for the iOS device daemon (`ios-device-daemon.mjs`).

It speaks the daemon's protocol — one JSON request per line, one JSON reply
per line with the same `id` — on a private Unix socket, and writes the
daemon's registry (`<state base>/iosd/<UDID>.json`, schema `autonom-iosd/v1`)
so the CLI finds it the way it finds a real one. Every request line it reads
is appended to the log the config names: that log is the oracle of what the
CLI sent the daemon, beside the fake idb's argv log.

Two ways to run it:

    fake_iosd.py serve --udid U --state-base B --config C.json
    fake_iosd.py <anything>.mjs --udid U --state-base B   (as AUTONOM_NODE)

The second is how a test lets the CLI *start* it: with AUTONOM_NODE pointing
here, the CLI's `node ios-device-daemon.mjs --udid U --state-base B` runs this
fake, which reads its config from AUTONOM_FAKE_IOSD_CONFIG. Either way it
prints the daemon's one status line on stdout once it serves (or refuses).

Config keys (all optional):

- ``log``: file the request lines are appended to (one JSON object a line);
- ``tree_file``: the describe-all JSON a `tree` (and `act`) read returns;
  ``ax_backend`` (default ``ax``) names the backend it reports;
- ``status``: ``serving`` (default), ``unsupported`` (every op but status
  and stop fails with ``companion_too_old``) or ``failed`` (prints a failed
  status line and exits 2 without serving);
- ``companion_pid``: the companion pid the registry lists;
  ``companion_owned`` (default true) is its ``owned`` flag (false: a
  companion the daemon adopted and never stops);
- ``delay_on``: op -> seconds the fake waits after reading that op's line
  and before it answers (a daemon that accepted the request and is stuck);
- ``close_on``: ops whose line is read and then the connection closed
  without a reply (a daemon that died after the request was written);
- ``replies``: op -> reply fields merged over the default reply
  (``{"ok": false, "error": {...}}`` makes it a failure);
- ``max_seconds``: the fake exits after this long whatever happens
  (default 120), so a test that forgets it leaves nothing behind.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = "autonom-iosd/v1"


def parse(argv: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {"config": os.environ.get("AUTONOM_FAKE_IOSD_CONFIG")}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--udid", "--state-base", "--config", "--idle-ms") and index + 1 < len(argv):
            options[arg[2:].replace("-", "_")] = argv[index + 1]
            index += 2
        else:
            index += 1  # `serve`, or the daemon script path when run as node
    return options


class Fake:
    def __init__(self, udid: str, base: Path, config: dict[str, Any]) -> None:
        self.udid = udid
        self.base = base
        self.config = config
        self.dir = Path(tempfile.mkdtemp(prefix="fiosd-"))
        os.chmod(self.dir, 0o700)
        self.socket = str(self.dir / "d.sock")
        self.registry = base / "iosd" / f"{udid}.json"
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.state = config.get("status") or "serving"

    def log(self, request: Any) -> None:
        path = self.config.get("log")
        if not path:
            return
        with self.lock, open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(request) + "\n")

    def record(self) -> dict[str, Any]:
        companion_pid = self.config.get("companion_pid")
        return {
            "schema": SCHEMA, "udid": self.udid, "pid": os.getpid(), "socket": self.socket,
            "state": self.state,
            **({"reason": "companion_too_old", "companion_version": "1.1.8"}
               if self.state == "unsupported" else {}),
            "companion": ({"pid": int(companion_pid), "socket": None,
                           "owned": bool(self.config.get("companion_owned", True))}
                          if companion_pid else None),
            "versions": {"xcode_path": None, "xcode": None, "coresimulator": None,
                         "companion": "1.1.8" if self.state == "unsupported" else "1.6.4"},
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "idle_ms": 300000,
        }

    def tree_json(self) -> str:
        path = self.config.get("tree_file")
        return Path(path).read_text(encoding="utf-8") if path else "[]"

    def reply(self, request: dict[str, Any]) -> dict[str, Any] | None:
        op = request.get("op")
        if op in (self.config.get("close_on") or ()):
            return None
        if self.state == "unsupported" and op not in ("status", "stop"):
            return {"ok": False, "error": {"code": "companion_too_old", "stage": "before_send",
                                           "message": "idb_companion 1.1.8 is older than 1.6.4",
                                           "version": "1.1.8"}}
        backend = self.config.get("ax_backend") or "ax"
        defaults: dict[str, dict[str, Any]] = {
            "status": {"ok": True, **self.record(), "status": self.state},
            "stop": {"ok": True, "stopping": True},
            "tree": {"ok": True, "json": self.tree_json(), "ax_backend": backend, "coverage": 0.9,
                     "reads": 1},
            "quiet": {"ok": True, "state": "quiet", "busy_signals": [], "waited_ms": 260,
                      "timed_out": False, "method": "quiet-stream"},
            "act": {"ok": True, "input_confirmed": True, "confirmed_by": "touches_completed",
                    "method": "quiet-stream", "settled": True, "waited_ms": 320, "warnings": [],
                    "json": self.tree_json(), "ax_backend": backend, "coverage": 0.9, "reads": 1},
        }
        base = defaults.get(op, {"ok": True, "sent": True})
        override = (self.config.get("replies") or {}).get(op) or {}
        if override.get("ok") is False:
            return dict(override)  # a failure carries only what the test gave it
        return {**base, **override}

    def serve(self) -> None:
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for raw in self.rfile:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                    except ValueError:
                        request = {"raw": line}
                    fake.log(request)
                    if not isinstance(request, dict) or "op" not in request:
                        self.wfile.write((json.dumps({"id": None, "ok": False, "error": {
                            "code": "invalid_request", "stage": "before_send",
                            "message": "not a request"}}) + "\n").encode())
                        continue
                    delay = (fake.config.get("delay_on") or {}).get(request.get("op"))
                    if delay:
                        fake.stopping.wait(float(delay))  # a daemon that is stuck
                    answer = fake.reply(request)
                    if answer is None:
                        return  # close without answering
                    answer["id"] = request.get("id")
                    self.wfile.write((json.dumps(answer) + "\n").encode())
                    self.wfile.flush()
                    if request.get("op") == "stop":
                        fake.stopping.set()
                        return

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        server = Server(self.socket, Handler)
        os.chmod(self.socket, 0o600)
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(self.record(), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps({"status": self.state, **self.record()}), flush=True)
        deadline = time.monotonic() + float(self.config.get("max_seconds") or 120)
        try:
            while not self.stopping.is_set() and time.monotonic() < deadline:
                self.stopping.wait(0.1)
        finally:
            server.shutdown()
            server.server_close()
            self.cleanup()

    def cleanup(self) -> None:
        try:
            if json.loads(self.registry.read_text(encoding="utf-8")).get("pid") == os.getpid():
                self.registry.unlink()
        except (OSError, ValueError):
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    options = parse(argv)
    config_path = options.get("config")
    config = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
    udid = options.get("udid")
    base = options.get("state_base")
    if not udid or not base:
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error": {
            "code": "daemon_start_failed", "reason": "bad_arguments",
            "message": "--udid and --state-base are required"}}), flush=True)
        return 2
    if config.get("status") == "failed":
        print(json.dumps({"schema": SCHEMA, "status": "failed", "error": {
            "code": "daemon_start_failed", "reason": "start_error",
            "message": "the fake daemon was told to fail"}}), flush=True)
        return 2
    fake = Fake(udid, Path(base), config)
    signal.signal(signal.SIGTERM, lambda *_: fake.stopping.set())
    fake.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
