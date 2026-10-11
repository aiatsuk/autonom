"""Pre-test storage: the record, its directory, locks and the active pointer.

Layout (contract §1)::

    <artifacts_dir>/pretests/
      active.json            {"pretest_id": "...", "check_id": null|"chk-N"}
      .lock                  serializes active.json and pre-test creation
      <pretest_id>/
        pretest.json         the record (autonom.pretest/v1), rewritten atomically
        ticket.md            the ticket, redacted before it is written
        context.json         diff context (autonom.pretest-context/v1)
        baseline.json        device baseline (autonom.pretest-baseline/v1)
        budget.json          {"actions_used": N, "refusals": [...]}
        checks.ndjson        append-only check events
        findings.ndjson      append-only finding events
        .lock                serializes every write in this directory

Every file is owner-only (0600) and every directory 0700: a pre-test holds
screen content, ticket text and request data.

``active_ids`` is on the hot path of every journal append, screenshot and
action detail, so it reads one small file and never raises. With no
``pretests/`` directory it costs one failed ``open``.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .. import contracts, errors
from ..network import redact
from . import RECORD_SCHEMA

ROOT_NAME = "pretests"
ACTIVE_FILE = "active.json"
RECORD_FILE = "pretest.json"
TICKET_FILE = "ticket.md"
CONTEXT_FILE = "context.json"
BASELINE_FILE = "baseline.json"
BUDGET_FILE = "budget.json"
CHECKS_FILE = "checks.ndjson"
FINDINGS_FILE = "findings.ndjson"
LOCK_FILE = ".lock"

STATUSES = ("active", "stopped", "packed")
KINDS = ("pretest", "more-checks", "rerun")
PLATFORMS = ("android", "ios")

DEFAULT_MAX_ACTIONS = 300
DEFAULT_MAX_MINUTES = 60
# tp-006 defaults; the guard reads the record's own list, never this tuple.
DEFAULT_FORBID_TEXT = (
    "buy", "pay", "purchase", "delete", "remove account",
    "купить", "оплатить", "удалить",
)

PRETEST_ID = re.compile(r"^pt_\d{8}T\d{6}_[0-9a-f]{6}$")
CHECK_ID = re.compile(r"^chk-[1-9]\d*$")

TITLE_LIMIT = 200


def now() -> str:
    """ISO UTC with milliseconds: check windows compare against it."""
    return contracts.utc_now()


def new_pretest_id() -> str:
    return "pt_" + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "_" + secrets.token_hex(3)


# --- redaction -----------------------------------------------------------------
#
# Free text (the ticket, check and finding text, notes) is masked best effort
# over a FIXED list of forms (contract section 8). Masking is line based and
# strict (section 8.1): when a line holds one of the forms 1-4 below,
# everything from the start of its value to the END OF THAT LINE becomes
# ``<redacted>``; there is no partial-token logic. Anything outside this list
# is a documented limitation, not a defect:
#
# 1. a credential header from ``REDACTED_HEADERS`` followed by ``:`` anywhere
#    in a line, also inside a pasted curl command (``-H 'cookie: ...'``,
#    ``--header "Authorization: Basic ..."``);
# 2. the auth-scheme words ``bearer``, ``basic`` and ``token`` in any letter
#    case, followed by an optional ``:`` and a value. Prose loses the rest of
#    the line too ("the token is refreshed" keeps only "the token ");
# 3. a field whose name (one to five words, any letters) holds a secret word
#    (English or Russian, ``_SECRET_WORDS``) followed by ``:``, ``=``, an em or
#    en dash, or a closing ``**`` (``**Password:** x``, ``**PIN** x``);
# 4. ``.env`` lines ``NAME=value`` whose upper-case NAME contains PASSWORD,
#    SECRET, TOKEN, KEY, AUTH, COOKIE or SESSION anywhere; blanks are allowed
#    before and after ``=`` (``GITHUBTOKEN = x``, as dotenv parsers accept);
# 5. what the existing helpers recognize on the rest of the text: JWT-shaped
#    strings, credentials inside URLs, JSON and form fields with secret names.
#
# Any Unicode whitespace (``\s``: also U+00A0, U+2003, U+3000) separates a
# name, its ``:`` and its value. Every pattern is linear: no nested
# quantifiers, no two whitespace runs next to each other, names are capped at
# five words, quote and emphasis runs at three characters, and a name, a JWT
# or a URL scheme only starts where a lookbehind says no character of its own
# run precedes it (never ``\b`` before a class that holds ``-``), so a match
# attempt cannot restart inside a run an earlier attempt already walked. Runs
# followed by a character outside their class are possessive (``*+``), so
# they never give characters back. The patterns stop at the separator; the
# blanks and emphasis before the value are skipped in plain Python
# (``_skip_to_value``), so a long run of blanks is walked once.
#
# Free text is masked first and cut to its length limit afterwards (``clip``,
# contract 8.2): a cut never hides a credential from the masking and never
# splits the ``<redacted>`` marker.

_HEADER_NAMES = "|".join(re.escape(name) for name in
                         sorted(redact.REDACTED_HEADERS, key=len, reverse=True))
_HEADER = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:" + _HEADER_NAMES + r")\s*+:")
_SCHEME = re.compile(r"(?i)(?<![\w-])(?:bearer|basic|token)(?:\s*+:|\s)")
_ENV_LINE = re.compile(r"^\s*+(?:export\s++)?(?P<name>[A-Z][A-Z0-9_]*+)\s*+=")
_ENV_WORDS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "AUTH", "COOKIE", "SESSION")
_PAIR = re.compile(
    r"(?<![\w.\"'*-])[\"'*_]{0,3}"
    r"(?P<name>[^\W\d][\w.-]*+(?:\s++[^\W\d][\w.-]*+){0,4})"
    r"(?:[\"'*_]{0,3}\s*+(?::|=|—|–)|\*\*)")
_EMPHASIS = "*_"
_SECRET_WORDS = frozenset({
    # the fixed English list
    "password", "passwd", "pwd", "secret", "token", "apikey", "auth", "cookie",
    "session", "otp", "pin",
    # their plural or joined spellings
    "passwords", "secrets", "tokens", "cookies", "sessions", "pins", "otps",
    "sessionid", "accesskey", "privatekey", "clientsecret", "authtoken",
    "authorization", "authentication", "bearer", "passphrase", "credential",
    "credentials",
})
_SECRET_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key"),
                           ("client", "secret"), ("api", "keys"), ("access", "keys"),
                           ("private", "keys")})
# Russian words change their ending (пароль, пароля, паролем): stems match.
_SECRET_STEMS = ("парол", "секрет", "токен", "ключ")
_SECRET_RU_EXACT = frozenset({"код", "кода", "коду", "кодом", "коде", "коды"})
_NAME_SPLIT = re.compile(r"[\s_.\-]+|(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])")


def _secret_name(name: str) -> bool:
    words = [word.lower() for word in _NAME_SPLIT.split(name) if word]
    if not words:
        return False
    if "".join(words) in _SECRET_WORDS:
        return True
    for index, word in enumerate(words):
        if word in _SECRET_WORDS or word in _SECRET_RU_EXACT \
                or word.startswith(_SECRET_STEMS):
            return True
        if index and (words[index - 1], word) in _SECRET_PAIRS:
            return True
    return False


def _skip_blanks(line: str, index: int) -> int:
    while index < len(line) and line[index].isspace():
        index += 1
    return index


def _skip_to_value(line: str, index: int, emphasis: bool = False) -> int | None:
    """Index of the value after a separator ending at ``index``: blanks, then
    (for a field) up to three ``*``/``_`` closing an emphasis, then blanks.
    None when the rest of the line is blank (nothing to mask)."""
    index = _skip_blanks(line, index)
    if emphasis:
        stop = index
        while stop < len(line) and stop - index < 3 and line[stop] in _EMPHASIS:
            stop += 1
        index = _skip_blanks(line, stop)
    return index if index < len(line) else None


def _value_start(line: str) -> int | None:
    """Where the first masked value of ``line`` starts (rules 1-4), or None."""
    found: list[int] = []
    for pattern in (_HEADER, _SCHEME):
        match = pattern.search(line)
        if match:
            start = _skip_to_value(line, match.end())
            if start is not None:
                found.append(start)
    env = _ENV_LINE.match(line)
    if env and any(word in env.group("name") for word in _ENV_WORDS):
        # blanks before and after "=" are a separator too (contract 8.2
        # item 2): "GITHUBTOKEN= ghp_x" and "GITHUBTOKEN = ghp_x" mask "ghp_x"
        start = _skip_to_value(line, env.end())
        if start is not None:
            found.append(start)
    best = min(found) if found else None
    position = 0
    while True:
        match = _PAIR.search(line, position)
        if match is None or (best is not None and match.start() >= best):
            break
        if _secret_name(match.group("name")):
            start = _skip_to_value(line, match.end(), emphasis=True)
            if start is not None:
                found.append(start)
            break
        # Not a secret name: its value may still hold one ("user: a password: b").
        position = match.end("name")
    return min(found) if found else None


def _mask_line(line: str) -> str:
    ending = "\r" if line.endswith("\r") else ""
    body = line[:-1] if ending else line
    start = _value_start(body)
    if start is None:
        return line
    return body[:start] + redact.PLACEHOLDER + ending


# A token starts only where no token character precedes it (a lookbehind,
# not ``\b``: ``\b`` also matches after ``-``, which the token class eats, so
# every ``-eyJ`` would restart a scan of the same run). Each segment is
# possessive: it never gives characters back to look for a ``.`` inside it.
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}+\.[A-Za-z0-9_-]{4,}+"
                  r"\.[A-Za-z0-9_-]{4,}+")
# A scheme starts at a word start and is short, so a long word costs one pass.
_URL = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,31}+://[^\s<>\"'`)]++")

_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


# The trailing ``[..]`` groups of a query key, read on the REVERSED key: one
# group is "]", its content, then the "[" nearest the previous group (the
# greedy content backs off to it). Each group is walked once and the run is
# possessive, so a key of any length costs one pass.
_KEY_SUFFIXES_REVERSED = re.compile(r"\A(?:\][^\]]*\[)*+")


def _query_key(name: str) -> str:
    """``redact._query_key`` without its trailing-``[..]`` regex: that regex
    restarts at every ``[`` of a run and is quadratic on a long one. Here the
    suffixes are measured once on the reversed key and cut with one slice.
    Same result (decoded, lower-case, ``-`` as ``_``, no ``[..]`` suffixes)."""
    decoded = _PERCENT.sub(lambda m: chr(int(m.group(1), 16)), name.replace("+", " "))
    decoded = decoded.strip()
    suffixes = _KEY_SUFFIXES_REVERSED.match(decoded[::-1]).end()
    return decoded[:len(decoded) - suffixes].strip().lower().replace("-", "_")


def _scrub_pairs(text: str) -> str:
    parts = []
    for part in text.split("&"):
        name, equals, _value = part.partition("=")
        if equals and _query_key(name) in redact.SENSITIVE_QUERY_KEYS:
            part = f"{name}={redact.PLACEHOLDER}"
        parts.append(part)
    return "&".join(parts)


def _scrub_url(url: str) -> str:
    """``redact.scrub_url`` with the linear ``_query_key``: the userinfo
    password and the values of credential query and fragment keys masked,
    every other character kept."""
    url = redact._scrub_userinfo(url)  # noqa: SLF001 — the shared helper
    if "?" not in url and "#" not in url:
        return url
    base, hash_mark, fragment = url.partition("#")
    head, question, query = base.partition("?")
    result = head + (question + _scrub_pairs(query) if question else "")
    if hash_mark:
        result += hash_mark + _scrub_pairs(fragment)
    return result


# Every compiled pattern the masking runs, the shared helpers' included: the
# linear-time tests iterate over this mapping (contract 8.3).
MASKING_PATTERNS: dict[str, re.Pattern[str]] = {
    "_HEADER": _HEADER, "_SCHEME": _SCHEME, "_ENV_LINE": _ENV_LINE,
    "_PAIR": _PAIR, "_NAME_SPLIT": _NAME_SPLIT, "_JWT": _JWT, "_URL": _URL,
    "_PERCENT": _PERCENT, "_KEY_SUFFIXES_REVERSED": _KEY_SUFFIXES_REVERSED,
    "redact._FIELD_RE": redact._FIELD_RE,  # noqa: SLF001 — used by scrub_patterns
    "redact._FORM_RE": redact._FORM_RE,  # noqa: SLF001 — used by scrub_patterns
}


def redact_text(text: str | None) -> str | None:
    """Mask credentials in free text before it is written anywhere (the
    fixed list above). Best effort: forms outside the list are not masked.
    To also cap the length, use ``clip`` (it masks first)."""
    if not text:
        return text
    scrubbed = "\n".join(_mask_line(line) for line in text.split("\n"))
    scrubbed = _JWT.sub(redact.PLACEHOLDER, scrubbed)
    scrubbed = _URL.sub(lambda m: _scrub_url(m.group(0)), scrubbed)
    return redact.scrub_patterns(scrubbed)


def clip(text: Any, limit: int) -> str | None:
    """Masked, stripped and cut to ``limit`` characters, in that order
    (contract 8.2): the whole text is masked before anything is dropped, and
    a cut that would split a ``<redacted>`` marker cuts before it. None for
    None; ``""`` when nothing is left."""
    if text is None:
        return None
    masked = redact_text(str(text).strip()) or ""
    if len(masked) <= limit:
        return masked
    marker = redact.PLACEHOLDER
    cut = limit
    split = masked.find(marker, max(0, limit - len(marker) + 1))
    if split != -1 and split < limit < split + len(marker):
        cut = split
    return masked[:cut].rstrip()


# --- paths and files -------------------------------------------------------------


def pretests_root(session: dict[str, Any]) -> Path:
    return Path(session["artifacts_dir"]) / ROOT_NAME


def _check_id_shape(pretest_id: Any) -> str:
    if not isinstance(pretest_id, str) or not PRETEST_ID.match(pretest_id):
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"not a pre-test id: {pretest_id!r}",
            hint="Pre-test ids look like pt_20261010T120000_a1b2c3; "
                 "list them with 'autonom pretest list'.")
    return pretest_id


def pretest_dir(session: dict[str, Any], pretest_id: str) -> Path:
    return pretests_root(session) / _check_id_shape(pretest_id)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_json(path: Path, value: Any) -> None:
    """Atomic, owner-only JSON write (temp file in the same directory)."""
    scratch = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.chmod(scratch, 0o600)
        os.replace(scratch, path)
    finally:
        with contextlib.suppress(OSError):
            scratch.unlink()


def write_text(path: Path, text: str) -> None:
    scratch = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(scratch, 0o600)
        os.replace(scratch, path)
    finally:
        with contextlib.suppress(OSError):
            scratch.unlink()


def append_line(path: Path, value: dict[str, Any]) -> None:
    """Append one JSON line, owner-only; the caller holds the pre-test lock.

    A torn last line (a writer that died mid-line) is closed with a newline
    first, so it never absorbs this record; readers skip the torn line."""
    line = json.dumps(value, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size:
            handle.seek(size - 1)
            if handle.read(1) != b"\n":
                line = "\n" + line
        handle.write(line.encode("utf-8"))
        handle.flush()
    os.chmod(path, 0o600)


def read_lines(path: Path) -> list[dict[str, Any]]:
    """Every parseable JSON object line; a torn final line is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --- locks -------------------------------------------------------------------------

_HELD = threading.local()


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    """Exclusive, re-entrant within one thread (flock on a second descriptor
    of the same file would block the holder against itself)."""
    held: dict[str, int] = getattr(_HELD, "paths", None) or {}
    _HELD.paths = held
    key = str(path)
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows
            pass
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
    finally:
        os.close(fd)


def lock(session: dict[str, Any], pretest_id: str):
    """The lock every write inside one pre-test directory holds. The
    directory must exist: a lock never creates a pre-test."""
    directory = pretest_dir(session, pretest_id)
    if not directory.is_dir():
        raise errors.AutonomError(
            errors.NO_ACTIVE_PRETEST, f"no pre-test {pretest_id} in this session",
            hint="List this session's pre-tests with 'autonom pretest list'.",
            pretest_id=pretest_id)
    return _flock(directory / LOCK_FILE)


def _root_lock(session: dict[str, Any]):
    return _flock(_ensure_dir(pretests_root(session)) / LOCK_FILE)


# --- active pointer ------------------------------------------------------------------


def active_ids(session: dict[str, Any] | None) -> tuple[str, str | None] | None:
    """(pretest_id, open check id) from ``active.json`` alone; never raises."""
    try:
        if not session or not session.get("artifacts_dir"):
            return None
        path = Path(session["artifacts_dir"]) / ROOT_NAME / ACTIVE_FILE
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        pretest_id = data.get("pretest_id")
        if not isinstance(pretest_id, str) or not PRETEST_ID.match(pretest_id):
            return None
        check_id = data.get("check_id")
        if not (isinstance(check_id, str) and CHECK_ID.match(check_id)):
            check_id = None
        return pretest_id, check_id
    except Exception:  # noqa: BLE001 — attribution must never fail a command
        return None


def _record_is_active(session: dict[str, Any], pretest_id: str) -> bool:
    try:
        path = Path(session["artifacts_dir"]) / ROOT_NAME / pretest_id / RECORD_FILE
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        return isinstance(record, dict) and record.get("status") == "active"
    except Exception:  # noqa: BLE001
        return False


def _folded_open_check(session: dict[str, Any], pretest_id: str,
                       fallback: str | None) -> str | None:
    """The open check by the folded ``checks.ndjson`` events (the truth when
    a crash left ``active.json`` behind them). ``fallback`` (the pointer's
    check) when the events file exists but cannot be read (a directory in its
    place, no read permission) or folding fails; None when there is no events
    file yet (no check was ever added)."""
    try:
        with open(pretest_dir(session, pretest_id) / CHECKS_FILE, "rb"):
            pass
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — attribution must never fail a command
        return fallback
    try:
        from . import checks as checks_mod

        states = checks_mod.fold(session, {"pretest_id": pretest_id})
    except Exception:  # noqa: BLE001 — attribution must never fail a command
        return fallback
    return next((item["check_id"] for item in states if item["state"] == "open"), None)


def attribution(session: dict[str, Any] | None) -> dict[str, str]:
    """``{"pretest_id", "check_id"?}`` for an evidence writer, ``{}`` without
    an active pre-test (so nothing new is written then). A pointer whose
    record is missing or no longer active (left by a crash) is ignored, and
    the open check comes from the folded check events, so a pointer left
    behind by a crash between an event and its pointer update never
    misattributes evidence (contract 8.2)."""
    ids = active_ids(session)
    if not ids or not _record_is_active(session, ids[0]):
        return {}
    out = {"pretest_id": ids[0]}
    check_id = _folded_open_check(session, ids[0], ids[1])
    if check_id:
        out["check_id"] = check_id
    return out


def active(session: dict[str, Any] | None) -> dict[str, Any] | None:
    """The active pre-test's record, or None on any read problem."""
    try:
        ids = active_ids(session)
        if not ids:
            return None
        record = _read_json(pretest_dir(session, ids[0]) / RECORD_FILE)
        if not isinstance(record, dict) or record.get("status") != "active":
            return None
        return record
    except Exception:  # noqa: BLE001
        return None


def _write_active(session: dict[str, Any], pretest_id: str, check_id: str | None) -> None:
    write_json(pretests_root(session) / ACTIVE_FILE,
               {"pretest_id": pretest_id, "check_id": check_id})


def _clear_active(session: dict[str, Any], pretest_id: str) -> bool:
    path = pretests_root(session) / ACTIVE_FILE
    ids = active_ids(session)
    if ids and ids[0] == pretest_id:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return True
    return False


def set_open_check(session: dict[str, Any], pretest_id: str, check_id: str | None) -> bool:
    """Point ``active.json`` at ``check_id`` (or none). Only the active
    pre-test's pointer moves; returns whether it did."""
    if check_id is not None and not CHECK_ID.match(str(check_id)):
        raise errors.AutonomError(errors.UNKNOWN_CHECK, f"not a check id: {check_id!r}",
                                  hint="Check ids look like chk-1.", check_id=check_id)
    with _root_lock(session):
        ids = active_ids(session)
        if not ids or ids[0] != pretest_id:
            return False
        _write_active(session, pretest_id, check_id)
        return True


# --- record --------------------------------------------------------------------------


def _failures_for_record(record: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(record, dict):
        return ["record must be an object"]

    def need_str(key: str, nullable: bool = False) -> None:
        value = record.get(key)
        if value is None and nullable and key in record:
            return
        if not isinstance(value, str) or not value:
            failures.append(f"{key} must be a non-empty string"
                            + (" or null" if nullable else ""))

    if record.get("schema") != RECORD_SCHEMA:
        failures.append(f"schema must be {RECORD_SCHEMA}")
    if not isinstance(record.get("pretest_id"), str) or not PRETEST_ID.match(
            record.get("pretest_id") or ""):
        failures.append("pretest_id must look like pt_YYYYMMDDTHHMMSS_xxxxxx")
    need_str("session_id")
    if record.get("status") not in STATUSES:
        failures.append("status must be one of " + ", ".join(STATUSES))
    need_str("title")
    need_str("ticket_ref", nullable=True)
    need_str("created_at")
    need_str("ended_at", nullable=True)
    need_str("repo", nullable=True)
    need_str("base", nullable=True)
    need_str("head", nullable=True)
    need_str("flows_dir", nullable=True)
    need_str("app_id", nullable=True)
    need_str("parent_pack", nullable=True)
    target = record.get("target")
    if not isinstance(target, dict) or target.get("platform") not in PLATFORMS \
            or not isinstance(target.get("target_id"), str) or not target.get("target_id"):
        failures.append("target must be {platform: android|ios, target_id: string}")
    budget = record.get("budget")
    if not isinstance(budget, dict):
        failures.append("budget must be an object")
    else:
        if not isinstance(budget.get("max_actions"), int) or isinstance(
                budget.get("max_actions"), bool) or budget["max_actions"] < 1:
            failures.append("budget.max_actions must be an integer >= 1")
        minutes = budget.get("max_minutes")
        if not _positive_minutes(minutes):
            failures.append("budget.max_minutes must be a finite number > 0")
        for key in ("forbid_text", "allow_apps"):
            items = budget.get(key)
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                failures.append(f"budget.{key} must be a list of strings")
    if record.get("kind") not in KINDS:
        failures.append("kind must be one of " + ", ".join(KINDS))
    rerun_of = record.get("rerun_of")
    if rerun_of is not None:
        if not isinstance(rerun_of, dict) or not isinstance(rerun_of.get("pack"), str) \
                or not isinstance(rerun_of.get("checks"), list):
            failures.append("rerun_of must be null or {pack: string, checks: [ids]}")
    pack = record.get("pack")
    if pack is not None:
        if not isinstance(pack, dict) or not isinstance(pack.get("path"), str) \
                or not isinstance(pack.get("integrity_sha256"), str):
            failures.append("pack must be null or {path, integrity_sha256}")
    if record.get("status") == "packed" and pack is None:
        failures.append("a packed record must name its pack")
    for key in ("schema", "pretest_id", "session_id", "status", "title", "ticket_ref",
                "created_at", "ended_at", "repo", "base", "head", "flows_dir",
                "target", "app_id", "budget", "parent_pack", "kind", "rerun_of", "pack"):
        if key not in record:
            failures.append(f"missing {key}")
    return failures


def validate_record(record: Any) -> dict[str, Any]:
    """Hand-written validator for ``schemas/pretest-v1.schema.json``."""
    failures = _failures_for_record(record)
    if failures:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "pre-test record does not satisfy autonom.pretest/v1",
            hint="This is a bug in the writer or a hand-edited pretest.json.",
            failures=failures)
    return record


def load(session: dict[str, Any], pretest_id: str) -> dict[str, Any]:
    path = pretest_dir(session, pretest_id) / RECORD_FILE
    try:
        record = _read_json(path)
    except FileNotFoundError:
        raise errors.AutonomError(
            errors.NO_ACTIVE_PRETEST, f"no pre-test {pretest_id} in this session",
            hint="List this session's pre-tests with 'autonom pretest list'.",
            pretest_id=pretest_id) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"pre-test record {pretest_id} is unreadable: {exc}",
            hint="The file was damaged; the event logs beside it are intact.") from None
    return validate_record(record)


def save(session: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    validate_record(record)
    pretest_id = record["pretest_id"]
    with lock(session, pretest_id):
        write_json(pretest_dir(session, pretest_id) / RECORD_FILE, record)
    return record


def list_records(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Every readable pre-test record of the session, oldest first."""
    root = pretests_root(session)
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not PRETEST_ID.match(child.name):
            continue
        try:
            out.append(load(session, child.name))
        except errors.AutonomError:
            continue
    return out


def require_active(session: dict[str, Any] | None) -> dict[str, Any]:
    if not session:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION, "no active session",
            hint="Start one with 'autonom session start --serial <id> --app-id <app>'.")
    record = active(session)
    if record is None:
        raise errors.AutonomError(
            errors.NO_ACTIVE_PRETEST, "no active pre-test in this session",
            hint="Start one with 'autonom pretest start --ticket FILE --base REF'.")
    return record


def ensure_mutable(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("status") == "packed":
        pack = (record.get("pack") or {}).get("path")
        raise errors.AutonomError(
            errors.PRETEST_FINALIZED,
            f"pre-test {record.get('pretest_id')} is packed and can no longer change",
            hint="Start a child with 'autonom pretest review <pack> more-checks' or "
                 "'autonom pretest rerun <pack>'.",
            pretest_id=record.get("pretest_id"), pack=pack)
    return record


def check_open_record(record: dict[str, Any]) -> dict[str, Any]:
    """A begin needs the pre-test to be the running one, not stopped."""
    ensure_mutable(record)
    if record.get("status") != "active":
        raise errors.AutonomError(
            errors.NO_ACTIVE_PRETEST,
            f"pre-test {record.get('pretest_id')} is {record.get('status')}",
            hint="Start a new pre-test with 'autonom pretest start'.",
            pretest_id=record.get("pretest_id"))
    return record


# --- budget file -----------------------------------------------------------------------


def read_budget(session: dict[str, Any], pretest_id: str) -> dict[str, Any]:
    try:
        data = _read_json(pretest_dir(session, pretest_id) / BUDGET_FILE)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"actions_used": int(data.get("actions_used") or 0),
            "refusals": list(data.get("refusals") or [])}


def write_budget(session: dict[str, Any], pretest_id: str, budget: dict[str, Any]) -> None:
    with lock(session, pretest_id):
        write_json(pretest_dir(session, pretest_id) / BUDGET_FILE, budget)


# --- create / stop / pack --------------------------------------------------------------


def _target_block(target: Any, session: dict[str, Any]) -> dict[str, str]:
    if isinstance(target, dict):
        platform, target_id = target.get("platform"), target.get("target_id")
    elif target is not None:
        platform, target_id = getattr(target, "platform", None), getattr(target, "target_id", None)
    else:
        platform, target_id = session.get("platform"), session.get("target_id")
    if platform not in PLATFORMS or not target_id:
        raise errors.AutonomError(
            errors.NO_TARGET, "the pre-test needs the session's device",
            hint="Start the session on an Android target or an iOS Simulator first.")
    return {"platform": str(platform), "target_id": str(target_id)}


def _positive_minutes(value: Any) -> bool:
    """A finite number > 0 (NaN, inf, 0, negatives and booleans are not)."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _normalize_budget(budget: dict[str, Any] | None, app_id: str | None) -> dict[str, Any]:
    budget = dict(budget or {})
    max_actions = budget.get("max_actions")
    max_actions = DEFAULT_MAX_ACTIONS if max_actions is None else max_actions
    max_minutes = budget.get("max_minutes")
    max_minutes = DEFAULT_MAX_MINUTES if max_minutes is None else max_minutes
    if isinstance(max_actions, bool) or not isinstance(max_actions, int) or max_actions < 1:
        raise errors.AutonomError(errors.INVALID_VALUE,
                                  f"--max-actions must be a whole number >= 1, got {max_actions!r}",
                                  hint="For example --max-actions 300.")
    if not _positive_minutes(max_minutes):
        raise errors.AutonomError(errors.INVALID_VALUE,
                                  "--max-minutes must be a finite number > 0, "
                                  f"got {max_minutes!r}",
                                  hint="For example --max-minutes 60 (not NaN, inf, 0 or "
                                       "a negative number).")
    forbid = budget.get("forbid_text")
    forbid = list(DEFAULT_FORBID_TEXT) if forbid is None else [str(item) for item in forbid]
    for pattern in forbid:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"--forbid-text {pattern!r} is not a valid pattern: {exc}",
                hint="Patterns are case-insensitive regular expressions; escape "
                     "special characters.", pattern=pattern) from None
    allow = budget.get("allow_apps")
    if allow is None or not list(allow):
        allow = [app_id] if app_id else []
    return {"max_actions": max_actions, "max_minutes": max_minutes,
            "forbid_text": forbid, "allow_apps": [str(item) for item in allow]}


def _default_title(ticket_text: str) -> str:
    for line in (ticket_text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return "Pre-test"


def _refuse_active(existing: dict[str, Any]) -> errors.AutonomError:
    return errors.AutonomError(
        errors.PRETEST_ALREADY_ACTIVE,
        f"pre-test {existing.get('pretest_id')} is still active in this session",
        hint="Finish it with 'autonom pretest pack' or 'autonom pretest stop' first.",
        pretest_id=existing.get("pretest_id"))


def create(session: dict[str, Any] | None, *, target: Any, ticket_text: str,
           ticket_ref: str | None, title: str | None, repo: str | Path | None,
           base: str | None, head: str | None, flows_dir: str | Path | None,
           app_id: str | None, budget: dict[str, Any] | None,
           parent_pack: str | Path | None = None, kind: str = "pretest",
           rerun_of: dict[str, Any] | None = None,
           context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create and activate a pre-test. Refusals create nothing.

    ``context`` lets a child pre-test (more-checks) reuse its parent's diff
    context instead of recomputing it.
    """
    if not session:
        raise errors.AutonomError(
            errors.NO_ACTIVE_SESSION, "no active session",
            hint="Start one with 'autonom session start --serial <id> --app-id <app>'.")
    existing = active(session)
    if existing is not None:
        raise _refuse_active(existing)
    if kind not in KINDS:
        raise errors.AutonomError(errors.INVALID_VALUE, f"unknown pre-test kind {kind!r}",
                                  hint="Kinds: " + ", ".join(KINDS) + ".")
    if context is None and not base:
        raise errors.AutonomError(errors.INVALID_VALUE, "--base is required",
                                  hint="Name the branch the ticket's change is "
                                       "compared with, e.g. --base main.")
    if rerun_of is not None and (not isinstance(rerun_of, dict)
                                 or not isinstance(rerun_of.get("pack"), str)):
        raise errors.AutonomError(errors.INVALID_VALUE, "rerun_of must name a pack",
                                  hint="Internal: pass {'pack': path, 'checks': [...]}.")
    target_block = _target_block(target, session)
    app_id = app_id or session.get("app_id")
    budget_block = _normalize_budget(budget, app_id)
    repo_path = str(Path(repo).expanduser().resolve()) if repo else None
    flows_path = str(Path(flows_dir).expanduser().resolve()) if flows_dir else None
    ticket_clean = redact_text(ticket_text or "") or ""
    # Mask first, then cut (contract 8.2): the title comes from the masked ticket.
    clean_title = (clip(title, TITLE_LIMIT)
                   or clip(_default_title(ticket_clean), TITLE_LIMIT) or "Pre-test")

    # The expensive, fallible parts run before anything is written.
    from . import baseline as baseline_mod
    from . import context as context_mod

    if context is None:
        context_doc = context_mod.build(repo_path, base, head, flows_path, session, app_id)
    else:
        context_doc = json.loads(json.dumps(context))
    baseline_doc = baseline_mod.capture(target, session, app_id)

    with _root_lock(session):
        existing = active(session)
        if existing is not None:
            raise _refuse_active(existing)
        root = _ensure_dir(pretests_root(session))
        pretest_id = new_pretest_id()
        while (root / pretest_id).exists():
            pretest_id = new_pretest_id()
        directory = root / pretest_id
        directory.mkdir(mode=0o700)
        try:
            os.chmod(directory, 0o700)
            record: dict[str, Any] = {
                "schema": RECORD_SCHEMA,
                "pretest_id": pretest_id,
                "session_id": str(session.get("session_id") or "unknown"),
                "status": "active",
                "title": clean_title,
                "ticket_ref": clip(ticket_ref, TITLE_LIMIT) or None,
                "created_at": now(),
                "ended_at": None,
                "repo": repo_path,
                "base": base,
                "head": head,
                "flows_dir": flows_path,
                "target": target_block,
                "app_id": app_id,
                "budget": budget_block,
                "parent_pack": str(parent_pack) if parent_pack else None,
                "kind": kind,
                "rerun_of": contracts.redact_value(rerun_of) if rerun_of else None,
                "pack": None,
            }
            validate_record(record)
            write_text(directory / TICKET_FILE, ticket_clean)
            write_json(directory / CONTEXT_FILE, context_doc)
            write_json(directory / BASELINE_FILE, baseline_doc)
            write_json(directory / BUDGET_FILE, {"actions_used": 0, "refusals": []})
            write_json(directory / RECORD_FILE, record)
            _write_active(session, pretest_id, None)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return record


def newest_unpointed_active(session: dict[str, Any]) -> dict[str, Any] | None:
    """The newest record that still says ``active`` although no pointer names
    it (a crash between clearing the pointer and writing the final record)."""
    candidates = [record for record in list_records(session)
                  if record.get("status") == "active"]
    if not candidates:
        return None
    return max(candidates, key=lambda record: (str(record.get("created_at") or ""),
                                               record["pretest_id"]))


def stop(session: dict[str, Any] | None) -> dict[str, Any]:
    """Close the active pre-test without a pack. An open check is ended
    ``inconclusive`` so no check is left half open. Without a pointer, the
    newest record still marked active is stopped (crash recovery)."""
    if session:
        _clear_stale_pointer(session)
    if session and active(session) is None:
        orphan = newest_unpointed_active(session)
        if orphan is not None:
            return _stop_record(session, orphan)
    record = require_active(session)
    return _stop_record(session, record)


def _stop_record(session: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    ensure_mutable(record)
    from . import checks as checks_mod

    pretest_id = record["pretest_id"]
    with lock(session, pretest_id):
        # Re-read under the lock: a pack that won the race must stay packed.
        record = load(session, pretest_id)
        check_open_record(record)
        open_ids = [item["check_id"] for item in checks_mod.fold(session, record)
                    if item["state"] == "open"]
        for check_id in open_ids:
            checks_mod.end(session, record, check_id, verdict="inconclusive",
                           note="open at stop time")
        record = load(session, pretest_id)
        check_open_record(record)
        record["status"] = "stopped"
        record["ended_at"] = record.get("ended_at") or now()
        validate_record(record)
        _finish(session, record)
    return record


def _finish(session: dict[str, Any], record: dict[str, Any]) -> None:
    """Clear ``active.json`` first, then write the final record; the caller
    holds the pre-test lock. A crash between the two leaves a record that
    still says active but has no pointer: attribution stops at once, and
    ``create`` is free to start the next pre-test. The reverse order would
    leave a pointer that keeps stamping evidence with a closed pre-test."""
    pretest_id = record["pretest_id"]
    with _root_lock(session):
        cleared = _clear_active(session, pretest_id)
    try:
        save(session, record)
    except BaseException:
        if cleared:
            with _root_lock(session), contextlib.suppress(Exception):
                if active_ids(session) is None:
                    _write_active(session, pretest_id, None)
        raise


def _clear_stale_pointer(session: dict[str, Any]) -> None:
    """Drop an ``active.json`` whose pre-test is not active (written by an
    older build or left behind by a crash), so evidence stops naming it."""
    with contextlib.suppress(Exception):
        ids = active_ids(session)
        if not ids or active(session) is not None:
            return
        with _root_lock(session):
            ids = active_ids(session)
            if ids and active(session) is None:
                (pretests_root(session) / ACTIVE_FILE).unlink(missing_ok=True)


def mark_packed(session: dict[str, Any], record: dict[str, Any],
                pack_info: dict[str, Any]) -> dict[str, Any]:
    pretest_id = record["pretest_id"]
    if not isinstance(pack_info, dict) or not pack_info.get("path") \
            or not pack_info.get("integrity_sha256"):
        raise errors.AutonomError(errors.INVALID_VALUE,
                                  "pack info needs path and integrity_sha256",
                                  hint="Internal: pass the finalized pack's descriptor.")
    with lock(session, pretest_id):
        current = load(session, pretest_id)
        ensure_mutable(current)
        current["status"] = "packed"
        current["ended_at"] = current.get("ended_at") or now()
        current["pack"] = {"path": str(pack_info["path"]),
                           "integrity_sha256": str(pack_info["integrity_sha256"])}
        validate_record(current)
        _finish(session, current)
    record.clear()
    record.update(current)
    return current


def find_pack_record(pack_path: str | Path) -> dict[str, Any]:
    """The record inside a pack (``<pack>/pretest.json``)."""
    path = Path(pack_path).expanduser() / RECORD_FILE
    try:
        record = _read_json(path)
    except (OSError, json.JSONDecodeError):
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"{pack_path} holds no readable pretest.json",
            hint="Pass the pack directory 'autonom pretest pack' printed.",
            path=str(pack_path)) from None
    failures = _failures_for_record(record)
    if failures:
        raise errors.AutonomError(
            errors.NOT_A_PRETEST_PACK, f"{pack_path} does not hold a pre-test record",
            hint="Pass the pack directory 'autonom pretest pack' printed.",
            path=str(pack_path), failures=failures)
    return record


def read_context(session: dict[str, Any], pretest_id: str) -> dict[str, Any]:
    return _read_json(pretest_dir(session, pretest_id) / CONTEXT_FILE)


def read_baseline(session: dict[str, Any], pretest_id: str) -> dict[str, Any]:
    return _read_json(pretest_dir(session, pretest_id) / BASELINE_FILE)
