"""Autonom mitmproxy addon — records flows and applies mock rules.

**This file is loaded by mitmproxy's own Python interpreter**, which on a typical
install is a self-contained binary bundle with a different version from the one
running the rest of Autonom. It therefore:

- imports nothing from `autonom_lib` (there is no import path to it);
- uses only the standard library plus mitmproxy's public addon API;
- takes all configuration through mitmproxy options (`--set autonom_dir=...`);
- keeps its pure helpers importable **without** mitmproxy, so the repository's
  own test suite can exercise them directly.

The redaction table below is a by-value copy of `redact.REDACTED_HEADERS`; a unit
test asserts the two never diverge.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

REDACTED_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
})
PLACEHOLDER = "<redacted>"
PREVIEW_LIMIT = 2048
TRUNCATION_MARKER = "…[truncated]"

SENSITIVE_FIELDS = (
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "id_token", "api_key", "apikey", "client_secret", "authorization",
    "session_key", "private_key", "credential", "otp", "pin",
)


# --- pure helpers (importable without mitmproxy) ------------------------------


def redact_headers(pairs) -> dict:
    items = pairs.items() if hasattr(pairs, "items") else pairs
    result = {}
    for name, value in items:
        key = str(name).lower()
        result[key] = PLACEHOLDER if key in REDACTED_HEADERS else str(value)
    return result


def _scrub_json(value):
    if isinstance(value, dict):
        return {
            key: (PLACEHOLDER if str(key).lower() in SENSITIVE_FIELDS else _scrub_json(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_json(item) for item in value]
    return value


def scrub_body(text):
    """Mask credential fields in a body preview before it reaches disk."""
    import re

    if not text:
        return text
    if text.lstrip()[:1] in "{[":
        try:
            return json.dumps(_scrub_json(json.loads(text)), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    names = "|".join(SENSITIVE_FIELDS)
    text = re.sub(r'(?i)("(?:' + names + r')"\s*:\s*)"(?:[^"\\]|\\.)*"',
                  r'\1"' + PLACEHOLDER + '"', text)
    return re.sub(r'(?i)\b((?:' + names + r')=)[^&\s]+', r'\1' + PLACEHOLDER, text)


def preview(body, limit: int = PREVIEW_LIMIT):
    if body is None:
        return None
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = scrub_body(text)
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER


def glob_match(pattern: str, value: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(value, pattern)


def rule_matches(rule: dict, method: str, url: str, host: str) -> bool:
    """First-enabled-wins matching on url glob, optional method, optional host.

    `ignore_query` (set by `--url`) compares against the query-stripped URL, so
    `…/update/12341` also matches `…/update/12341?ts=1699`. It is opt-in per
    rule: a hand-written `--match` glob keeps exact control over the query.
    """
    if not rule.get("enabled", True):
        return False
    match = rule.get("match") or {}
    wanted_method = match.get("method")
    if wanted_method and wanted_method.upper() != (method or "").upper():
        return False
    wanted_host = match.get("host")
    if wanted_host and wanted_host.lower() != (host or "").lower():
        return False
    url_glob = match.get("url_glob")
    if url_glob:
        candidate = url or ""
        if match.get("ignore_query"):
            candidate = candidate.split("?", 1)[0]
        if not glob_match(url_glob, candidate):
            return False
    return True


def select_rule(rules, method: str, url: str, host: str):
    for rule in rules or []:
        if rule_matches(rule, method, url, host):
            return rule
    return None


def flow_id(counter: int) -> str:
    return "f_%04d" % counter


# --- addon --------------------------------------------------------------------


class AutonomRecorder:
    """Append every flow to JSONL, and short-circuit requests that match a mock."""

    def __init__(self) -> None:
        self.directory = ""
        self.mocks_path = ""
        self.capture_bodies = False
        self.counter = 0
        self._rules: list = []
        self._rules_mtime: float | None = None
        self._rules_warned = False

    # -- mitmproxy hooks --

    def load(self, loader) -> None:
        loader.add_option(
            name="autonom_dir", typespec=str, default="",
            help="Directory for flows.jsonl and bodies/",
        )
        loader.add_option(
            name="autonom_mocks", typespec=str, default="",
            help="Path to the persistent mock registry.json (falls back to "
                 "<autonom_dir>/mocks.json when empty)",
        )
        loader.add_option(
            name="autonom_capture_bodies", typespec=bool, default=False,
            help="Persist full request/response bodies (off by default)",
        )

    def configure(self, updates) -> None:
        from mitmproxy import ctx  # noqa: PLC0415 - only available inside mitmproxy

        if "autonom_dir" in updates:
            self.directory = ctx.options.autonom_dir or ""
            if self.directory:
                os.makedirs(self.directory, mode=0o700, exist_ok=True)
        if "autonom_mocks" in updates:
            self.mocks_path = ctx.options.autonom_mocks or ""
        if "autonom_capture_bodies" in updates:
            self.capture_bodies = bool(ctx.options.autonom_capture_bodies)

    def request(self, flow) -> None:
        rule = select_rule(
            self._load_rules(), flow.request.method, flow.request.pretty_url, flow.request.host
        )
        if not rule:
            return
        from mitmproxy import http  # noqa: PLC0415

        response = rule.get("response") or {}
        body = b""
        body_path = response.get("body_path")
        if body_path:
            resolved = body_path if os.path.isabs(body_path) else os.path.join(
                self.directory or ".", os.path.relpath(body_path, "network")
                if body_path.startswith("network/") else body_path
            )
            try:
                with open(resolved, "rb") as handle:
                    body = handle.read()
            except OSError:
                body = b""
        flow.response = http.Response.make(
            int(response.get("status", 200)),
            body,
            {str(k): str(v) for k, v in (response.get("headers") or {}).items()},
        )
        flow.metadata["autonom_mock_id"] = rule.get("id")

    def response(self, flow) -> None:
        if not self.directory:
            return
        self.counter += 1
        identifier = flow_id(self.counter)
        started = getattr(flow.request, "timestamp_start", None) or time.time()
        ended = getattr(flow.response, "timestamp_end", None) or time.time()

        record = {
            "id": identifier,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            # Millisecond boundaries let a flow report correlate requests to
            # individual steps. Keep the ISO field for human/CLI compatibility.
            "started_at_ms": int(started * 1000),
            "finished_at_ms": int(ended * 1000),
            "method": flow.request.method,
            "url": flow.request.pretty_url,
            "host": flow.request.host,
            "path": flow.request.path.split("?", 1)[0],
            "status": flow.response.status_code,
            "duration_ms": max(0, int((ended - started) * 1000)),
            "request_headers_preview": redact_headers(flow.request.headers.items()),
            "response_headers_preview": redact_headers(flow.response.headers.items()),
            "request_body_preview": preview(flow.request.content),
            "response_body_preview": preview(flow.response.content),
            "mocked": bool(flow.metadata.get("autonom_mock_id")),
            "mock_id": flow.metadata.get("autonom_mock_id"),
            "sizes": {
                "request_bytes": len(flow.request.content or b""),
                "response_bytes": len(flow.response.content or b""),
            },
        }

        if self.capture_bodies:
            bodies = os.path.join(self.directory, "bodies")
            os.makedirs(bodies, mode=0o700, exist_ok=True)
            for suffix, content in (("req", flow.request.content), ("res", flow.response.content)):
                if content:
                    path = os.path.join(bodies, "%s.%s" % (identifier, suffix))
                    with open(path, "wb") as handle:
                        handle.write(content)
                    os.chmod(path, 0o600)

        self._append(record)

    # -- internals --

    def _append(self, record: dict) -> None:
        path = os.path.join(self.directory, "flows.jsonl")
        existed = os.path.exists(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if not existed:
            os.chmod(path, 0o600)

    def _rules_file(self) -> str:
        """The persistent registry when configured, else the legacy session file."""
        if self.mocks_path:
            return self.mocks_path
        if not self.directory:
            return ""
        return os.path.join(self.directory, "mocks.json")

    def _load_rules(self) -> list:
        """Reload the rules file when its mtime changes; keep the last good set."""
        path = self._rules_file()
        if not path:
            return []
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._rules = []
            self._rules_mtime = None
            return self._rules
        if mtime == self._rules_mtime:
            return self._rules
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            # A half-written or invalid file must not drop the active rules;
            # warn once rather than on every request.
            if not self._rules_warned:
                self._rules_warned = True
                try:
                    from mitmproxy import ctx

                    ctx.log.warn("autonom: the mock registry is unreadable; "
                                 "keeping the last good rules")
                except Exception:  # noqa: BLE001
                    pass
            return self._rules
        self._rules = payload.get("mocks", []) if isinstance(payload, dict) else list(payload)
        self._rules_mtime = mtime
        self._rules_warned = False
        return self._rules


def _make_addons() -> list:
    return [AutonomRecorder()]


try:  # pragma: no cover - only true inside mitmproxy
    import mitmproxy  # noqa: F401

    addons = _make_addons()
except ImportError:  # pragma: no cover - importing for unit tests
    addons: list[Any] = []
