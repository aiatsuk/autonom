"""Autonom mitmproxy addon — records flows and applies mock rules.

**This file is loaded by mitmproxy's own Python interpreter**, which on a typical
install is a self-contained binary bundle with a different version from the one
running the rest of Autonom. It therefore:

- imports nothing from `autonom_lib` (there is no import path to it);
- uses only the standard library plus mitmproxy's public addon API;
- takes all configuration through mitmproxy options (`--set autonom_dir=...`);
- keeps its pure helpers importable **without** mitmproxy, so the repository's
  own test suite can exercise them directly.

The redaction tables below are by-value copies of `redact.REDACTED_HEADERS` and
`redact.SENSITIVE_QUERY_KEYS`; unit tests assert they never diverge.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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
# A `delay_s` above this is clamped, and a `hang` rule holds a flow at most
# this long before the flow is killed and recorded as incomplete.
HANG_CAP_S = 120.0
HANG_POLL_S = 0.25
ERROR_KINDS = ("client_abort", "upstream_error", "hang_cap", "other")
TRUNCATION_MARKER = "…[truncated]"

SENSITIVE_FIELDS = (
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "id_token", "api_key", "apikey", "client_secret", "authorization",
    "session_key", "private_key", "credential", "otp", "pin",
)

SENSITIVE_QUERY_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "key", "secret", "client_secret", "password", "passwd", "auth",
    "authorization", "session", "sessionid", "session_id", "sid", "signature",
    "sig", "code", "jwt", "bearer",
    "x_amz_signature", "x_amz_credential", "x_amz_security_token",
})


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


def _query_key(name):
    """Normalised query key: decoded, lower-case, `-` as `_`, no `[..]` suffix.

    `Access-Token`, `access%5Ftoken`, `token[]` and `Token[0]` all compare as
    their plain form. Decoding uses a regex rather than `urllib` so the addon's
    by-value copy (which may import only a fixed stdlib set) stays identical.
    """
    import re

    decoded = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)),
                     name.replace("+", " "))
    decoded = re.sub(r"(\[[^\]]*\])+$", "", decoded.strip())
    return decoded.strip().lower().replace("-", "_")


def is_sensitive_query_key(name):
    return _query_key(name) in SENSITIVE_QUERY_KEYS


def _scrub_pairs(text):
    """Mask sensitive values in an `a=1&b=2` string, byte-for-byte otherwise."""
    parts = []
    for part in text.split("&"):
        name, equals, _value = part.partition("=")
        if equals and is_sensitive_query_key(name):
            part = "%s=%s" % (name, PLACEHOLDER)
        parts.append(part)
    return "&".join(parts)


def _scrub_userinfo(url):
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
    return "%s://%s:%s@%s%s" % (scheme, user, PLACEHOLDER, host, rest[end:])


def scrub_url(url):
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


def preview(body, limit: int = PREVIEW_LIMIT):
    if body is None:
        return None
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = scrub_body(text)
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER


COPY_BODY_LIMIT = 4 * 1024 * 1024


def save_copy_body(directory, identifier, which, content):
    """Store complete UTF-8 text after redaction; never save a truncated prefix."""
    content = content or b""
    if len(content) > COPY_BODY_LIMIT:
        return "too_large"
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        if "\x00" in text:
            return "binary"
        text = scrub_body(text)
        encoded = text.encode("utf-8")
        if len(encoded) > COPY_BODY_LIMIT:
            return "too_large"
        folder = os.path.join(directory, "copy-bodies")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, "%s.%s" % (identifier, which))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        return "saved"
    except UnicodeError:
        return "binary"
    except (OSError, ValueError, TypeError, RecursionError):
        return "unavailable"


def glob_match(pattern: str, value: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(value, pattern)


def _glob_case(value: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(value, pattern)


# --- request conditions (match.query / match.headers / match.body) ------------

# Bodies above this never satisfy a body condition (DEC-006); rewrite edits
# give up on bodies above REWRITE_BODY_LIMIT and pass them through unchanged.
MATCH_BODY_LIMIT = 1024 * 1024
REWRITE_BODY_LIMIT = 8 * 1024 * 1024
_MISSING = object()
_INDEX = re.compile(r"[0-9]+")


def percent_decode(text: str) -> str:
    """`a%20b+c` -> `a b c`; bytes are joined before UTF-8 decoding.

    A regex rather than `urllib`: the addon may import only a fixed stdlib set.
    """
    raw = str(text).replace("+", " ").encode("utf-8", "surrogatepass")
    raw = re.sub(rb"%([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
    return raw.decode("utf-8", "replace")


def query_pairs(url) -> list:
    """Decoded `(name, value)` pairs of the URL's query; `?a` reads as `a=""`."""
    base = str(url or "").split("#", 1)[0]
    if "?" not in base:
        return []
    pairs = []
    for part in base.split("?", 1)[1].split("&"):
        if not part:
            continue
        name, _equals, value = part.partition("=")
        pairs.append((percent_decode(name), percent_decode(value)))
    return pairs


def header_pairs(headers) -> list:
    """`(lower-case name, value)` for every header field, repeats kept.

    mitmproxy's `Headers.items()` joins repeated fields; `items(multi=True)`
    keeps them apart. Plain mappings (the unit fakes) take no keyword.
    """
    if headers is None:
        return []
    try:
        if hasattr(headers, "items"):
            try:
                items = headers.items(multi=True)
            except TypeError:
                items = headers.items()
        else:
            items = headers
        return [(str(name).lower(), str(value)) for name, value in items]
    except Exception:  # noqa: BLE001 - never break a flow on odd headers
        return []


def decoded_body(message, limit: int):
    """A message body decoded per Content-Encoding, or None.

    None when it is over `limit` (before or after decoding), the encoding
    cannot be decoded, or the body is not available (streamed).
    """
    raw = getattr(message, "raw_content", None)
    if isinstance(raw, (bytes, bytearray)) and len(raw) > limit:
        return None
    getter = getattr(message, "get_content", None)
    try:
        content = getter(strict=True) if callable(getter) else getattr(message, "content", None)
    except Exception:  # noqa: BLE001 - mitmproxy raises ValueError on a bad encoding
        return None
    if content is None:
        return None
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, (bytes, bytearray)) or len(content) > limit:
        return None
    return bytes(content)


class RequestFacts:
    """What request conditions look at, each part computed at most once per flow.

    The body is read only when a rule with a body condition asks for it
    (RISK-001); `body_reads` counts the reads for the tests.
    """

    def __init__(self, url="", headers=None, body=None, body_loader=None) -> None:
        self.url = url or ""
        self._header_source = headers
        self._body = body
        self._body_loader = body_loader
        self._query = None
        self._headers = None
        self._text = _MISSING
        self._json = _MISSING
        self.body_reads = 0

    @classmethod
    def from_flow(cls, flow) -> "RequestFacts":
        request = flow.request
        return cls(getattr(request, "pretty_url", "") or "", getattr(request, "headers", None),
                   body_loader=lambda: decoded_body(request, MATCH_BODY_LIMIT))

    def query(self) -> list:
        if self._query is None:
            self._query = query_pairs(self.url)
        return self._query

    def headers(self) -> list:
        if self._headers is None:
            self._headers = header_pairs(self._header_source)
        return self._headers

    def body_text(self):
        """The UTF-8 body text, or None (too big, undecodable, not UTF-8)."""
        if self._text is _MISSING:
            self.body_reads += 1
            try:
                content = self._body_loader() if self._body_loader else self._body
            except Exception:  # noqa: BLE001
                content = None
            if isinstance(content, str):
                content = content.encode("utf-8", "surrogatepass")
            text = None
            if content is None and self._body_loader is None:
                content = b""
            if isinstance(content, (bytes, bytearray)) and len(content) <= MATCH_BODY_LIMIT:
                try:
                    text = bytes(content).decode("utf-8")
                except UnicodeDecodeError:
                    text = None
            self._text = text
        return self._text

    def body_json(self):
        """`(True, document)` when the body is JSON, else `(False, None)`."""
        if self._json is _MISSING:
            text = self.body_text()
            result = (False, None)
            if text is not None:
                try:
                    result = (True, json.loads(text))
                except (ValueError, RecursionError):
                    result = (False, None)
            self._json = result
        return self._json


def json_path_segments(path):
    """`user.items.0` -> segments; None for a path that is not usable."""
    if not isinstance(path, str) or not path:
        return None
    segments = path.split(".")
    if any(segment == "" for segment in segments):
        return None
    return segments


def json_lookup(document, segments):
    """`(True, value)` when the dotted path resolves, else `(False, None)`."""
    current = document
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, list):
            if not _INDEX.fullmatch(segment) or int(segment) >= len(current):
                return False, None
            current = current[int(segment)]
        else:
            return False, None
    return True, current


def json_equal(left, right) -> bool:
    """JSON equality: a bool never equals a number, 2 equals 2.0."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    return False


def json_value_matches(field, expected) -> bool:
    """A string condition is a case-sensitive glob over a string field."""
    if isinstance(expected, str):
        return isinstance(field, str) and _glob_case(field, expected)
    return json_equal(field, expected)


def _named_conditions_hold(conditions, pairs, *, fold_case: bool) -> bool:
    if conditions is None or conditions == []:
        return True
    if not isinstance(conditions, list):
        return False
    for condition in conditions:
        if not isinstance(condition, dict):
            return False
        name = condition.get("name")
        value = condition.get("value")
        if not isinstance(name, str) or not name or (
                value is not None and not isinstance(value, str)):
            return False
        key = name.lower() if fold_case else name
        values = [item for found, item in pairs if found == key]
        if not values:
            return False
        if value is not None and not any(_glob_case(item, value) for item in values):
            return False
    return True


def _body_conditions_hold(body, facts: RequestFacts) -> bool:
    if body is None or body == {}:
        return True
    if not isinstance(body, dict):
        return False
    contains = body.get("contains") or []
    wanted = body.get("json") or []
    if not isinstance(contains, list) or not isinstance(wanted, list):
        return False
    if not contains and not wanted:
        return True
    # Shape first, so a malformed rule never costs a body read.
    if any(not isinstance(item, str) or not item for item in contains):
        return False
    paths = []
    for item in wanted:
        segments = json_path_segments(item.get("path")) if isinstance(item, dict) else None
        if segments is None or "value" not in item:
            return False
        paths.append((segments, item["value"]))
    text = facts.body_text()
    if text is None:
        return False
    if any(item not in text for item in contains):
        return False
    if paths:
        parsed, document = facts.body_json()
        if not parsed:
            return False
        for segments, expected in paths:
            found, field = json_lookup(document, segments)
            if not found or not json_value_matches(field, expected):
                return False
    return True


def conditions_hold(match, facts: RequestFacts) -> bool:
    """All of a rule's query, header and body conditions (AND); malformed = False."""
    query = match.get("query")
    headers = match.get("headers")
    body = match.get("body")
    if not query and not headers and not body:
        return True
    try:
        return (_named_conditions_hold(query, facts.query(), fold_case=False)
                and _named_conditions_hold(headers, facts.headers(), fold_case=True)
                and _body_conditions_hold(body, facts))
    except Exception:  # noqa: BLE001 - a hand-edited rule never breaks a flow
        return False


def rule_mode(rule) -> str | None:
    """`respond` (also when absent), `rewrite`, or None for an unknown mode."""
    mode = (rule or {}).get("mode")
    if mode is None or mode == "respond":
        return "respond"
    if mode == "rewrite":
        return "rewrite"
    return None


def rule_matches(rule: dict, method: str, url: str, host: str, request=None) -> bool:
    """First-enabled-wins matching on url glob, optional method, optional host.

    `ignore_query` (set by `--url`) compares against the query-stripped URL, so
    `…/update/12341` also matches `…/update/12341?ts=1699`. It is opt-in per
    rule: a hand-written `--match` glob keeps exact control over the query.

    `request` (a `RequestFacts`) feeds the query, header and body conditions;
    without it the query still comes from `url` and header or body
    conditions do not hold. Every condition must hold (AND).
    """
    if not isinstance(rule, dict) or not rule.get("enabled", True):
        return False
    if rule_mode(rule) is None:
        return False
    match = rule.get("match") or {}
    if not isinstance(match, dict):
        return False
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
    if match.get("query") or match.get("headers") or match.get("body"):
        return conditions_hold(match, request if request is not None else RequestFacts(url))
    return True


def select_rule(rules, method: str, url: str, host: str, request=None):
    facts = request
    for rule in rules or []:
        if facts is None and isinstance(rule, dict) and isinstance(rule.get("match"), dict) \
                and any(rule["match"].get(key) for key in ("query", "headers", "body")):
            facts = RequestFacts(url)
        if rule_matches(rule, method, url, host, facts):
            return rule
    return None


# --- response rewrite -------------------------------------------------------------


class RewriteError(Exception):
    """A rewrite step that cannot apply; `reason` goes to `rewrite.error`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def _copy_json(value):
    """A private copy, so edits never touch the cached rule."""
    return json.loads(json.dumps(value))


def rewrite_plan(rule) -> dict:
    """The rule's checked rewrite edits; `RewriteError('invalid_rule')` if malformed."""
    def bad():
        return RewriteError("invalid_rule")

    response = rule.get("response") or {}
    rewrite = rule.get("rewrite") or {}
    if not isinstance(response, dict) or not isinstance(rewrite, dict):
        raise bad()
    status = response.get("status")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)
                               or not 100 <= status <= 599):
        raise bad()
    headers = response.get("headers") or {}
    if not isinstance(headers, dict):
        raise bad()
    set_headers = []
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name.strip()) \
                or not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise bad()
        set_headers.append((name.strip(), str(value)))
    body_path = response.get("body_path")
    if body_path is not None and not isinstance(body_path, str):
        raise bad()
    lists = {}
    for key in ("remove_headers", "json_set", "json_remove", "replace"):
        value = rewrite.get(key) or []
        if not isinstance(value, list):
            raise bad()
        lists[key] = value
    if any(not isinstance(name, str) or not _HEADER_NAME.fullmatch(name.strip())
           for name in lists["remove_headers"]):
        raise bad()
    json_set = []
    for item in lists["json_set"]:
        if not isinstance(item, dict) or json_path_segments(item.get("path")) is None \
                or "value" not in item:
            raise bad()
        json_set.append((item["path"], item["value"]))
    if any(json_path_segments(path) is None for path in lists["json_remove"]):
        raise bad()
    replace = []
    for item in lists["replace"]:
        if not isinstance(item, dict) or not isinstance(item.get("find"), str) \
                or not item["find"] or not isinstance(item.get("with"), str):
            raise bad()
        replace.append((item["find"], item["with"]))
    if body_path and (json_set or lists["json_remove"] or replace):
        raise bad()
    return {
        "status": status,
        "remove_headers": [name.strip() for name in lists["remove_headers"]],
        "set_headers": set_headers,
        "body_path": body_path or None,
        "json_set": json_set,
        "json_remove": list(lists["json_remove"]),
        "replace": replace,
    }


def _status_reason(status: int) -> str:
    try:
        from mitmproxy.net.http import status_codes  # noqa: PLC0415

        return status_codes.RESPONSES.get(status, "")
    except Exception:  # noqa: BLE001 - outside mitmproxy, or a moved table
        return ""


def _remove_header(headers, name: str) -> None:
    lowered = name.lower()
    for key in [key for key in list(headers.keys()) if str(key).lower() == lowered]:
        if key in headers:
            del headers[key]


def rewrite_status_headers(response, plan: dict) -> list:
    """Status, then removed headers, then set headers. Returns the edit names."""
    applied = []
    if plan.get("status") is not None:
        response.status_code = plan["status"]
        try:
            response.reason = _status_reason(plan["status"])
        except Exception:  # noqa: BLE001
            pass
        applied.append("status")
    if plan.get("remove_headers"):
        for name in plan["remove_headers"]:
            _remove_header(response.headers, name)
        applied.append("remove_headers")
    if plan.get("set_headers"):
        for name, value in plan["set_headers"]:
            _remove_header(response.headers, name)
            response.headers[name] = value
        applied.append("headers")
    return applied


def _json_set(document, path: str, value) -> None:
    segments = path.split(".")
    found, parent = json_lookup(document, segments[:-1])
    last = segments[-1]
    if found and isinstance(parent, dict):
        parent[last] = _copy_json(value)
    elif found and isinstance(parent, list) and _INDEX.fullmatch(last) \
            and int(last) < len(parent):
        parent[int(last)] = _copy_json(value)
    else:
        raise RewriteError("path_not_found: " + path)


def _json_remove(document, path: str) -> None:
    segments = path.split(".")
    found, parent = json_lookup(document, segments[:-1])
    last = segments[-1]
    if found and isinstance(parent, dict) and last in parent:
        del parent[last]
    elif found and isinstance(parent, list) and _INDEX.fullmatch(last) \
            and int(last) < len(parent):
        del parent[int(last)]
    else:
        raise RewriteError("path_not_found: " + path)


def rewrite_body(content: bytes, plan: dict) -> bytes:
    """Apply JSON sets, JSON removals, then text replacements, all or nothing.

    Works on the decoded body; raises `RewriteError` (`body_too_large`,
    `not_json`, `not_text`, `path_not_found: PATH`) without a partial result.
    """
    if content is None:
        raise RewriteError("body_unavailable")
    if len(content) > REWRITE_BODY_LIMIT:
        raise RewriteError("body_too_large")
    if plan.get("json_set") or plan.get("json_remove"):
        try:
            document = json.loads(bytes(content).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise RewriteError("not_json") from None
        for path, value in plan.get("json_set") or []:
            _json_set(document, path, value)
        for path in plan.get("json_remove") or []:
            _json_remove(document, path)
        text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    else:
        try:
            text = bytes(content).decode("utf-8")
        except UnicodeDecodeError:
            raise RewriteError("not_text") from None
    for find, replacement in plan.get("replace") or []:
        text = text.replace(find, replacement)
    return text.encode("utf-8")


def _set_body(message, content: bytes) -> None:
    """Re-encode with the message's Content-Encoding; Content-Length follows."""
    setter = getattr(message, "set_content", None)
    if callable(setter):
        setter(content)
    else:
        message.content = content


def _resolve_body_path(directory: str, body_path: str) -> str:
    if os.path.isabs(body_path):
        return body_path
    return os.path.join(directory or ".", os.path.relpath(body_path, "network")
                        if body_path.startswith("network/") else body_path)


def apply_rewrite(flow, rule, directory: str = "") -> dict:
    """Edit `flow.response` per the rule: `{"applied": [...], "error": str|None}`.

    Status and header edits always apply; a body edit that cannot apply
    leaves the body exactly as the server sent it (DEC-007). Never raises.
    """
    outcome = {"applied": [], "error": None}
    try:
        plan = rewrite_plan(rule)
    except RewriteError as exc:
        outcome["error"] = exc.reason
        return outcome
    response = flow.response
    try:
        outcome["applied"] = rewrite_status_headers(response, plan)
    except Exception:  # noqa: BLE001
        outcome["error"] = "invalid_rule"
        return outcome
    try:
        if plan["body_path"]:
            try:
                with open(_resolve_body_path(directory, plan["body_path"]), "rb") as handle:
                    content = handle.read()
            except OSError:
                raise RewriteError("body_unreadable") from None
            _set_body(response, content)
            outcome["applied"].append("body")
        elif plan["json_set"] or plan["json_remove"] or plan["replace"]:
            raw = getattr(response, "raw_content", None)
            if isinstance(raw, (bytes, bytearray)) and len(raw) > REWRITE_BODY_LIMIT:
                raise RewriteError("body_too_large")
            getter = getattr(response, "get_content", None)
            try:
                content = getter(strict=True) if callable(getter) else response.content
            except Exception:  # noqa: BLE001 - mitmproxy raises ValueError
                raise RewriteError("undecodable_encoding") from None
            edited = rewrite_body(content, plan)
            _set_body(response, edited)
            outcome["applied"] += [name for name in ("json_set", "json_remove", "replace")
                                   if plan[name]]
    except RewriteError as exc:
        outcome["error"] = exc.reason
    except Exception:  # noqa: BLE001 - INV-2: never out of a hook
        outcome["error"] = "internal_error"
    return outcome


def flow_id(counter: int) -> str:
    return "f_%04d" % counter


def last_flow_counter(directory: str) -> int:
    """Resume ids across proxy restarts; duplicate ids hide rows and overwrite bodies."""
    largest = 0
    try:
        with open(os.path.join(directory, "flows.jsonl"), encoding="utf-8",
                  errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    identifier = record.get("id") if isinstance(record, dict) else None
                    match = re.fullmatch(r"f_([0-9]+)", identifier or "")
                    if match:
                        largest = max(largest, int(match.group(1)))
                except (ValueError, RecursionError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return largest


def client_ip(flow):
    """The address the flow's client connected from, or None.

    mitmproxy 7+ exposes it as `client_conn.peername`; older releases as
    `client_conn.address`. Either is a (host, port) pair.
    """
    connection = getattr(flow, "client_conn", None)
    for name in ("peername", "address"):
        peer = getattr(connection, name, None)
        if isinstance(peer, (tuple, list)) and peer and peer[0]:
            return str(peer[0])
    return None


# An IPv4 dotted quad, or anything IPv6-shaped (hex groups with a colon,
# optionally bracketed). A hostname never contains a colon.
_IP_LITERAL = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|\[?[0-9A-Fa-f]*:[0-9A-Fa-f:.]*\]?)$")


def flow_host(flow) -> str:
    """The host the client asked for: the Host header, not the CONNECT target.

    `request.host` is whatever the CONNECT named. Under the emulator's
    `-http-proxy` every CONNECT names an IP, so it read `130.193.59.68` while the
    app asked for `backend.woolbox.app` — and `requests list --host` and a
    `--host` mock both missed every flow. mitmproxy's `pretty_host` prefers the
    Host header (`:authority` on HTTP/2), which is the name. Falls back to
    `request.host` when a flow has no `pretty_host`.
    """
    request = flow.request
    return getattr(request, "pretty_host", None) or request.host


def server_ip(flow):
    """The CONNECT target when it is a literal IP other than the recorded host.

    Kept (additively) so the address the connection actually went to is not
    lost once `host` is the requested name; None when the two agree.
    """
    raw = getattr(flow.request, "host", None)
    if isinstance(raw, str) and raw and raw != flow_host(flow) and _IP_LITERAL.match(raw):
        return raw
    return None


def rule_delay(rule) -> float | None:
    """The rule's delay in seconds, or None. Bad values are ignored, big ones capped.

    The registry is validated on write, but the addon must never crash or
    stall on a hand-edited file.
    """
    value = (rule or {}).get("delay_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value <= 0:  # NaN or not positive
        return None
    return float(min(value, HANG_CAP_S))


def rule_hangs(rule) -> bool:
    return (rule or {}).get("hang") is True


# Words that name the server side outright; they win over a closed client.
# The DNS and connect texts are what mitmdump 12.2.3 reports on macOS.
_UPSTREAM_WORDS = ("server", "upstream", "connect to", "failed to connect", "connect call",
                   "tls", "ssl", "certificate", "dns", "resolve", "nodename", "not known",
                   "getaddrinfo", "refused", "unreachable")
# Words that fit either side ("peer closed connection" is what mitmproxy says
# when an HTTP/2 client goes away); a closed client connection decides them.
_AMBIGUOUS_WORDS = ("timed out", "timeout", "peer")


def classify_error(message, *, client_side=None) -> str:
    """Map a mitmproxy flow error to one of `ERROR_KINDS` (never `hang_cap`).

    A message that names the client or the server decides. Otherwise
    `client_side=True` (the client connection is known to be closed) makes it
    a client abort, and an ambiguous message ("peer closed connection",
    "timed out") without that is an upstream error.
    """
    text = str(message or "").lower()
    if "client" in text and any(
            word in text for word in ("disconnect", "closed", "abort", "reset", "cancel")):
        return "client_abort"
    if any(word in text for word in _UPSTREAM_WORDS):
        return "upstream_error"
    if client_side is True:
        return "client_abort"
    if any(word in text for word in _AMBIGUOUS_WORDS):
        return "upstream_error"
    return "other"


def client_closed_at(flow):
    """When the client's connection closed (epoch seconds), else None.

    mitmproxy 12 queues a flow's stream events while its request hook is
    awaited, so `flow.error` stays None for the whole hold and the error hook
    only runs after the hook returns. The client connection object is updated
    at once, though: its `timestamp_end` is set and its state becomes CLOSED
    as soon as the client hangs up (checked with mitmdump 12.2.3 and
    `curl --max-time`, HTTP/1.1 and HTTP/2). That is the signal a delayed or
    held flow polls.
    """
    connection = getattr(flow, "client_conn", None)
    if connection is None:
        return None
    ended = getattr(connection, "timestamp_end", None)
    if isinstance(ended, (int, float)) and not isinstance(ended, bool) and ended > 0:
        return float(ended)
    if getattr(getattr(connection, "state", None), "name", None) == "CLOSED":
        return time.time()
    return None


def _iso(seconds) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def incomplete_record(flow, identifier: str, error_kind: str, *, now=None,
                      ended=None) -> dict:
    """The store record of a flow that ended without a response.

    Method, host, path, a scrubbed URL, timing and the mock id only: never
    headers or bodies, since an aborted request is often a login. `ended`
    defaults to the flow error's time, else `now`.
    """
    if error_kind not in ERROR_KINDS:
        error_kind = "other"
    request = flow.request
    now = time.time() if now is None else now
    started = getattr(request, "timestamp_start", None) or None
    error = getattr(flow, "error", None)
    ended = ended or getattr(error, "timestamp", None) or now
    metadata = getattr(flow, "metadata", None) or {}
    mock_id = metadata.get("autonom_mock_id")
    record = {
        "id": identifier,
        "completed": False,
        "error_kind": error_kind,
        "started_at": _iso(started or now),
        "started_at_ms": int((started or now) * 1000),
        "method": request.method,
        "url": scrub_url(getattr(request, "pretty_url", None)),
        "host": flow_host(flow),
        "path": (request.path or "").split("?", 1)[0],
        "status": None,
        "mocked": bool(mock_id),
        "mock_id": mock_id,
        "mock_kind": mock_kind_of(metadata),
        "client_ip": client_ip(flow),
    }
    record.update(rewrite_fields(metadata))
    if started:
        record["finished_at_ms"] = int(ended * 1000)
        record["duration_ms"] = max(0, int((ended - started) * 1000))
    return record


def mock_kind_of(metadata) -> str | None:
    """`respond` / `rewrite` for a flow a rule answered or edited, else None."""
    if not metadata.get("autonom_mock_id"):
        return None
    return "rewrite" if metadata.get("autonom_mock_kind") == "rewrite" else "respond"


def rewrite_fields(metadata) -> dict:
    """`upstream_status` and `rewrite` (edit names and error, never values)."""
    outcome = metadata.get("autonom_rewrite")
    if not metadata.get("autonom_mock_id") or not isinstance(outcome, dict):
        return {}
    return {
        "upstream_status": outcome.get("upstream_status"),
        "rewrite": {"applied": list(outcome.get("applied") or []),
                    "error": outcome.get("error")},
    }


# --- addon --------------------------------------------------------------------


class AutonomRecorder:
    """Append every flow to JSONL, and short-circuit requests that match a mock."""

    def __init__(self) -> None:
        self.directory = ""
        self.mocks_path = ""
        self.capture_bodies = False
        self.counter = 0
        self._session_reader = None
        self._rules: list = []
        self._rules_mtime: float | None = None
        self._rules_warned = False
        # Seams for the unit tests; mitmproxy never touches them.
        self._sleep = asyncio.sleep
        self._clock = time.monotonic
        self._wall = time.time
        self.hang_cap_s = HANG_CAP_S
        self.hang_poll_s = HANG_POLL_S

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
                self.counter = max(self.counter, last_flow_counter(self.directory))
        if "autonom_mocks" in updates:
            self.mocks_path = ctx.options.autonom_mocks or ""
        if "autonom_capture_bodies" in updates:
            self.capture_bodies = bool(ctx.options.autonom_capture_bodies)

    def request(self, flow):
        """Serve a matching mock; a `delay_s` / `hang` rule waits without blocking.

        A plain mock is served at once and the hook returns None. A rule that
        waits returns a coroutine instead: mitmproxy awaits an awaitable hook
        result on the event loop (its addon manager accepts sync and async
        hooks alike), so a delayed or held flow never stalls any other flow.

        A `rewrite` rule only marks the flow: the request goes upstream
        unchanged and the `response` hook edits the real answer.
        """
        # Host-based rules match the requested name, never the CONNECT IP.
        rule = select_rule(
            self._load_rules(), flow.request.method, flow.request.pretty_url, flow_host(flow),
            RequestFacts.from_flow(flow),
        )
        if not rule:
            return None
        if rule_mode(rule) == "rewrite":
            self._mark_rewrite(flow, rule)
            return None
        if rule_hangs(rule) or rule_delay(rule):
            return self._wait_then_serve(flow, rule)
        self._serve(flow, rule)
        return None

    @staticmethod
    def _mark_rewrite(flow, rule) -> None:
        flow.metadata["autonom_mock_id"] = rule.get("id")
        flow.metadata["autonom_mock_kind"] = "rewrite"

    async def _wait_then_serve(self, flow, rule) -> None:
        # Marked before any wait, so an abort meanwhile records `mocked`.
        flow.metadata["autonom_mock_id"] = rule.get("id")
        flow.metadata["autonom_mock_kind"] = "respond"
        if rule_hangs(rule):
            rule = await self._hold(flow, rule)
            if rule is None:
                return
            if rule_mode(rule) == "rewrite":
                # Switched to a rewrite rule while held: go upstream, edit later.
                self._mark_rewrite(flow, rule)
                return
        delay = rule_delay(rule)
        if delay:
            flow.metadata["autonom_mock_id"] = rule.get("id")
            if not await self._wait(flow, delay):
                return
        self._serve(flow, rule)

    async def _wait(self, flow, seconds: float) -> bool:
        """Sleep `seconds` in short steps; False when the client hung up meanwhile.

        One long sleep would serve a response nobody reads and record the
        flow only once mitmproxy gets to its queued close.
        """
        deadline = self._clock() + seconds
        while True:
            if self._client_left(flow):
                return False
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            await self._sleep(min(self.hang_poll_s, remaining))

    async def _hold(self, flow, rule):
        """Hold a flow while its `hang` rule stays in force.

        Returns the rule to serve when it was switched to answering, None when
        the request should go upstream (rule removed or disabled), the client
        hung up (recorded as `client_abort`), or the flow was killed at the cap
        (recorded as `hang_cap`).

        Limitation: an HTTP/2 client that cancels one stream but keeps its
        connection open is not visible during the hold (mitmproxy queues the
        stream reset), so that flow is held to the cap.
        """
        identifier = rule.get("id")
        deadline = self._clock() + self.hang_cap_s
        while True:
            if self._client_left(flow):
                return None
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._end_incomplete(flow, "hang_cap")
                return None
            await self._sleep(min(self.hang_poll_s, remaining))
            if self._client_left(flow):
                return None
            current = next((item for item in self._load_rules()
                            if isinstance(item, dict) and item.get("id") == identifier), None)
            if current is None or not current.get("enabled", True):
                # Released: the request continues to the real server.
                flow.metadata.pop("autonom_mock_id", None)
                flow.metadata.pop("autonom_mock_kind", None)
                return None
            if not rule_hangs(current):
                return current

    def _client_left(self, flow) -> bool:
        """Record and end a waiting flow whose client hung up; True if so."""
        closed = client_closed_at(flow)
        if closed is None:
            return False
        self._end_incomplete(flow, "client_abort", ended=closed)
        return True

    def _end_incomplete(self, flow, kind: str, *, ended=None) -> None:
        """Write the one incomplete record and kill the flow.

        The kill keeps mitmproxy from sending a request nobody waits for
        upstream once the hook returns; the error hook that follows sees
        `autonom_recorded` and adds nothing.
        """
        if not flow.metadata.get("autonom_recorded"):
            flow.metadata["autonom_recorded"] = True
            if self.directory:
                self.counter += 1
                self._append(incomplete_record(flow, flow_id(self.counter), kind, ended=ended))
        try:
            flow.kill()
        except Exception:  # noqa: BLE001 - an already-dead flow is fine
            pass

    def _serve(self, flow, rule) -> None:
        response = rule.get("response") or {}
        body = b""
        body_path = response.get("body_path")
        if body_path:
            try:
                with open(_resolve_body_path(self.directory, body_path), "rb") as handle:
                    body = handle.read()
            except OSError:
                body = b""
        status = response.get("status")
        flow.response = self._make_response(
            200 if status is None else int(status),
            body,
            {str(k): str(v) for k, v in (response.get("headers") or {}).items()},
        )
        flow.metadata["autonom_mock_id"] = rule.get("id")
        flow.metadata["autonom_mock_kind"] = "respond"

    @staticmethod
    def _make_response(status: int, body: bytes, headers: dict):
        from mitmproxy import http  # noqa: PLC0415

        return http.Response.make(status, body, headers)

    def error(self, flow) -> None:
        """A flow that ended without a response: one incomplete record."""
        if not self.directory or flow.metadata.get("autonom_recorded"):
            return
        flow.metadata["autonom_recorded"] = True
        error = getattr(flow, "error", None)
        closed = client_closed_at(flow)
        kind = classify_error(getattr(error, "msg", None) or error,
                              client_side=True if closed is not None else None)
        self.counter += 1
        self._append(incomplete_record(flow, flow_id(self.counter), kind))

    def response(self, flow):
        """Record the flow; a flow marked by a rewrite rule is edited first.

        The rule is read again by id now: one removed, disabled or switched to
        answering meanwhile leaves the real response untouched (`rule_gone`).
        A rewrite `delay_s` returns a coroutine that holds the edited response
        without blocking other flows, and ends early when the client leaves.
        """
        metadata = flow.metadata
        if metadata.get("autonom_mock_kind") == "rewrite" and "autonom_rewrite" not in metadata \
                and getattr(flow, "response", None) is not None:
            delay = self._rewrite(flow)
            if delay:
                return self._delay_then_record(flow, delay)
        self._record(flow)
        return None

    def _current_rewrite_rule(self, identifier):
        for rule in self._load_rules():
            if isinstance(rule, dict) and rule.get("id") == identifier \
                    and rule.get("enabled", True) and rule_mode(rule) == "rewrite":
                return rule
        return None

    def _rewrite(self, flow) -> float | None:
        """Apply the edits; the delay to hold the edited response for, or None."""
        upstream = getattr(flow.response, "status_code", None)
        delay = None
        try:
            rule = self._current_rewrite_rule(flow.metadata.get("autonom_mock_id"))
            if rule is None:
                outcome = {"applied": [], "error": "rule_gone"}
            else:
                outcome = apply_rewrite(flow, rule, self.directory)
                delay = rule_delay(rule)
        except Exception:  # noqa: BLE001 - INV-2: never out of a hook
            outcome = {"applied": [], "error": "internal_error"}
        flow.metadata["autonom_rewrite"] = {"upstream_status": upstream, **outcome}
        return delay

    async def _delay_then_record(self, flow, delay: float) -> None:
        if not await self._wait(flow, delay):
            return
        # The hold is part of the flow's time, as for a delayed fake response.
        flow.metadata["autonom_held_until"] = self._wall()
        self._record(flow)

    def _record(self, flow) -> None:
        if not self.directory:
            return
        self.counter += 1
        identifier = flow_id(self.counter)
        flow.metadata["autonom_recorded"] = True
        started = getattr(flow.request, "timestamp_start", None) or time.time()
        ended = getattr(flow.response, "timestamp_end", None) or time.time()
        held_until = flow.metadata.get("autonom_held_until")
        if isinstance(held_until, (int, float)) and held_until > ended:
            ended = held_until

        record = {
            "id": identifier,
            "completed": True,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            # Millisecond boundaries let a flow report correlate requests to
            # individual steps. Keep the ISO field for human/CLI compatibility.
            "started_at_ms": int(started * 1000),
            "finished_at_ms": int(ended * 1000),
            "method": flow.request.method,
            # Mock matching above sees the raw URL; only the record is scrubbed.
            "url": scrub_url(flow.request.pretty_url),
            # The requested name (Host header), so `--host` filters work
            # when the CONNECT target is an IP (the emulator's -http-proxy).
            "host": flow_host(flow),
            "path": flow.request.path.split("?", 1)[0],
            "status": flow.response.status_code,
            "duration_ms": max(0, int((ended - started) * 1000)),
            "request_headers_preview": redact_headers(flow.request.headers.items()),
            "response_headers_preview": redact_headers(flow.response.headers.items()),
            "request_body_preview": preview(flow.request.content),
            "response_body_preview": preview(flow.response.content),
            "mocked": bool(flow.metadata.get("autonom_mock_id")),
            "mock_id": flow.metadata.get("autonom_mock_id"),
            "mock_kind": mock_kind_of(flow.metadata),
            # Who sent it: `network status` counts a flow as the target's only
            # when its client is the target (a host curl is a flow too).
            "client_ip": client_ip(flow),
            "sizes": {
                "request_bytes": len(flow.request.content or b""),
                "response_bytes": len(flow.response.content or b""),
            },
        }

        record.update(rewrite_fields(flow.metadata))

        connected_ip = server_ip(flow)
        if connected_ip:
            record["server_ip"] = connected_ip

        for key, header in (("request_headers_preview", "referer"),
                            ("response_headers_preview", "location")):
            if record[key].get(header):
                record[key][header] = scrub_url(record[key][header])

        record["body_copy"] = {
            "request": save_copy_body(self.directory, identifier, "req", flow.request.content),
            "response": save_copy_body(self.directory, identifier, "res", flow.response.content),
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
        """Explicit session fixtures precede standing machine-wide mocks."""
        rules = self._load_registry()
        if not self.directory or not self.mocks_path:
            return rules
        path = os.path.join(self.directory, "session-mocks", "registry.json")
        if self._session_reader is None or self._session_reader.mocks_path != path:
            self._session_reader = AutonomRecorder()
            self._session_reader.mocks_path = path
        return self._session_reader._load_registry() + rules

    def _load_registry(self) -> list:
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
