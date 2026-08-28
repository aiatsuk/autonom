"""Versioned, provider-neutral contracts shared by reports, replay, and CI.

The module intentionally uses only Python's standard library.  It is the
compatibility seam between today's Flow v1/manifest v3 executor and the
Report Model v2 described by the product blueprint: old manifests can be
compiled into the new model without being rewritten in place.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

EVENT_SCHEMA = "autonom.event/v1"
REPORT_SCHEMA = "autonom.report/v2"
REPLAY_SCHEMA = "autonom.replay/v1"
PROVIDER_SCHEMA = "autonom.provider/v1"
BUNDLE_SCHEMA = "autonom.bundle/v2"

EXECUTION_STATUSES = ("passed", "failed", "broken", "skipped", "unknown")
PROOF_VERDICTS = (
    "pass", "fail", "not_covered", "blocked", "inconclusive", "not_applicable",
)


def utc_now() -> str:
    now = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{base}.{int((now % 1) * 1000):03d}Z"


def stable_id(prefix: str, *parts: object, size: int = 16) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:size]}"


def fresh_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SAFE_PARAM = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,200}$")
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|"
    r"private[_-]?key|credential|access[_-]?token|refresh[_-]?token|otp|pin)$")


def redact_value(value: Any, key: str | None = None) -> Any:
    """Defence-in-depth redaction before any derived artifact is persisted."""
    if key and _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(name): redact_value(item, str(name))
                for name, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


def history_id(app_id: str | None, flow_id: str | None,
               parameters: dict[str, Any] | None = None,
               safe_names: Iterable[str] = ()) -> str:
    """Stable test identity containing declared, low-risk business params only."""
    allowed = set(safe_names)
    selected: list[tuple[str, str]] = []
    for name, value in sorted((parameters or {}).items()):
        rendered = str(value)
        if name in allowed and _SAFE_PARAM.fullmatch(rendered):
            selected.append((name, rendered))
    return stable_id("hist", app_id or "unknown-app", flow_id or "unknown-flow",
                     canonical_json(selected).decode("utf-8"))


def execution_status(manifest_status: str | None,
                     primary_error: dict[str, Any] | None = None) -> str:
    if manifest_status in ("passed", "replayed"):
        return "passed"
    if manifest_status == "skipped":
        return "skipped"
    if manifest_status == "failed":
        failure_class = (primary_error or {}).get("failure_class")
        return "failed" if failure_class in (None, "test_failure") else "broken"
    return "unknown"


def proof_verdict(status: str, *, covered: bool = True,
                  blocked: bool = False) -> str:
    if blocked:
        return "blocked"
    if not covered:
        return "not_covered"
    if status == "passed":
        return "pass"
    if status == "failed":
        return "fail"
    if status == "skipped":
        return "not_applicable"
    return "inconclusive"


@dataclass(frozen=True)
class Capability:
    name: str
    state: str  # available | unavailable | degraded | unknown
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.state in ("available", "degraded")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "state": self.state}
        if self.reason:
            payload["reason"] = self.reason
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class CapabilitySnapshot:
    provider: str
    target_id: str
    device_class: str
    captured_at: str
    capabilities: tuple[Capability, ...]
    schema: str = PROVIDER_SCHEMA

    def supports(self, name: str) -> bool:
        return any(item.name == name and item.available
                   for item in self.capabilities)

    def missing(self, required: Iterable[str]) -> list[str]:
        return sorted(name for name in required if not self.supports(name))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "provider": self.provider,
            "target_id": self.target_id,
            "device_class": self.device_class,
            "captured_at": self.captured_at,
            "capabilities": [item.as_dict() for item in self.capabilities],
        }
