"""Credential redaction and preview truncation (CAP-NET-004, INV-03).

Redaction happens **before the first write**, not before display. A MITM proxy
reads credentials by construction, and `.autonom/` is a working directory a user
may later archive or attach to a bug report — so an artifact that has never
contained a token is the only safe artifact.

`mitm_addon.py` carries a by-value copy of `REDACTED_HEADERS`: mitmproxy runs the
addon in its own interpreter and cannot import this package. A unit test asserts
the two tables stay identical.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

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


def redact_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    """Lower-case header map with sensitive values replaced.

    The header **name** is preserved so an agent can still tell the header was
    present — "there was an Authorization header" is useful; its value is not.
    """
    items = headers.items() if hasattr(headers, "items") else headers
    result: dict[str, str] = {}
    for name, value in items:
        key = str(name).lower()
        result[key] = PLACEHOLDER if key in REDACTED_HEADERS else str(value)
    return result


SENSITIVE_FIELDS = (
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "id_token", "api_key", "apikey", "client_secret", "authorization",
    "session_key", "private_key", "credential", "otp", "pin",
)
_FIELD_RE = re.compile(
    r'(?i)("(?:' + "|".join(SENSITIVE_FIELDS) + r')"\s*:\s*)"(?:[^"\\]|\\.)*"'
)
_FORM_RE = re.compile(
    r'(?i)\b((?:' + "|".join(SENSITIVE_FIELDS) + r')=)[^&\s]+'
)


def scrub_body(text: str) -> str:
    """Mask obvious credential fields inside a body preview.

    Header redaction alone is not enough: a login request carries its password in
    the body, and a preview of it would land on disk by default. JSON is handled
    structurally where possible, with a regex fallback for form encoding and for
    bodies that do not parse.
    """
    if not text:
        return text
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            return json.dumps(_scrub_json(json.loads(text)), ensure_ascii=False)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    scrubbed = _FIELD_RE.sub(r'\1"' + PLACEHOLDER + '"', text)
    return _FORM_RE.sub(r"\1" + PLACEHOLDER, scrubbed)


def _scrub_json(value):
    if isinstance(value, dict):
        return {
            key: (PLACEHOLDER if str(key).lower() in SENSITIVE_FIELDS else _scrub_json(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_json(item) for item in value]
    return value


def preview(body: bytes | str | None, limit: int = PREVIEW_LIMIT) -> str | None:
    if body is None:
        return None
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    else:
        text = body
    text = scrub_body(text)
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER


def body_size(body: bytes | str | None) -> int:
    if body is None:
        return 0
    if isinstance(body, bytes):
        return len(body)
    return len(body.encode("utf-8"))


def scrub_flow(record: dict[str, Any]) -> dict[str, Any]:
    """Defence in depth: re-apply redaction to a record read back from disk."""
    scrubbed = dict(record)
    for key in ("request_headers_preview", "response_headers_preview"):
        headers = scrubbed.get(key)
        if isinstance(headers, dict):
            scrubbed[key] = redact_headers(headers)
    return scrubbed
