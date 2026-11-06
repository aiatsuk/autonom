"""Steps to reproduce (tp-011): what was done during a check, from the journal.

``steps(session, check)`` reads the session journal between the check's
``seq_from`` and ``seq_to`` (to the end for the open check; nothing for a
check that never began) and returns one step per state-changing entry, in
journal order::

    {"seq", "ts", "verb", "args": [...], "selector": {...}?, "origin",
     "mocks_active": [{"match"|"url", "method"?, "host"?, "status"?}, ...]}

Read-only verbs (trees, screenshots, journal, pre-test bookkeeping, ...) and
failed or refused actions are left out: they changed nothing on the device.
Flow steps appear as ``flow step`` with the flow command and label.

Redaction, on top of the journal's own scrub:

- the text of ``ui type`` is always ``<redacted>``, whatever the field;
- ``KEY=VALUE`` arguments keep the key and lose the value;
- values of secret-bearing flags (``--json``, ``--header``, ``--env`` ...)
  are ``<redacted>``;
- every other argument goes through the pre-test free-text masking.

``mocks_active`` lists the mock rules that were active (enabled) at that
step, each with its state at that time (``id``, ``url`` or ``match``,
``method``, ``host``, ``status`` after any ``mock update``, ``mode`` when the
rule is a ``rewrite`` rule, ``conditions`` when it has request conditions (a
count: query, request-header and body conditions together), ``enabled``),
replayed in journal order. While a pre-test is active the CLI journals the
whole registry as a ``mock_state`` entry after every ``network mock`` change
and at ``pretest start`` (``mock_state_entry``); such an entry is the truth
from that point on, and a ``network mock`` step takes the state its own
change produced. Without those entries (an older journal) the ``network
mock`` actions themselves are replayed: ``add`` appends an enabled rule,
``update``/``enable``/``disable``/``remove`` act on the rule with that id
(or on the only known rule when the id was never seen), ``--all`` on every
rule, ``clear`` empties the list. A step with no active mock lists none.

A ``network mock add|update`` step keeps its condition and edit flags
(``--query``, ``--request-header``, ``--body-contains``, ``--body-json``,
``--remove-header``, ``--json-set``, ``--json-remove``, ``--replace``) so the
step still reads as the command that was run. The CLI journals their values
already masked, with a count word per kind (``query=2``); a journal written
some other way may hold the values, and they are shown only when they cannot
be a credential: a query value unless its name looks secret, a header
condition unless its header is sensitive, a JSON field unless its last path
segment is a credential field. ``--body-contains`` text and ``--replace``
text are always ``<redacted>``.

``for_finding`` cuts a finding's steps at its evidence: when its refs
resolve to times (a screenshot's capture time, an action detail or tree
file's write time, a request's start or finish, a log window's end, a
crash's time, a flow run's finish), the steps run from the check start to
the latest of them (plus ``EVIDENCE_SLACK_S`` for second-resolution
stamps), not to the end of the check.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import errors
from .. import journal as journal_mod
from ..network import mocks as mocks_mod
from ..network import redact
from . import store

REDACTED = redact.PLACEHOLDER

READ_ONLY_VERBS = (
    "pretest", "journal", "note", "shots", "screenshot", "status", "doctor",
    "devices", "processes", "report", "logs", "metrics", "crash", "atlas",
    "runtime-map", "proof", "canvas", "tool-health", "help",
    "ui tree", "ui find", "ui dump", "ui snapshot", "ui inspect", "ui screen",
    "network requests", "network status", "network export",
    "session status", "session outputs", "session list", "flow check",
    "flow validate", "flow list", "flow show", "flow fmt",
)
SELECTOR_FLAGS = {
    "--text": "text", "--desc": "desc", "--resource-id": "resource_id",
    "--class-name": "class_name", "--package": "package", "--role": "role",
    "--clickable": "clickable", "--enabled": "enabled", "--mode": "mode",
    "--index": "index",
}
SECRET_FLAGS = frozenset({
    "--json", "--header", "--setenv", "--data", "--body", "--raw", "--value",
    "--token", "--secret", "--password", "--env",
})
MOCK_FLAGS = {"--url": "url", "--match": "match", "--method": "method",
              "--host": "host", "--status": "status"}
# Value-taking flags of `network mock add|update`: their values are never the
# rule id.
MOCK_VALUE_FLAGS = frozenset({"--url", "--match", "--method", "--host", "--status",
                              "--header", "--json", "--body-file", "--note", "--delay"})
# Condition and edit flags of `network mock add|update`: option -> (count word,
# values taken). They are spelled in full only (the CLI matches no abbreviation
# of them), so they sit apart from the abbreviable tables above.
MOCK_COUNTED_FLAGS = {
    "--query": ("query", 1), "--request-header": ("request_header", 1),
    "--body-contains": ("body_contains", 1), "--body-json": ("body_json", 1),
    "--remove-header": ("remove_header", 1), "--json-set": ("json_set", 1),
    "--json-remove": ("json_remove", 1), "--replace": ("replace", 2),
}
CONDITION_WORDS = ("query", "request_header", "body_contains", "body_json")
# `query=2`: what the CLI journals for a kind of condition or edit.
_COUNT_WORD = re.compile(r"\A(?P<word>query|request_header|body_contains|body_json|"
                         r"remove_header|json_set|json_remove|replace)=(?P<count>\d{1,4})\Z")
MOCK_STATE_KIND = "mock_state"
_PAIR = re.compile(r"\A(?P<key>[A-Za-z_][A-Za-z0-9_.\-]{0,127}+)=")
MAX_STEPS = 500
# Screenshot capture times have whole seconds: a step journaled in the same
# second as the evidence still belongs before it.
EVIDENCE_SLACK_S = 1.0


def _flag(token: str, table: dict[str, str] | frozenset[str]) -> str | None:
    """The full flag ``token`` names in ``table``: exact, or an unambiguous
    abbreviation of at least three letters (the journal's own scrub already
    masked shorter abbreviations of secret flags)."""
    if not token.startswith("--") or len(token) <= 2:
        return None
    if token in table:
        return token
    if len(token) < 5:
        return None  # `--to` is `ui swipe --to`, not `--token`: short forms stay exact
    hits = [name for name in table if name.startswith(token)]
    return hits[0] if len(hits) == 1 else None


def _mask_pair(token: str) -> str | None:
    match = _PAIR.match(token)
    return f"{match.group('key')}={REDACTED}" if match else None


def _secret_value(value: str) -> str:
    return _mask_pair(value) or REDACTED


def _command_path(argv: list[str], verb: str) -> tuple[list[str], set[int]]:
    """The argv indexes of the verb's words (to drop them from the args)
    and the bare words that follow the verb (subcommand words)."""
    try:
        words, terminator = journal_mod._bare_words(argv)  # noqa: SLF001
    except Exception:  # noqa: BLE001 — an odd argv still yields steps
        words, terminator = [i for i, t in enumerate(argv) if not t.startswith("-")], None
    wanted = verb.split()
    path: set[int] = set()
    for index, word in zip(words, wanted):
        if argv[index] != word:
            break
        path.add(index)
    rest = [argv[i] for i in words if i not in path
            and (terminator is None or i < terminator)]
    return rest, path


def _typed_indexes(argv: list[str]) -> set[int]:
    try:
        return set(journal_mod._typed_text_indexes(argv))  # noqa: SLF001
    except Exception:  # noqa: BLE001 — mask every non-option token instead
        seen_type = False
        out: set[int] = set()
        for index, token in enumerate(argv):
            if seen_type and not token.startswith("--"):
                out.add(index)
            if token == "type":
                seen_type = True
        return out


def _is_ui_type(verb: str, argv: list[str]) -> bool:
    if verb == "ui type":
        return True
    try:
        words, _ = journal_mod._bare_words(argv)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False
    return [argv[i] for i in words[:2]] == ["ui", "type"]


def _query_text(value: str) -> str:
    """``NAME`` or ``NAME=GLOB`` of a ``--query``: the value stays unless the
    name looks like a credential."""
    name, equals, _rest = value.partition("=")
    if equals and redact.is_sensitive_query_key(name):
        return f"{store.clip(name, 100)}={REDACTED}"
    return store.clip(value, 300) or ""


def _header_text(value: str) -> str:
    """``Name`` or ``Name: GLOB`` of a ``--request-header``: the value stays
    unless the header carries credentials."""
    name, colon, _rest = value.partition(":")
    lowered = name.strip().lower()
    if colon and (lowered in redact.REDACTED_HEADERS or journal_mod.secret_header_name(name)):
        return f"{store.clip(name, 100)}: {REDACTED}"
    return store.clip(value, 300) or ""


def _field_text(value: str) -> str:
    """``PATH=VALUE`` of ``--body-json`` / ``--json-set``: the value stays
    unless the last path segment is a credential field."""
    path, equals, _rest = value.partition("=")
    if equals and path.strip().rsplit(".", 1)[-1].lower() in redact.SENSITIVE_FIELDS:
        return f"{store.clip(path, 100)}={REDACTED}"
    return store.clip(value, 300) or ""


def _mock_flag_values(flag: str, values: list[str]) -> list[str]:
    """The values of a condition or edit flag as a step may show them.

    Names and paths stay; values that cannot be ruled out as credentials are
    ``<redacted>`` (``--body-contains`` and ``--replace`` text always is).
    """
    if flag == "--query":
        return [_query_text(value) for value in values]
    if flag == "--request-header":
        return [_header_text(value) for value in values]
    if flag in ("--body-json", "--json-set"):
        return [_field_text(value) for value in values]
    if flag in ("--remove-header", "--json-remove"):
        return [store.clip(value, 300) or "" for value in values]
    return [REDACTED for _value in values]  # --body-contains, --replace


def redact_args(verb: str, argv: list[Any]) -> tuple[list[str], dict[str, Any]]:
    """(args, selector) of one journaled command, redacted; the verb's own
    words are dropped from the args."""
    tokens = [str(token) for token in (argv or [])]
    typed = _typed_indexes(tokens) if _is_ui_type(verb, tokens) else set()
    _rest, path = _command_path(tokens, verb)
    with_selector = verb.startswith("ui ") and verb != "ui type"
    is_mock = verb == "network mock"
    args: list[str] = []
    selector: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if index in path:
            index += 1
            continue
        if index in typed:
            args.append(REDACTED)
            index += 1
            continue
        if is_mock and token in MOCK_COUNTED_FLAGS:
            width = MOCK_COUNTED_FLAGS[token][1]
            values = tokens[index + 1:index + 1 + width]
            args.extend([token, *_mock_flag_values(token, values)])
            index += 1 + len(values)
            continue
        if is_mock and token.startswith("--") and token.partition("=")[0] in MOCK_COUNTED_FLAGS:
            name, _, value = token.partition("=")
            args.append(f"{name}={_mock_flag_values(name, [value])[0]}")
            index += 1
            continue
        if is_mock and _COUNT_WORD.match(token):
            args.append(token)  # `query=2`: a kind and its count, never a value
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            if _flag(name, SECRET_FLAGS):
                args.append(f"{name}={_secret_value(value)}")
            elif with_selector and _flag(name, SELECTOR_FLAGS):
                selector[SELECTOR_FLAGS[_flag(name, SELECTOR_FLAGS)]] = \
                    store.clip(value, 300)
            else:
                args.append(f"{name}={_mask_pair(value) or store.clip(value, 500)}")
            index += 1
            continue
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None
        if _flag(token, SECRET_FLAGS) and nxt is not None:
            args.extend([token, REDACTED if (index + 1) in typed else _secret_value(nxt)])
            index += 2
            continue
        if with_selector and _flag(token, SELECTOR_FLAGS) and nxt is not None \
                and (index + 1) not in path:
            selector[SELECTOR_FLAGS[_flag(token, SELECTOR_FLAGS)]] = store.clip(nxt, 300)
            index += 2
            continue
        args.append(_mask_pair(token) or store.clip(token, 500) or "")
        index += 1
    return args, selector


def _read_only(verb: str, rest: list[str]) -> bool:
    for prefix in READ_ONLY_VERBS:
        if verb == prefix or verb.startswith(prefix + " "):
            return True
    return verb == "network mock" and bool(rest) and rest[0] in ("list", "show")


def _kind_counts(argv: list[str]) -> dict[str, int]:
    """How many conditions and edits of each kind a ``network mock`` argv
    gave: the CLI's count words (``query=2``) when they are there, else the
    flags themselves, once each."""
    words: dict[str, int] = {}
    for token in argv:
        match = _COUNT_WORD.match(token)
        if match:
            words[match.group("word")] = int(match.group("count"))
    if words:
        return words
    flags: dict[str, int] = {}
    for token in argv:
        counted = MOCK_COUNTED_FLAGS.get(token.partition("=")[0]) if token.startswith("--") \
            else None
        if counted:
            flags[counted[0]] = flags.get(counted[0], 0) + 1
    return flags


def _mock_rule(argv: list[str]) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    for token in argv:  # the CLI refuses both flags together
        name = token.partition("=")[0] if token.startswith("--") else token
        if name == "--rewrite":
            rule["mode"] = "rewrite"
        elif name == "--no-rewrite":
            rule["mode"] = "respond"
    given = _kind_counts(argv)
    cleared = any(token == "--clear-conditions" for token in argv)
    if cleared or any(given.get(word) for word in CONDITION_WORDS):
        # A list that is given replaces the stored one; an update that names
        # only some kinds is counted as if it named every kind it keeps.
        rule["conditions"] = sum(given.get(word, 0) for word in CONDITION_WORDS)
    for index, token in enumerate(argv):
        name, value = token, None
        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
        elif index + 1 < len(argv):
            value = argv[index + 1]
        full = _flag(name, MOCK_FLAGS)
        if full and value is not None:
            key = MOCK_FLAGS[full]
            if key == "status":
                rule[key] = int(value) if value.isdigit() else store.clip(value, 16)
            else:
                rule[key] = store.clip(value, 300)
    return rule


def _mock_words(argv: list[str]) -> list[str]:
    """The bare words of a ``network mock`` argv, values of its own
    value-taking flags left out (``--status 404`` is no rule id)."""
    words: list[str] = []
    skip = 0
    for token in argv:
        if skip:
            skip -= 1
            continue
        if token == "--":
            continue
        if token.startswith("--"):
            if "=" not in token:
                if token in MOCK_COUNTED_FLAGS:
                    skip = MOCK_COUNTED_FLAGS[token][1]
                elif _flag(token, MOCK_VALUE_FLAGS):
                    skip = 1
            continue
        if token.startswith("-") or _COUNT_WORD.match(token):
            continue
        words.append(token)
    return words


def _mock_target(argv: list[str], action: str) -> str | None:
    """The rule id an ``update``/``enable``/``disable``/``remove`` names."""
    words = _mock_words(argv)
    try:
        index = words.index(action)
    except ValueError:
        return None
    return words[index + 1] if index + 1 < len(words) else None


def _find_rule(mocks: list[dict[str, Any]], identifier: str | None
               ) -> dict[str, Any] | None:
    """The rule with ``identifier``; else the only rule whose id was never
    seen (an ``add`` journals no id), adopting the id."""
    if identifier:
        for rule in mocks:
            if rule.get("id") == identifier:
                return rule
    unnamed = [rule for rule in mocks if not rule.get("id")]
    if len(unnamed) == 1:
        if identifier:
            unnamed[0]["id"] = store.clip(identifier, 64)
        return unnamed[0]
    return None


def _apply_mock(mocks: list[dict[str, Any]], argv: list[str], rest: list[str]) -> None:
    """Replay one successful ``network mock`` action on ``mocks`` (the
    registry as far as the journal tells it)."""
    action = rest[0] if rest else None
    all_flag = any(_flag(token.split("=", 1)[0], frozenset({"--all"})) for token in argv)
    if action == "add":
        rule = {"id": None, **_mock_rule(argv), "enabled": True}
        if rule.get("mode") != "rewrite":
            rule.setdefault("status", 200)  # a rewrite rule keeps the real status
        mocks.append(rule)
    elif action == "clear":
        mocks.clear()
    elif action in ("enable", "disable") and all_flag:
        for rule in mocks:
            rule["enabled"] = action == "enable"
    elif action in ("update", "enable", "disable", "remove"):
        rule = _find_rule(mocks, _mock_target(argv, action))
        if rule is None:
            return
        if action == "update":
            rule.update(_mock_rule(argv))
        elif action == "remove":
            mocks.remove(rule)
        else:
            rule["enabled"] = action == "enable"


def _public_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """One active rule as a step shows it."""
    out: dict[str, Any] = {}
    if rule.get("id"):
        out["id"] = rule["id"]
    for key in ("url", "match", "method", "host", "status"):
        if rule.get(key) is not None:
            out[key] = rule[key]
    if rule.get("mode") == "rewrite":
        out["mode"] = "rewrite"  # a fake answer is the default and stays unnamed
    conditions = rule.get("conditions")
    if isinstance(conditions, int) and not isinstance(conditions, bool) and conditions > 0:
        out["conditions"] = conditions
    out["enabled"] = True
    return out


def _active_rules(mocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_public_rule(rule) for rule in mocks if rule.get("enabled", True) is not False]


def _glob_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if "://" in value:
        value = redact.scrub_url(value) or value
    return store.clip(value, 300)


def mock_state_entry(rules: Any) -> dict[str, Any]:
    """The ``mock_state`` journal entry for the registry ``rules``
    (``network.mocks.load()``): per rule its id, URL glob, method, host,
    status, enabled flag, ``mode`` (``rewrite`` rules only) and ``conditions``
    (how many request conditions, when there are any); never headers, bodies,
    notes, or the values of a condition or an edit."""
    mocks: list[dict[str, Any]] = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
        response = rule.get("response") if isinstance(rule.get("response"), dict) else {}
        item: dict[str, Any] = {"id": store.clip(rule.get("id"), 64),
                                "enabled": rule.get("enabled", True) is not False}
        glob = _glob_text(match.get("url_glob"))
        if glob:
            item["match"] = glob
        if match.get("method"):
            item["method"] = store.clip(match.get("method"), 16)
        if match.get("host"):
            item["host"] = store.clip(match.get("host"), 255)
        status = response.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            item["status"] = status
        if mocks_mod.rule_mode(rule) == "rewrite":
            item["mode"] = "rewrite"
        conditions = sum(mocks_mod.condition_counts(rule).values())
        if conditions:
            item["conditions"] = conditions
        mocks.append(item)
    return {"kind": MOCK_STATE_KIND, "mocks": mocks}


def _from_state(entry: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rule in entry.get("mocks") or []:
        if isinstance(rule, dict):
            out.append({key: rule.get(key) for key in
                        ("id", "url", "match", "method", "host", "status", "mode",
                         "conditions", "enabled")
                        if key in rule})
    return out


def _range(check: dict[str, Any]) -> tuple[int, int | None] | None:
    seq_from = check.get("seq_from")
    if isinstance(seq_from, bool) or not isinstance(seq_from, int):
        return None
    seq_to = check.get("seq_to")
    if isinstance(seq_to, bool) or not isinstance(seq_to, int):
        seq_to = None
    return seq_from, seq_to


def _flow_step(entry: dict[str, Any], mocks: list[dict[str, Any]]) -> dict[str, Any]:
    step: dict[str, Any] = {
        "seq": entry.get("seq"), "ts": entry.get("ts"), "verb": "flow step",
        "args": [store.clip(entry.get("command"), 100) or ""], "origin": "flow",
        "mocks_active": _active_rules(mocks)}
    if entry.get("label"):
        step["label"] = store.clip(entry.get("label"), 200)
    if entry.get("run_id"):
        step["run_id"] = str(entry.get("run_id"))
    return step


def steps(session: dict[str, Any], check: dict[str, Any]) -> list[dict[str, Any]]:
    """The reproduction steps of one folded check (see the module text)."""
    if not isinstance(check, dict):
        raise errors.AutonomError(errors.INVALID_VALUE, "repro needs a folded check",
                                  hint="Pass an item of 'checks.fold'.")
    window = _range(check)
    if window is None:
        return []
    seq_from, seq_to = window
    mocks: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    entries = [entry for entry in store.read_lines(journal_mod.journal_path(session))
               if isinstance(entry, dict)]
    for position, entry in enumerate(entries):
        seq = entry.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            continue
        if seq_to is not None and seq > seq_to:
            break
        inside = seq >= seq_from
        kind = entry.get("kind")
        if kind == MOCK_STATE_KIND:
            mocks = _from_state(entry)
            continue
        if kind == "flow_step":
            if inside and entry.get("ok") is not False and len(out) < MAX_STEPS:
                out.append(_flow_step(entry, mocks))
            continue
        if kind != "action" or entry.get("ok") is False:
            continue
        verb = str(entry.get("verb") or "").strip()
        if not verb:
            continue
        raw_argv = entry.get("argv")
        argv = [str(token) for token in raw_argv] if isinstance(raw_argv, list) else []
        rest, _path = _command_path(argv, verb)
        if verb == "network mock" and not _read_only(verb, rest):
            following = entries[position + 1] if position + 1 < len(entries) else None
            if isinstance(following, dict) and following.get("kind") == MOCK_STATE_KIND:
                mocks = _from_state(following)  # the state this change produced
            else:
                _apply_mock(mocks, argv, rest)
        if not inside or _read_only(verb, rest) or len(out) >= MAX_STEPS:
            continue
        args, selector = redact_args(verb, argv)
        step: dict[str, Any] = {"seq": seq, "ts": entry.get("ts"), "verb": verb,
                                "args": args}
        if selector:
            step["selector"] = selector
        step["origin"] = str(entry.get("origin") or "agent")
        step["mocks_active"] = _active_rules(mocks)
        out.append(step)
    return out


def _parse(value: Any) -> datetime | None:
    from .checks import parse_ts

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return parse_ts(value) if isinstance(value, str) else None


def _file_time(path: Any) -> datetime | None:
    if not path:
        return None
    try:
        return _parse(os.stat(path).st_mtime)
    except (OSError, TypeError, ValueError):
        return None


def _request_time(path: Any, flow_id: str) -> datetime | None:
    for row in store.read_lines(Path(path)) if path else []:
        if isinstance(row, dict) and str(row.get("id")) == flow_id:
            for key in ("finished_at_ms", "started_at_ms"):
                millis = row.get(key)
                if isinstance(millis, (int, float)) and not isinstance(millis, bool):
                    return _parse(millis / 1000.0)
            return _parse(row.get("started_at"))
    return None


def evidence_time(session: dict[str, Any], ref: Any) -> datetime | None:
    """When the evidence ``ref`` was made, or None when it does not resolve
    or carries no time (see the module text)."""
    from . import findings as findings_mod

    try:
        resolved = findings_mod.resolve_ref(session, ref)
    except Exception:  # noqa: BLE001 — an unresolvable ref cuts nothing
        return None
    kind, data = resolved.get("kind"), resolved.get("data") or {}
    path = resolved.get("path")
    if kind == "shot":
        return _parse(data.get("captured_at")) or _file_time(path)
    if kind in ("action", "tree"):
        return _file_time(path)
    if kind == "request":
        return _request_time(path, str(data.get("id")))
    if kind == "log":
        return _parse(data.get("to"))
    if kind == "crash":
        return _parse(data.get("time")) or _file_time(path)
    if kind == "run" and path:
        try:
            doc = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        millis = doc.get("finished_at_ms") if isinstance(doc, dict) else None
        if isinstance(millis, (int, float)) and not isinstance(millis, bool):
            return _parse(millis / 1000.0)
    return None


def cut_at_evidence(session: dict[str, Any], finding: dict[str, Any],
                    check_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``check_steps`` up to the latest time a ``finding``'s evidence refs
    resolve to (all of them when no ref has a time)."""
    refs = finding.get("evidence") if isinstance(finding, dict) else None
    times = [stamp for stamp in (evidence_time(session, ref) for ref in refs or [])
             if stamp is not None]
    if not times:
        return [dict(step) for step in check_steps]
    cutoff = max(times) + timedelta(seconds=EVIDENCE_SLACK_S)
    out = []
    for step in check_steps:
        stamp = _parse(step.get("ts"))
        if stamp is not None and stamp > cutoff:
            break
        out.append(dict(step))
    return out


def for_finding(session: dict[str, Any], finding: dict[str, Any],
                checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The steps of the check a finding belongs to, from the check start to
    its latest evidence; none without a check."""
    check_id = finding.get("check_id") if isinstance(finding, dict) else None
    for check in checks or []:
        if isinstance(check, dict) and check.get("check_id") == check_id and check_id:
            return cut_at_evidence(session, finding, steps(session, check))
    return []
