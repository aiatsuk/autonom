"""Credential redaction and preview truncation (CAP-NET-004, INV-03).

Redaction happens **before the first write**, not before display. A MITM proxy
reads credentials by construction, and `.autonom/` is a working directory a user
may later archive or attach to a bug report — so an artifact that has never
contained a token is the only safe artifact.

`mitm_addon.py` carries by-value copies of `REDACTED_HEADERS` and
`SENSITIVE_QUERY_KEYS`: mitmproxy runs the addon in its own interpreter and
cannot import this package. Unit tests assert the tables stay identical.
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
    return scrub_patterns(text)


def scrub_patterns(text: str) -> str:
    """The regex half of `scrub_body`: JSON-ish fields and form fields.

    Also useful on its own after structural JSON scrubbing, for a form value
    embedded in a JSON string (`{"a": "password=x"}`).
    """
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


# Query-string (and fragment) keys whose value is a credential. Matched
# case-insensitively on the decoded key. A signed or tokenised URL is the
# commonest way a secret reaches a recorded flow without any header involved.
SENSITIVE_QUERY_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "key", "secret", "client_secret", "password", "passwd", "auth",
    "authorization", "session", "sessionid", "session_id", "sid", "signature",
    "sig", "code", "jwt", "bearer",
    "x_amz_signature", "x_amz_credential", "x_amz_security_token",
})


def _query_key(name: str) -> str:
    """Normalised query key: decoded, lower-case, `-` as `_`, no `[..]` suffix.

    `Access-Token`, `access%5Ftoken`, `token[]` and `Token[0]` all compare as
    their plain form. Decoding uses a regex rather than `urllib` so the addon's
    by-value copy (which may import only a fixed stdlib set) stays identical.
    """
    decoded = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)),
                     name.replace("+", " "))
    decoded = re.sub(r"(\[[^\]]*\])+$", "", decoded.strip())
    return decoded.strip().lower().replace("-", "_")


def is_sensitive_query_key(name: str) -> bool:
    return _query_key(name) in SENSITIVE_QUERY_KEYS


def _scrub_pairs(text: str) -> str:
    """Mask sensitive values in an `a=1&b=2` string, byte-for-byte otherwise."""
    parts = []
    for part in text.split("&"):
        name, equals, _value = part.partition("=")
        if equals and is_sensitive_query_key(name):
            part = f"{name}={PLACEHOLDER}"
        parts.append(part)
    return "&".join(parts)


def _scrub_userinfo(url: str) -> str:
    """`scheme://user:pw@host` keeps the user and loses the password."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    end = len(rest)
    for mark in "/?#":
        position = rest.find(mark)
        if position != -1:
            end = min(end, position)
    authority = rest[:end]
    if "@" not in authority:
        return url
    userinfo, host = authority.rsplit("@", 1)
    if ":" not in userinfo:
        return url
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:{PLACEHOLDER}@{host}{rest[end:]}"


def scrub_url(url: str | None) -> str | None:
    """Redact credentials in a URL; keep every other byte of it.

    `https://h/p?token=abc&page=2` becomes `https://h/p?token=<redacted>&page=2`.
    The fragment gets the same treatment, since OAuth implicit flows carry
    `#access_token=...` there, and a `user:password@` userinfo loses the
    password.
    """
    if not url or not isinstance(url, str):
        return url
    url = _scrub_userinfo(url)
    if "?" not in url and "#" not in url:
        return url
    base, hash_mark, fragment = url.partition("#")
    head, question, query = base.partition("?")
    result = head + (question + _scrub_pairs(query) if question else "")
    if hash_mark:
        result += hash_mark + _scrub_pairs(fragment)
    return result


def scrub_flow(record: dict[str, Any]) -> dict[str, Any]:
    """Defence in depth: re-apply redaction to a record read back from disk.

    Flows recorded before URL redaction existed still carry raw query secrets,
    so the URL, a redirect `location` and a request `referer` are scrubbed
    here at read time too.
    """
    scrubbed = dict(record)
    for key in ("request_headers_preview", "response_headers_preview"):
        headers = scrubbed.get(key)
        if isinstance(headers, dict):
            scrubbed[key] = redact_headers(headers)
    if isinstance(scrubbed.get("url"), str):
        scrubbed["url"] = scrub_url(scrubbed["url"])
    for key, header in (("request_headers_preview", "referer"),
                        ("response_headers_preview", "location")):
        headers = scrubbed.get(key)
        if isinstance(headers, dict) and isinstance(headers.get(header), str):
            scrubbed[key] = {**headers, header: scrub_url(headers[header])}
    return scrubbed
