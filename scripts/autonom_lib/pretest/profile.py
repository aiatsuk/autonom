"""The app profile (tp-023): one file that tells a pre-test how to start an app.

Lookup has one fixed order (plan decision 5): the workspace's
``<repo>/.autonom/apps/<app-id>/profile.yaml`` first, then the home's
``$AUTONOM_HOME/apps/<app-id>/profile.yaml`` (``~/.autonom`` without
``AUTONOM_HOME``, the home the Runtime Map already uses). The first file that
exists is the profile; the other is never read. ``pretest context`` reports
the path used in ``context.profile`` (``null`` when neither exists).

The file is parsed with the strict YAML subset the flow files use
(``flow.parser``: block mappings and sequences, one-line ``[a, b]`` lists,
plain and quoted scalars, comments; no anchors, tags, block scalars or
duplicate keys). Every value is a string. Supported keys::

    schema: autonom.pretest-profile/v1      # optional
    login_flow: flows/login.yaml            # relative to the profile's folder
    warmup_flow: flows/warmup.yaml
    accounts:                               # names -> environment variable NAMES
      qa: PRETEST_QA_USER                   #   one variable
      buyer:
        user: AUTONOM_BUYER_USER            #   or one variable per field
        password: AUTONOM_BUYER_PASSWORD
    fixtures:                               # free-form test data, no secrets
      product: Blue mug
    flaky_screens: [Splash, Promo banner]
    events:                                 # analytics rules for `pretest events`
      - name: add_to_cart
        method: POST
        host: analytics.example.com         # exact or a glob (*.example.com)
        path: /v1/events*                   # a glob over the path without its query
        body_key: event                     # optional; dotted for nested JSON
        body_value: add_to_cart             # optional; needs body_key

A profile never holds a secret. ``load`` refuses (``profile_invalid``, with
the dotted key path and never the value) when a field whose name is a secret
word (``password``, ``token``, ``secret``, ``pin``, ...) holds anything other
than an environment variable name, when a value matches the App Skill
validator's credential pattern, or when the pre-test's free-text masking
(``store.redact_text``) would change a value (a bearer token, a JWT, a URL
with a password). A value written like a variable name (``PRETEST_QA_PASSWORD``,
upper case, digits and ``_``) is read as a name, never as a secret: that is
the documented limit of the check. ``accounts`` values must be variable
names that start with ``AUTONOM_`` or ``PRETEST_`` (then upper case, digits
and ``_``; ``ACCOUNT_VAR``): any other name makes the profile invalid, so a
profile can never point warm-up at an unrelated variable (``HOME``,
``AWS_SECRET_ACCESS_KEY``). ``pretest warmup`` hands the variables that are
set to the flows as secrets (``--secret``), so their values never reach a
file; it never reads any other variable.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .. import errors
from ..flow import parser as flow_parser
from . import store

PROFILE_FILE = "profile.yaml"
SCHEMA = "autonom.pretest-profile/v1"
KEYS = ("schema", "login_flow", "warmup_flow", "accounts", "fixtures", "flaky_screens",
        "events")
EVENT_KEYS = ("name", "method", "host", "path", "body_key", "body_value")
SOURCES = ("workspace", "home")
MAX_BYTES = 256 * 1024
MAX_EVENTS = 100
MAX_FLAKY = 200
VALUE_LIMIT = 500
MAX_DEPTH = 32

APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,199}$")  # app_skills.APP_ID_RE
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# The only environment variables a profile's accounts may name (fix round 5).
ACCOUNT_VAR = re.compile(r"^(?:AUTONOM|PRETEST)_[A-Z0-9_]+$")
ACCOUNT_VAR_RULE = ("must name an environment variable starting with AUTONOM_ or PRETEST_ "
                    "(then A-Z, 0-9, _), never hold a value")
METHOD = re.compile(r"^[A-Za-z]{1,16}$")
BODY_KEY = re.compile(r"^[^.\s]+(?:\.[^.\s]+)*$")
# The App Skill validator's credential pattern (app_skills.validate, SKILL.md).
APP_SKILL_CREDENTIAL = re.compile(r"(?i)(api[_-]?key|password|secret)\s*[:=]\s*\S+")
# Secret words for a field NAME (English and Russian, as the free-text masking).
_SECRET_WORDS = frozenset({
    "password", "passwd", "pwd", "pass", "passcode", "secret", "token", "apikey",
    "auth", "cookie", "session", "otp", "pin", "credential", "credentials",
    "пароль", "секрет", "токен", "ключ", "код",
})
_SECRET_PAIRS = (("api", "key"), ("access", "key"), ("private", "key"),
                 ("client", "secret"))
_NAME_SPLIT = re.compile(r"[\s_.\-]+|(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])")


def _invalid(message: str, hint: str, **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(errors.PROFILE_INVALID, message, hint=hint, **extra)


# --- lookup ---------------------------------------------------------------------------


def home_root() -> Path:
    home = os.environ.get("AUTONOM_HOME")
    return (Path(home) if home else Path.home() / ".autonom") / "apps"


def candidates(workspace: str | Path | None, app_id: str | None) -> list[tuple[str, Path]]:
    """``[(source, path)]`` in lookup order; empty for a missing or unsafe app id
    (an id with ``/`` or ``..`` names no profile folder)."""
    if not app_id or not APP_ID.match(str(app_id)) or ".." in str(app_id):
        return []
    out: list[tuple[str, Path]] = []
    if workspace:
        out.append(("workspace", Path(workspace).expanduser().resolve() / ".autonom" / "apps"
                    / app_id / PROFILE_FILE))
    out.append(("home", home_root() / app_id / PROFILE_FILE))
    return out


def locate(workspace: str | Path | None,
           app_id: str | None) -> tuple[str, Path] | None:
    """The first profile that exists, else None. Something at a profile path
    that is not a regular file is refused, never skipped: skipping it would
    silently read the other location."""
    for source, path in candidates(workspace, app_id):
        if path.is_file():
            return source, path
        if os.path.lexists(path):
            raise _invalid(f"{path} is not a regular file",
                           "Make profile.yaml a plain file, or remove it.",
                           path=str(path), source=source)
    return None


# --- parsing ---------------------------------------------------------------------------


def _plain(node: Any) -> Any:
    if isinstance(node, flow_parser.Scalar):
        return node.text
    if isinstance(node, flow_parser.Sequence):
        return [_plain(item) for item in node.items]
    if isinstance(node, flow_parser.Mapping):
        return {key.text: _plain(value) for key, value in node.pairs}
    return None


def parse_text(text: str, path: str) -> dict[str, Any]:
    """The document as plain dicts, lists and strings (``profile_invalid``
    with the position for anything the flow YAML subset refuses)."""
    text = flow_parser.strip_bom(text)
    try:
        node = flow_parser._Parser(text.split("\n"), 1, path).parse_top_mapping(  # noqa: SLF001
            empty_line=1)
    except RecursionError:
        raise _invalid("profile does not parse: it is nested too deeply",
                       f"Keep the profile at most {MAX_DEPTH} levels deep.", path=path) from None
    except errors.AutonomError as exc:
        extra = {key: exc.extra[key] for key in ("line", "column", "reason")
                 if key in exc.extra}
        raise _invalid(f"profile does not parse: {exc.message}",
                       "profile.yaml uses the flow files' YAML subset: block mappings "
                       "and sequences, [a, b] lists, plain or quoted values; no anchors, "
                       "tags, block scalars or duplicate keys.",
                       path=path, **extra) from None
    return _plain(node)


def read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise _invalid(f"cannot read {path}: {exc.strerror or exc}",
                       "Check the file's permissions.", path=str(path)) from None
    if len(data) > MAX_BYTES:
        raise _invalid(f"{path} is larger than {MAX_BYTES // 1024} KiB",
                       "Keep the profile short: flows, account variable names, "
                       "fixtures and event rules.", path=str(path))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid(f"{path} is not UTF-8 text", "Save profile.yaml as UTF-8.",
                       path=str(path)) from None
    return parse_text(text, str(path))


# --- credential check ------------------------------------------------------------------


def secret_name(name: str) -> bool:
    """A field name that names a secret (``password``, ``apiKey``, ``pin``,
    ``client_secret``, ``пароль``)."""
    words = [word.lower() for word in _NAME_SPLIT.split(str(name)) if word]
    joined = "".join(words)
    if joined in _SECRET_WORDS or any(word in _SECRET_WORDS for word in words):
        return True
    if any(stem in joined for stem in ("password", "passwd", "secret", "token", "apikey",
                                       "api_key", "парол", "секрет", "токен")):
        return True
    return any(first in words and second in words for first, second in _SECRET_PAIRS)


def _credential_reason(key: str | None, value: str) -> str | None:
    if key is not None and secret_name(key):
        if ENV_NAME.match(value):
            return None  # an environment variable NAME, not a value
        return "a secret-named field holds a value instead of an environment variable name"
    if APP_SKILL_CREDENTIAL.search(value) or (
            key is not None and APP_SKILL_CREDENTIAL.search(f"{key}: {value}")):
        return "credential-shaped value"
    if (store.redact_text(value) or "") != value:
        return "credential-shaped value (it would be masked as a secret)"
    return None


def credential_failures(doc: Any) -> list[dict[str, str]]:
    """``[{"path", "reason"}]`` for every credential-shaped value; never the value."""
    failures: list[dict[str, str]] = []

    def walk(node: Any, path: str, key: str | None) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                walk(value, f"{path}.{name}" if path else str(name), str(name))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", key)
        elif isinstance(node, str):
            reason = _credential_reason(key, node)
            if reason:
                failures.append({"path": path, "reason": reason})

    walk(doc, "", None)
    return failures


# --- validation ------------------------------------------------------------------------

_SECRET_HINT = ("Keep secrets out of the profile: name the environment variable that holds "
                "each one (accounts: qa: {user: PRETEST_QA_USER, password: "
                "PRETEST_QA_PASSWORD} as block lines; account variables start with AUTONOM_ "
                "or PRETEST_) and export it before 'autonom pretest warmup'.")


def _text(doc: dict[str, Any], key: str, failures: list[dict[str, str]]) -> str | None:
    value = doc.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        failures.append({"path": key, "reason": "must be a non-empty string"})
        return None
    return value.strip()


def _accounts(value: Any, failures: list[dict[str, str]]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        failures.append({"path": "accounts", "reason": "must be a mapping of account names"})
        return {}
    out: dict[str, Any] = {}
    for name, entry in value.items():
        where = f"accounts.{name}"
        if isinstance(entry, str):
            if not ACCOUNT_VAR.match(entry):
                failures.append({"path": where, "reason": ACCOUNT_VAR_RULE})
                continue
            out[name] = entry
        elif isinstance(entry, dict) and entry:
            fields: dict[str, str] = {}
            for field, var in entry.items():
                if not isinstance(var, str) or not ACCOUNT_VAR.match(var):
                    failures.append({"path": f"{where}.{field}", "reason": ACCOUNT_VAR_RULE})
                    continue
                fields[field] = var
            out[name] = fields
        else:
            failures.append({"path": where, "reason": "must be a variable name or a mapping "
                                                      "of fields to variable names"})
    return out


def _clip_tree(node: Any) -> Any:
    if isinstance(node, dict):
        return {str(key)[:VALUE_LIMIT]: _clip_tree(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_clip_tree(item) for item in node]
    return str(node)[:VALUE_LIMIT] if node is not None else None


def _events(value: Any, failures: list[dict[str, str]]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        failures.append({"path": "events", "reason": "must be a list of rules"})
        return []
    if len(value) > MAX_EVENTS:
        failures.append({"path": "events", "reason": f"at most {MAX_EVENTS} rules"})
        return []
    rules: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, rule in enumerate(value):
        where = f"events[{index}]"
        if not isinstance(rule, dict):
            failures.append({"path": where, "reason": "must be a mapping"})
            continue
        unknown = sorted(set(rule) - set(EVENT_KEYS))
        if unknown:
            failures.append({"path": where, "reason": "unknown key(s): " + ", ".join(unknown)})
            continue
        bad = [key for key, item in rule.items() if not isinstance(item, str) or not item.strip()]
        if bad:
            failures.append({"path": where, "reason": "values must be non-empty strings: "
                             + ", ".join(sorted(bad))})
            continue
        clean = {key: rule.get(key).strip() if rule.get(key) is not None else None
                 for key in EVENT_KEYS}
        if not clean["name"]:
            failures.append({"path": where, "reason": "needs a name"})
            continue
        if clean["name"] in names:
            failures.append({"path": f"{where}.name", "reason": "duplicate rule name"})
            continue
        if not (clean["method"] or clean["host"] or clean["path"]):
            failures.append({"path": where, "reason": "needs at least one of method, host, path"})
            continue
        if clean["method"] and not METHOD.match(clean["method"]):
            failures.append({"path": f"{where}.method", "reason": "an HTTP method such as POST"})
            continue
        if clean["body_value"] is not None and not clean["body_key"]:
            failures.append({"path": f"{where}.body_value", "reason": "needs body_key"})
            continue
        if clean["body_key"] and not BODY_KEY.match(clean["body_key"]):
            failures.append({"path": f"{where}.body_key",
                             "reason": "a key, or a dotted path such as event.name"})
            continue
        if clean["method"]:
            clean["method"] = clean["method"].upper()
        if clean["host"]:
            clean["host"] = clean["host"].lower()
        names.add(clean["name"])
        rules.append({key: (item[:VALUE_LIMIT] if isinstance(item, str) else item)
                      for key, item in clean.items()})
    return rules


def depth(node: Any) -> int:
    """Nesting depth, counted without recursion."""
    deepest = 0
    stack = [(node, 1)]
    while stack:
        item, level = stack.pop()
        deepest = max(deepest, level)
        if level > MAX_DEPTH:
            break
        if isinstance(item, dict):
            stack.extend((value, level + 1) for value in item.values())
        elif isinstance(item, list):
            stack.extend((value, level + 1) for value in item)
    return deepest


def validate(doc: Any, path: Path, source: str, app_id: str) -> dict[str, Any]:
    """The normalized profile, or ``profile_invalid`` listing every problem."""
    location = str(path)
    if not isinstance(doc, dict):
        raise _invalid(f"{location}: the profile must be a mapping",
                       "Write 'key: value' lines at the top level.", path=location)
    if depth(doc) > MAX_DEPTH:
        raise _invalid(f"{location} is nested more than {MAX_DEPTH} levels deep",
                       "Keep fixtures shallow: the profile is configuration, not data.",
                       path=location)
    secrets = credential_failures(doc)
    if secrets:
        where = ", ".join(item["path"] for item in secrets)
        raise _invalid(f"{location} holds a credential-shaped value at {where}",
                       _SECRET_HINT, path=location, failures=secrets)
    failures: list[dict[str, str]] = []
    unknown = sorted(set(doc) - set(KEYS))
    for key in unknown:
        failures.append({"path": key, "reason": "unknown key; keys: " + ", ".join(KEYS)})
    if doc.get("schema") is not None and doc.get("schema") != SCHEMA:
        failures.append({"path": "schema", "reason": f"must be {SCHEMA}"})
    folder = path.parent
    flows: dict[str, str | None] = {}
    for key in ("login_flow", "warmup_flow"):
        value = _text(doc, key, failures)
        flows[key] = str((folder / Path(value).expanduser()).resolve()) if value else None
    accounts = _accounts(doc.get("accounts"), failures)
    fixtures = doc.get("fixtures")
    if fixtures is not None and not isinstance(fixtures, (dict, list)):
        failures.append({"path": "fixtures", "reason": "must be a mapping or a list"})
        fixtures = None
    flaky = doc.get("flaky_screens")
    if flaky is None:
        flaky = []
    elif not isinstance(flaky, list) or not all(isinstance(item, str) for item in flaky):
        failures.append({"path": "flaky_screens", "reason": "must be a list of screen ids "
                                                            "or labels"})
        flaky = []
    elif len(flaky) > MAX_FLAKY:
        failures.append({"path": "flaky_screens", "reason": f"at most {MAX_FLAKY} entries"})
        flaky = []
    events = _events(doc.get("events"), failures)
    if failures:
        where = ", ".join(item["path"] for item in failures)
        hint = ("Supported keys: " + ", ".join(KEYS) + "; see docs/USAGE.md, "
                "'App profile and warm-up'.")
        if any(item["path"].startswith("accounts") for item in failures):
            hint = ("accounts may only name environment variables whose names start with "
                    "AUTONOM_ or PRETEST_ (upper case, digits, _), e.g. "
                    "accounts: qa: PRETEST_QA_USER. " + hint)
        raise _invalid(f"{location} is not a valid app profile ({where})",
                       hint, path=location, failures=failures)
    return {
        "schema": SCHEMA, "path": location, "source": source, "app_id": app_id,
        "login_flow": flows["login_flow"], "warmup_flow": flows["warmup_flow"],
        "accounts": accounts, "fixtures": _clip_tree(fixtures),
        "flaky_screens": [item[:VALUE_LIMIT] for item in flaky], "events": events,
    }


def load(workspace: str | Path | None, app_id: str | None) -> dict[str, Any] | None:
    """The app's profile (normalized, credential-free), or None when neither
    location has one. ``profile_invalid`` for a profile that is unreadable,
    does not parse, holds a credential-shaped value or has unknown keys."""
    found = locate(workspace, app_id)
    if found is None:
        return None
    source, path = found
    return validate(read(path), path, source, str(app_id))


def account_variables(profile: dict[str, Any] | None) -> list[str]:
    """Every environment variable name the profile's accounts name, in order;
    only ``AUTONOM_``/``PRETEST_`` names (``ACCOUNT_VAR``), whatever the dict
    holds."""
    names: list[str] = []
    for entry in ((profile or {}).get("accounts") or {}).values():
        values = entry.values() if isinstance(entry, dict) else [entry]
        for name in values:
            if isinstance(name, str) and ACCOUNT_VAR.match(name) and name not in names:
                names.append(name)
    return names


def context_block(workspace: str | Path | None, app_id: str | None) -> dict[str, Any] | None:
    """What ``context.json`` records: the profile with the path used, or None."""
    return load(workspace, app_id)
