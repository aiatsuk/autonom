"""Session journal: an append-only timeline of everything a session did.

`<artifacts_dir>/journal.ndjson` gets one JSON line per event — every CLI verb
(what ran, its scrubbed arguments, whether it succeeded, and the artifact it
produced), plus freeform notes an agent writes with `autonom note`. Read it
back with `autonom journal` for a full account of a run: which taps happened,
which screens were captured, which mocks were in force, what the agent
concluded.

Two hard rules:

- **Best-effort.** A journal write must never fail the command it records. Every
  entry point swallows its own errors — losing a line is acceptable, breaking a
  tap is not.
- **No secrets.** The journal lands on disk, so it gets the same treatment as
  captured traffic: the text typed into a field (often a password) is reduced to
  its length, and the values behind body/header/env flags are masked. What the
  agent chooses to write in a note is its own responsibility.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import errors
from .network import redact

JOURNAL_FILE = "journal.ndjson"

# Flags whose *value* can carry a credential, in both `--flag value` and
# `--flag=value` form. `--token` is the Canvas bearer token; `--secret` names
# an environment variable today, but masking it costs nothing and keeps a
# future value-taking form safe.
_SENSITIVE_VALUE_FLAGS = {
    "--json", "--header", "--setenv", "--data", "--body", "--raw", "--value",
    "--token", "--secret", "--password",
}
# argparse accepts any unambiguous prefix, down to one letter (`--j`, `--v`,
# `--head`, `--tok`), so ANY `--x` token that is a prefix of a sensitive flag
# counts as that flag. Masking an ambiguous prefix argparse would reject
# anyway costs nothing. The only exemptions are prefixes that are themselves
# exact, non-secret flags under a specific command path, where argparse picks
# the exact flag. `tests/test_security_hardening.py` walks the real parser
# and fails if this table misses one.
_EXACT_NON_SECRET_PREFIXES: dict[tuple[str, ...], frozenset[str]] = {
    ("ui", "swipe"): frozenset({"--to"}),
    ("teach", "compile"): frozenset({"--to"}),
    ("atlas", "paths"): frozenset({"--to"}),
    ("atlas", "diff"): frozenset({"--head"}),
    ("runtime-map", "paths"): frozenset({"--to"}),
    ("runtime-map", "diff"): frozenset({"--head"}),
    ("proof",): frozenset({"--head"}),
}
# Namespace dests whose exact string values are secrets wherever they appear.
_SENSITIVE_DESTS = ("token", "secret", "password", "header", "value", "json",
                    "setenv", "data", "body", "raw")
# Target flags that take a value (global and per-verb). In the argv-only
# fallback these, and every argparse abbreviation of them (`--ser`, `--plat`),
# are skipped together with their value when looking for the command path or
# the typed text of `ui type`; every other bare word counts.
_TARGET_VALUE_FLAGS = {
    "--platform", "--target", "--serial", "--udid", "--adb", "--simctl",
    "--idb", "--idb-host", "--idb-port",
}
# Value-taking target flags argparse matches only when spelled in full
# (`EXACT_ONLY_OPTIONS`): added after abbreviations of the older flags were in
# use, so `--u` stays `--udid` and `--se` stays `--serial`.
EXACT_ONLY_OPTIONS = frozenset({"--session-id", "--ui-backend"})
# Fields worth lifting from a command's result into the timeline summary.
# Deliberately excludes anything free-form or body-shaped ("typed", previews).
_SUMMARY_KEYS = (
    "target_id", "platform", "saved", "path", "count", "matched", "gesture",
    "booted", "stopped", "via", "mocks_active", "mocks", "hits", "port",
    "har", "url", "app_id", "note", "status", "run_id", "detail",
    "ui_backend", "input_backend", "fallback_reason", "geometry", "component",
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def journal_path(session: dict[str, Any]) -> Path:
    return Path(session["artifacts_dir"]) / JOURNAL_FILE


def _next_seq(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle) + 1
    except OSError:
        return 1


def _mask(value: str) -> str:
    return f"<{len(value)} chars>"


def _bare_words(argv: list[str]) -> tuple[list[int], int | None]:
    """Indexes of bare argv words, and the index of a `--` terminator.

    Values of the target flags are not bare words
    (`autonom --serial X ui type ...`); a `--=value` form never takes the next
    token. A `--` met before the command path is complete (`-- ui type X`,
    `--plat ios -- ui type X`) is dropped by argparse and the subcommand still
    parses normally, so the scan continues past it. A later `--` ends the
    scan: everything after it is positional.
    """
    words: list[int] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            skip_next = False
        elif token == "--":
            if len(words) < 2:
                continue
            return words, index
        elif _target_flag(token):
            skip_next = True
        elif not token.startswith("-"):
            words.append(index)
    return words, None


def _target_flag(token: str) -> bool:
    """`token` is a value-taking target flag, or any abbreviation of one.

    `--ser X` is `--serial X` to argparse. Were `--ser` not recognised here,
    its value would read as the first word of the command path, and
    `--ser proof network mock add --head V` would look like `proof`. Every
    prefix counts, even one shared by several target flags (`--idb-`): all
    of them take a value, so the next token is a value either way. The
    `--flag=value` form carries its own value and never takes the next token.
    """
    if not token.startswith("--") or len(token) <= 2 or "=" in token:
        return False
    return token in EXACT_ONLY_OPTIONS or any(
        flag.startswith(token) for flag in _TARGET_VALUE_FLAGS)


def _is_ui_type(argv: list[str], args: Any = None) -> bool:
    if args is not None and getattr(args, "command", None) == "ui":
        return getattr(args, "ui_command", None) == "type"
    words, _terminator = _bare_words(argv)
    return [argv[index] for index in words[:2]] == ["ui", "type"]


def _typed_text_indexes(argv: list[str]) -> set[int]:
    """Indexes of `ui type` argv tokens that may be the typed text.

    Without the parsed namespace the position of the text is ambiguous
    (`ui type --serial X SECRET`, `ui type --sensitive SECRET`,
    `ui type -- -SECRET`, `ui type -9SECRET`), so this errs toward masking:
    after `type`, every token is text unless it is a recognised `ui type`
    option — a target flag (any abbreviation) with its value, `--sensitive`
    or `--help` (any abbreviation) — and everything after `--` is text.
    Dash-leading text (`-9x`, `-.5x`, `'-x y'`) is text too: argparse reads it
    as a positional.
    """
    words, _terminator = _bare_words(argv)
    if len(words) < 2:
        return set()
    indexes: set[int] = set()
    literal = False
    skip_value = False
    for index in range(words[1] + 1, len(argv)):
        token = argv[index]
        if literal:
            indexes.add(index)
        elif skip_value:
            skip_value = False
        elif token == "--":
            literal = True
        elif token == "-h" or _prefix_of(token, "--help") or _prefix_of(token, "--sensitive"):
            continue
        elif _target_flag(token):
            skip_value = True
        elif token.startswith("--") and "=" in token and _target_flag(token.split("=", 1)[0]):
            continue
        else:
            indexes.add(index)
    return indexes


def _prefix_of(token: str, flag: str) -> bool:
    """`token` is `flag` or an abbreviation of it (at least one letter)."""
    return len(token) > 2 and token.startswith("--") and flag.startswith(token)


def _head_rule_scrub(argv: list[str]) -> list[str]:
    """The exact argv rule of the previous release (bdb7044), kept verbatim.

    It is kept so the journal never masks less than that release did: every
    journaled argv is the per-token union of this rule, the argv-only scan and
    (when a parser is given) the parser-resolved scrub. Do not "improve" it;
    improvements belong in `scrub_argv` / `scrub_canonical`.
    """
    head_flags = {"--json", "--header", "--setenv", "--data", "--body", "--raw", "--value"}
    out: list[str] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        if token in head_flags:
            out.append(token)
            skip_next = True
            continue
        # `ui type <text>`: the positional after `type` is whatever was entered
        # into the focused field, which is exactly where a password shows up.
        if index >= 2 and argv[index - 1] == "type" and argv[index - 2] == "ui" \
                and not token.startswith("-"):
            out.append(f"<{len(token)} chars>")
            continue
        out.append(redact.scrub_body(token) if "=" in token or "{" in token else token)
    return out


_FULL_MASK = re.compile(r"(?:.*=)?\*\*\*$|<\d+ chars>$")


def _mask_rank(raw: str, scrubbed: str) -> int:
    """How strongly `scrubbed` hides `raw`: 3 full mask, 2 content scrubbed,
    1 respelled only (a canonical option name), 0 untouched."""
    if scrubbed == raw:
        return 0
    if _FULL_MASK.match(scrubbed) and not _FULL_MASK.match(raw):
        return 3
    if scrubbed.count(redact.PLACEHOLDER) > raw.count(redact.PLACEHOLDER):
        return 2
    return 1


def _most_masked(argv: list[str], candidates: list[list[str]]) -> list[str]:
    """Per token, the most masked of several 1:1 scrubs of `argv`.

    Ties keep the earliest candidate (the parser-resolved one when present,
    for its canonical spelling); among content scrubs the one hiding more
    fields wins.
    """
    for candidate in candidates:
        if len(candidate) != len(argv):
            raise ValueError("a scrubber did not map argv 1:1")
    merged: list[str] = []
    for index, raw in enumerate(argv):
        best = candidates[0][index]
        best_key = (_mask_rank(raw, best), best.count(redact.PLACEHOLDER))
        for candidate in candidates[1:]:
            key = (_mask_rank(raw, candidate[index]), candidate[index].count(redact.PLACEHOLDER))
            if key > best_key:
                best, best_key = candidate[index], key
        merged.append(best)
    return merged


def scrub_for_journal(argv: list[str], args: Any = None, parser: Any = None) -> list[str]:
    """What `record_action` writes: the union of every scrub that applies.

    Parser-resolved (when a parser is given and resolution succeeds), the
    argv-only scan, and the previous release's exact rule — so the journal is
    never less masked than any of them, by construction.
    """
    argv = [str(token) for token in argv]
    candidates: list[list[str]] = []
    if parser is not None:
        try:
            candidates.append(scrub_canonical(parser, argv, args))
        except Exception:  # noqa: BLE001 - the other scrubs still apply
            pass
    candidates.append(scrub_argv(argv, args))
    candidates.append(_head_rule_scrub(argv))
    return _most_masked(argv, candidates)


def _sensitive_flag(flag: str, path: tuple[str, ...]) -> bool:
    """`flag` is a secret-bearing flag or an argparse abbreviation of one.

    `path` is the leading bare words of argv (the command path), used only to
    honour `_EXACT_NON_SECRET_PREFIXES`.
    """
    if flag in _SENSITIVE_VALUE_FLAGS:
        return True
    if not flag.startswith("--") or len(flag) <= 2:
        return False
    if not any(full.startswith(flag) for full in _SENSITIVE_VALUE_FLAGS):
        return False
    for command, exact in _EXACT_NON_SECRET_PREFIXES.items():
        if flag in exact and path[:len(command)] == command:
            return False
    return True


def _namespace_secrets(args: Any) -> set[str]:
    """Exact string values of the secret-bearing dests of a parsed namespace."""
    found: set[str] = set()
    if args is None:
        return found
    for dest in _SENSITIVE_DESTS:
        value = getattr(args, dest, None)
        values = value if isinstance(value, (list, tuple)) else [value]
        found |= {item for item in values if isinstance(item, str) and item}
    return found


def scrub_argv(argv: list[str], args: Any = None) -> list[str]:
    """Mask credential-shaped arguments before they reach disk.

    `args` is the parsed `argparse.Namespace` when the caller has one. It is
    the authority on what the typed text of `ui type` is: the exact value is
    masked wherever it sits in argv. Without it, argv alone is scanned
    conservatively (see `_typed_text_indexes`).
    """
    typed: set[int] = set()
    if _is_ui_type(argv, args):
        typed = _typed_text_indexes(argv)
        text = getattr(args, "text", None) if args is not None else None
        if isinstance(text, str) and text:
            typed |= {index for index, token in enumerate(argv) if token == text}
    words, _terminator = _bare_words(argv)
    path = tuple(argv[index] for index in words[:3])
    secrets = _namespace_secrets(args)
    out: list[str] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        if index in typed:
            # Whatever was entered into the focused field is exactly where a
            # password shows up, so only its length survives.
            out.append(_mask(token))
            continue
        if _sensitive_flag(token, path):
            out.append(token)
            skip_next = True
            continue
        flag, equals, value = token.partition("=")
        if equals and (_sensitive_flag(flag, path) or (flag.startswith("-") and value in secrets)):
            out.append(f"{flag}=***")
            continue
        if token in secrets:
            out.append("***")
            continue
        out.append(_scrub_free(token))
    return out


# --- parser-resolved scrubbing ---------------------------------------------
#
# The argv-only scan above guesses what argparse will do. With the real parser
# in hand there is nothing to guess: every token is resolved exactly as
# argparse resolves it — along the actual subcommand chain, with unique
# prefixes and `--x=v` expanded to the full option string — and secrets are
# then matched on full option names and dests only. The command path comes
# from the chosen subparsers, never from bare words, so no value can pose as
# a command and no prefix table is needed.


class Token:
    """One argv token as argparse resolves it."""

    __slots__ = ("raw", "role", "option", "dest", "inline", "path", "spelling")

    def __init__(self, raw: str, role: str, *, option: str | None = None,
                 dest: str | None = None, inline: str | None = None,
                 path: tuple[str, ...] = (), spelling: str | None = None) -> None:
        self.raw = raw
        self.role = role          # option | value | command | positional | separator
        #                           | unknown (option argparse would reject)
        #                           | stray (positional with no slot)
        self.option = option      # canonical full option string, for option/value/unknown
        self.dest = dest          # argparse dest, for option/value/positional
        self.inline = inline      # the `=value` part of an option token, when present
        self.path = path          # command path in force at this token
        self.spelling = spelling  # the option as typed, without `=value`

    def text(self) -> str:
        if self.role == "option" and self.option:
            return self.option if self.inline is None else f"{self.option}={self.inline}"
        return self.raw


# Fallback for argparse's `_negative_number_matcher` when a parser does not
# carry one: 3.14 uses a prefix match, older releases a full match
# (`-5,10` is a negative number to 3.14 but an unknown option to 3.11).
_NEGATIVE_NUMBER = re.compile(r"-\.?\d") if sys.version_info >= (3, 14) \
    else re.compile(r"^-\d+$|^-\d*\.\d+$")


def _negative_number_matcher(parser: Any) -> Any:
    """The running parser's own negative-number rule (read-only)."""
    matcher = getattr(parser, "_negative_number_matcher", None)
    return matcher if hasattr(matcher, "match") else _NEGATIVE_NUMBER


def _canonical_option(action: Any) -> str:
    longs = [name for name in action.option_strings if name.startswith("--")]
    return longs[0] if longs else action.option_strings[0]


def _resolve_option(parser: Any, name: str) -> Any:
    """The action argparse would pick for option spelling `name`, or None."""
    table = parser._option_string_actions  # noqa: SLF001 - read-only
    if name in table:
        return table[name]
    if not getattr(parser, "allow_abbrev", True) or not name.startswith("--"):
        return None
    exact_only = getattr(parser, "exact_only_options", ())
    owners = {id(action): action for option, action in table.items()
              if option.startswith(name)
              and not set(action.option_strings) & set(exact_only)}
    return next(iter(owners.values())) if len(owners) == 1 else None


def _classify(parser: Any, raw: str) -> tuple[str, Any, str | None, str | None]:
    """Mirror `ArgumentParser._parse_optional` of the running Python for one token.

    Returns (kind, action, option spelling, inline value) where kind is
    `positional`, `option` or `unknown`. Same order as argparse: exact
    option, exact `--opt=value`, unique prefix (with or without `=`), then
    negative-number-like and space-containing tokens are positionals.
    """
    if not raw or raw[0] not in getattr(parser, "prefix_chars", "-"):
        return "positional", None, None, None
    table = parser._option_string_actions  # noqa: SLF001 - read-only
    if raw in table:
        return "option", table[raw], raw, None
    if len(raw) == 1:
        return "positional", None, None, None
    name, sep, inline = raw.partition("=")
    if sep and name in table:
        return "option", table[name], name, inline
    lookup = name if sep else raw
    action = _resolve_option(parser, lookup)
    if action is not None:
        return "option", action, lookup, inline if sep else None
    if _negative_number_matcher(parser).match(raw) \
            and not parser._has_negative_number_optionals:  # noqa: SLF001
        return "positional", None, None, None
    if " " in raw:
        return "positional", None, None, None
    return "unknown", None, lookup, inline if sep else None


def _takes(action: Any) -> str:
    """How many following tokens an option consumes: none, one, or many."""
    nargs = action.nargs
    if nargs == 0:
        return "none"
    if nargs is None or nargs == 1 or nargs == "?":
        return "one" if nargs != "?" else "optional"
    return "many"


def canonical_argv(parser: Any, argv: list[str]) -> list[Token]:
    """Resolve `argv` against the real parser, token by token.

    Read-only use of argparse internals: option tables and positional actions
    of each parser along the chosen subcommand chain. Nothing is parsed into a
    namespace and no action is invoked. A token argparse would not accept is
    kept as `unknown` (an unknown or ambiguous option) or `stray` (a
    positional with no slot); `scrub_canonical` then defers to the
    conservative argv-only scan for the whole argv.
    """
    import argparse

    tokens: list[Token] = []
    current = parser
    path: tuple[str, ...] = ()
    positionals = [action for action in current._actions if not action.option_strings]  # noqa: SLF001
    literal = False
    index = 0
    while index < len(argv):
        raw = argv[index]
        index += 1
        if not literal and raw == "--":
            literal = True
            tokens.append(Token(raw, "separator", path=path))
            continue
        kind, action, name, inline = ("positional", None, None, None) if literal \
            else _classify(current, raw)
        if kind == "unknown":
            tokens.append(Token(raw, "unknown", option=name, inline=inline, path=path))
            continue
        if kind == "option":
            option = _canonical_option(action)
            tokens.append(Token(raw, "option", option=option, dest=action.dest,
                                inline=inline, path=path, spelling=name))
            if inline is not None:
                continue
            mode = _takes(action)
            if mode == "none":
                continue
            while index < len(argv):
                following = argv[index]
                kind_following = _classify(current, following)[0]
                if mode != "one" and kind_following != "positional":
                    break
                if kind_following == "unknown":
                    # argparse takes only an argument-like token as a value
                    # (`--from -5,10` on 3.11): leave it unknown so the whole
                    # argv defers to the conservative scan, as argparse rejects it.
                    break
                tokens.append(Token(following, "value", option=option,
                                    dest=action.dest, path=path, spelling=name))
                index += 1
                if mode in ("one", "optional"):
                    break
            continue
        # A positional: the next subcommand choice, or a positional argument.
        action = positionals[0] if positionals else None
        if isinstance(action, argparse._SubParsersAction) and raw in action.choices:  # noqa: SLF001
            path = path + (raw,)
            current = action.choices[raw]
            positionals = [a for a in current._actions if not a.option_strings]  # noqa: SLF001
            # argparse drops a `--` before the subcommand; the subparser then
            # parses its own arguments normally (`-- ui type --sensitive X`).
            literal = False
            tokens.append(Token(raw, "command", path=path))
            continue
        if action is None or isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            # No positional slot takes this token here: argparse would reject it.
            tokens.append(Token(raw, "stray", path=path))
            continue
        tokens.append(Token(raw, "positional", dest=action.dest, path=path))
        if action.nargs in (None, 1, "?"):
            positionals = positionals[1:]
    return tokens


def scrub_canonical(parser: Any, argv: list[str], args: Any = None) -> list[str]:
    """Scrub argv resolved against the real parser.

    Parser mode is a strict superset of the argv-only scan (and so of the
    original exact-flag rule): it may mask more, never less.

    - A value is secret when its canonical option or its dest is secret, OR
      when the option as typed would be secret to the argv-only scan
      (`--body` resolves to `--body-file` under `network mock add`, but the
      author wrote `--body`).
    - A secret-looking spelling of a flag that takes no value still masks the
      next token, as the argv-only scan does.
    - Every other value, inline value and positional gets content scrubbing.
    - The `ui type` text, and given the namespace every exact secret value,
      is masked wherever it appears.
    - Finally every token the argv-only scan alters but this pass left intact
      takes the argv-only result, so the superset holds by construction.

    Option spellings are written back in their canonical full form.
    """
    tokens = canonical_argv(parser, argv)
    fallback = scrub_argv(argv, args)
    if any(token.role in ("unknown", "stray") for token in tokens):
        # argparse would not have accepted this argv as it stands (so it is
        # normally never journaled). Nothing here is resolved with certainty,
        # so the conservative argv-only scan decides instead.
        return fallback
    resolved_path = tokens[-1].path if tokens else ()
    secrets = _namespace_secrets(args)
    typed_text = None
    if resolved_path[:2] == ("ui", "type") and args is not None:
        text = getattr(args, "text", None)
        typed_text = text if isinstance(text, str) and text else None

    def secret_option(token: Token) -> bool:
        spelling = token.spelling or ""
        return (token.option in _SENSITIVE_VALUE_FLAGS or token.dest in _SENSITIVE_DESTS
                or spelling in _SENSITIVE_VALUE_FLAGS
                or _sensitive_flag(spelling, token.path))

    out: list[str] = []
    intact: list[bool] = []   # True when this pass left the token's content as typed
    mask_next = False
    for position, token in enumerate(tokens):
        if mask_next:
            mask_next = False
            out.append("***")
            intact.append(False)
            continue
        if token.role == "option":
            if token.inline is not None and (secret_option(token) or token.inline in secrets):
                out.append(f"{token.option}=***")
                intact.append(False)
            elif token.inline is not None:
                scrubbed = _scrub_free(token.inline)
                out.append(f"{token.option}={scrubbed}")
                intact.append(scrubbed == token.inline)
            else:
                out.append(token.text())
                intact.append(True)
                # `--raw` / `--val` style spellings mask the next token even
                # when this flag takes no value, exactly like the argv scan.
                following = tokens[position + 1] if position + 1 < len(tokens) else None
                mask_next = (secret_option(token) and following is not None
                             and following.role != "value")
            continue
        if token.role == "value":
            if secret_option(token) or token.raw in secrets:
                out.append("***")
                intact.append(False)
            else:
                scrubbed = _scrub_free(token.raw)
                out.append(scrubbed)
                intact.append(scrubbed == token.raw)
            continue
        if token.role == "positional" and token.path[:2] == ("ui", "type"):
            # Whatever was typed into the focused field: only its length survives.
            out.append(_mask(token.raw))
            intact.append(False)
            continue
        if typed_text is not None and token.raw == typed_text:
            out.append(_mask(token.raw))
            intact.append(False)
            continue
        if token.raw in secrets:
            out.append("***")
            intact.append(False)
            continue
        scrubbed = _scrub_free(token.raw) if token.role == "positional" else token.raw
        out.append(scrubbed)
        intact.append(scrubbed == token.raw)
    # Safety net: the argv-only result is 1:1 with argv, as is this one.
    for index, raw in enumerate(argv):
        if intact[index] and fallback[index] != raw:
            out[index] = fallback[index]
    return out


def _scrub_free(raw: str) -> str:
    """Content scrubbing for a value that is not itself secret by name.

    `--env password=X`, `--author password=X` or `--url https://h/?token=X`
    carry a credential inside an otherwise ordinary value, so every non-secret
    value, inline value and positional gets the fallback's body scrubbing
    (plus URL query redaction). Parser mode thereby masks at least everything
    the argv-only scan masks.
    """
    if "://" in raw:
        raw = redact.scrub_url(raw) or raw
    if "=" in raw or "{" in raw:
        # Structural JSON scrubbing, then the regex passes as well, so a form
        # field inside a JSON string is caught in either position.
        raw = redact.scrub_patterns(redact.scrub_body(raw))
    return raw


def _summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in _SUMMARY_KEYS if key in payload}


def _lock(handle: Any) -> None:
    """An exclusive lock for the append, held until the handle closes; a
    no-op where `fcntl` does not exist."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return
    fcntl.flock(handle, fcntl.LOCK_EX)


def append(session: dict[str, Any] | None, entry: dict[str, Any]) -> None:
    """Append one entry. Never raises — journaling is best-effort."""
    if not session:
        return
    try:
        path = journal_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            # Serialize sequence allocation and append across independent CLI
            # processes (a flow and a hand-run verb on one session).
            _lock(handle)
            full = {"seq": _next_seq(path), "ts": _now(), **entry}
            handle.write(json.dumps(full, ensure_ascii=False) + "\n")
            handle.flush()
    except Exception:  # noqa: BLE001 — a broken journal must not break a command
        pass


def record_action(
    session: dict[str, Any] | None,
    *,
    verb: str,
    argv: list[str],
    payload: dict[str, Any] | None,
    ok: bool,
    error_code: str | None = None,
    origin: str = "agent",
    args: Any = None,
    parser: Any = None,
) -> None:
    """Journal one CLI action.

    Pass the root `parser` (and the parsed `args`) whenever they exist: argv
    is then also resolved exactly as argparse resolved it. Whatever is passed,
    the argv written is `scrub_for_journal`: the per-token union of every
    applicable scrub, never less masked than the previous release.
    """
    try:
        scrubbed = scrub_for_journal(argv, args, parser)
    except Exception:  # noqa: BLE001 - never journal an unscrubbed argv
        scrubbed = ["***"] * len(argv)
    entry: dict[str, Any] = {
        "kind": "action",
        "verb": verb,
        "argv": scrubbed,
        "ok": ok,
        "origin": origin,
    }
    summary = _summary(payload)
    if summary:
        entry["result"] = summary
    if error_code:
        entry["error_code"] = error_code
    append(session, entry)


def note(
    session: dict[str, Any],
    text: str,
    *,
    task: str | None = None,
    tags: list[str] | None = None,
    author: str = "agent",
) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": "note", "author": author, "text": text}
    if task:
        entry["task"] = task
    if tags:
        entry["tags"] = tags
    append(session, entry)
    return entry


def read(
    session: dict[str, Any],
    *,
    kind: str | None = None,
    verb: str | None = None,
    task: str | None = None,
    grep: str | None = None,
    max_entries: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (entries, total_matched). Newest entries are kept when truncating."""
    path = journal_path(session)
    if not path.exists():
        return [], 0
    import re

    pattern = re.compile(grep, re.IGNORECASE) if grep else None
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if kind and item.get("kind") != kind:
            continue
        if verb and item.get("verb") != verb:
            continue
        if task and item.get("task") != task:
            continue
        if pattern and not pattern.search(json.dumps(item, ensure_ascii=False)):
            continue
        entries.append(item)
    total = len(entries)
    if max_entries is not None and total > max_entries:
        entries = entries[-max_entries:]
    return entries, total
