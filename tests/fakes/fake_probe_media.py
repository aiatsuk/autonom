#!/usr/bin/env python3
"""Stand-in for ``P/media/probe-media.mjs`` used by the ChatGPT probe server tests.

Started by the server as ``<node> fake_probe_media.py --port N ...`` (tests
set ``AUTONOM_PROBE_NODE`` to the Python interpreter). The token comes from
``$AUTONOM_PROBE_TOKEN`` exactly as for the real endpoint. ``$FAKE_PROBE_MEDIA_MODE``:

``ok``           (default) listen on 127.0.0.1, print the ready line, serve
                 ``GET /probe/status`` and ``GET /probe/frames`` with canned data
                 behind ``Authorization: Bearer <token>``; exit on stdin EOF
``port_in_use``  print ``{"event":"error","error_code":"media_port_in_use"}``, exit 3
``silent``       print nothing and wait to be killed
``noisy``        write 50 long stderr lines, then print nothing and wait to be killed
``crash``        write a diagnostic that includes the token to stderr and exit 1
                 (the server must redact it from the reason it reports)
``junk``         print a first line that is not the ready event, then wait

``$FAKE_PROBE_MEDIA_FRAMES=error`` makes ``/probe/frames`` answer 503 ``busy``.

``$FAKE_PROBE_MEDIA_PAD=N`` (optional) changes ``crash`` and ``junk``: the
stderr line (``crash``) or the first stdout line (``junk``) becomes ``N``
letters ``y`` followed directly by the token, so a server that cuts the line
before redacting it would leak a token prefix.

``$FAKE_PROBE_MEDIA_LOG`` (optional) gets one JSON line per start:
``{"pid": N, "mode": ..., "argv": [...], "token_len": N}`` (never the token).
"""
from __future__ import annotations

import base64
import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCHEMA = "autonom.chatgpt-probe.media/v1"
STARTED = time.time()
CONFIG = b"\x00\x00\x00\x01\x67\x42\xc0\x1e\x00\x00\x00\x01\x68\xce\x3c\x80"


def arg(argv: list, name: str, default: str = "") -> str:
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


def frame_bytes(seq: int) -> bytes:
    return b"\x00\x00\x00\x01\x65" + seq.to_bytes(4, "big") + bytes(range(23))


class Server(ThreadingHTTPServer):
    """HTTPServer.server_bind looks up the host name with socket.getfqdn, a reverse DNS
    query that can take tens of seconds on a CI runner: longer than the probe server
    waits for the ready line. Nothing here reads the name, so skip the lookup."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *_args) -> None:  # keep stderr quiet
        return

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.headers.get("Authorization") != "Bearer " + self.token:
            self._send(401, {"ok": False, "error_code": "unauthorized"})
            return
        url = urlparse(self.path)
        if url.path == "/probe/status":
            self._send(200, {
                "ok": True, "schema_version": SCHEMA, "uptime_ms": int((time.time() - STARTED) * 1000),
                "clients": 1, "sources": [{"id": "fixture", "subscribers": 1}],
                "refused_origins": [{"origin": "https://x.example", "count": 2,
                                     "last_at": "2026-10-10T14:22:33Z"}],
            })
            return
        if url.path == "/probe/frames" and os.environ.get("FAKE_PROBE_MEDIA_FRAMES") == "error":
            self._send(503, {"ok": False, "error_code": "busy"})
            return
        if url.path == "/probe/frames":
            query = parse_qs(url.query)
            try:
                count = int(query.get("count", ["10"])[0])
                after = int(query.get("after_seq", ["-1"])[0])
            except ValueError:
                self._send(400, {"ok": False, "error_code": "bad_request"})
                return
            if not 1 <= count <= 30 or after < -1:
                self._send(400, {"ok": False, "error_code": "bad_request"})
                return
            frames = [{"seq": seq, "key": True, "t_server_ms": time.time() * 1000.0,
                       "data_b64": base64.b64encode(frame_bytes(seq)).decode("ascii")}
                      for seq in range(after + 1, after + 1 + count)]
            self._send(200, {"ok": True, "schema_version": SCHEMA, "codec": "avc1.42C01E",
                             "width": 96, "height": 208,
                             "config_b64": base64.b64encode(CONFIG).decode("ascii"), "frames": frames})
            return
        self._send(404, {"ok": False, "error_code": "not_found"})


def main(argv: list) -> int:
    mode = os.environ.get("FAKE_PROBE_MEDIA_MODE", "ok")
    token = os.environ.get("AUTONOM_PROBE_TOKEN", "")
    log = os.environ.get("FAKE_PROBE_MEDIA_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "mode": mode, "argv": argv,
                                     "token_len": len(token)}) + "\n")
    if mode == "port_in_use":
        print(json.dumps({"event": "error", "error_code": "media_port_in_use"}), flush=True)
        return 3
    pad = int(os.environ.get("FAKE_PROBE_MEDIA_PAD") or -1)
    if mode == "junk":
        print("starting up..." if pad < 0 else "y" * pad + token, flush=True)
    if mode == "noisy":
        for index in range(50):
            sys.stderr.write("warn: line %d %s\n" % (index, "noise " * 20))
        sys.stderr.flush()
    if mode in ("silent", "noisy", "junk"):
        while True:
            time.sleep(60)
    if mode == "crash":
        if pad < 0:
            sys.stderr.write("fake media crashed while holding token %s\n" % token)
        else:
            sys.stderr.write("y" * pad + token + "\n")
        sys.stderr.flush()
        return 1
    if len(token) < 32:
        sys.stderr.write(json.dumps({"ok": False, "error_code": "token_missing"}) + "\n")
        return 2
    Handler.token = token
    server = Server(("127.0.0.1", int(arg(argv, "--port", "0"))), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({"event": "ready", "port": server.server_address[1], "pid": os.getpid(),
                      "version": "0.1.0"}), flush=True)
    sys.stdin.buffer.read()  # the server keeps stdin open; EOF means stop
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
