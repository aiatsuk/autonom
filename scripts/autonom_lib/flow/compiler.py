"""Session → Flow compiler (research doc §8): a verified journey becomes a
repeatable flow file.

Input is the session's journal plus the per-action detail records the
instrumented handlers write (``autonom_lib/actions.py``). The compiler is
deliberately conservative — §8.4's "never silently" list is the contract:

- an action whose provenance is missing (a coordinate tap, a pre-0.23
  session without detail records) is **skipped with a named warning**, never
  approximated;
- sensitive input (``ui type --sensitive``, or a value typed right after
  focusing a credential-shaped field) becomes ``${SECRET_n}`` — the value is
  not in the artifacts and cannot leak into the flow;
- a selector that was proven unique during the session is reused verbatim;
  an explicit ``--index`` carries over as an explicit ``index`` (it was the
  operator's stated choice, §7.9 item 6);
- the emitted file is parsed and built back before it is written, and the
  quality report explains the risk (§8.5) instead of hiding it.
"""
from __future__ import annotations

import re
from typing import Any

from .. import actions as actions_mod
from .. import errors
from .. import journal as journal_mod
from .canonical import emit_flow
from .parser import parse_document
from .schema import Flow, FlowSelector, Step, build_flow, url_has_scheme
from ..contracts import stable_id

_CREDENTIAL_HINT = re.compile(
    r"pass(word)?|pwd|pin\b|secret|token|otp|cvv|card", re.IGNORECASE)

_NOISE_VERBS = {
    "ui tree", "ui find", "journal", "shots", "devices", "doctor",
    "processes", "cleanup", "session show", "session stop", "session start",
    "network requests", "network status", "record start", "record stop",
    "flow", "crash", "file", "logs",
}


# The value-taking options of the verbs compiled below, as build_parser() in
# scripts/autonom.py declares them: the target flags every leaf verb repeats
# (plus the global-only --axe/--ios-hid), then each verb's own. A journal
# argv is read with these, so `open URL --serial S` yields URL — never S.
_TARGET_VALUE_FLAGS = (
    "--platform", "--target", "--serial", "--udid", "--adb", "--simctl",
    "--idb", "--idb-host", "--idb-port", "--axe", "--ios-hid",
)
_VERB_OPTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # verb: (value-taking options, switches)
    "open": ((), ()),
    "session launch": (("--activity", "--arg", "--setenv"), ("--fresh",)),
    "session force-stop": ((), ()),
    "session clear": (("--strategy",), ()),
    "session uninstall": ((), ()),
    "ui key": ((), ()),
    "location set": ((), ()),
    "permissions": ((), ()),
}
_NEGATIVE_NUMBER_RE = re.compile(r"^-\.?\d")
# `session launch` options Flow v1 launchApp cannot express (schema.py:
# launchApp takes clearState, resume, label, postcondition only)
_LAUNCH_ONLY_OPTIONS = ("--activity", "--arg", "--setenv")


def _resolve_option(token: str, known: tuple[str, ...],
                    value_flags: tuple[str, ...]) -> str | None:
    """The option a `--name` token means, with argparse's prefix rule."""
    if token in known:
        return token
    candidates = [flag for flag in known if flag.startswith(token)]
    if len(candidates) == 1:
        return candidates[0]
    if candidates and all(flag in value_flags for flag in candidates):
        return candidates[0]  # ambiguous, but it takes a value either way
    return None


def _parse_argv(verb: str, argv: list[Any]) -> tuple[list[str], set[str]] | None:
    """(positionals after the verb words, options present) for a journal argv.

    Option values are never positionals: `--serial S` and `--label=x` are
    skipped with their value wherever they sit (before the verb, between
    positionals, at the end). A `--` before the verb path is complete is
    dropped as argparse drops it; a later one makes the rest positional.
    None when the argv does not spell `verb`.
    """
    values, switches = _VERB_OPTIONS.get(verb, ((), ()))
    value_flags = _TARGET_VALUE_FLAGS + values
    known = value_flags + switches
    verb_words = verb.split()
    tokens = [str(token) for token in argv]
    words: list[str] = []
    present: set[str] = set()
    only_positionals = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if only_positionals:
            words.append(token)
        elif token == "--":
            if len(words) >= len(verb_words):
                only_positionals = True
        elif token.startswith("--") and len(token) > 2:
            name, equals, _value = token.partition("=")
            option = _resolve_option(name, known, value_flags)
            if option is not None:
                present.add(option)
                if option in value_flags and not equals:
                    index += 1  # its value
        elif token.startswith("-") and len(token) > 1 \
                and not _NEGATIVE_NUMBER_RE.match(token):
            continue  # a short switch (-h); none of these verbs takes a value
        else:
            words.append(token)
    if words[:len(verb_words)] != verb_words:
        return None
    return words[len(verb_words):], present


def _selector_from_detail(detail: dict[str, Any]) -> FlowSelector | None:
    raw = detail.get("selector") or {}
    fields: dict[str, Any] = {}
    source: dict[str, Any] = {}
    mapping = {"resource_id": ("resource_id", "id"), "text": ("text", "text"),
               "desc": ("desc", "description"), "role": ("role", "role")}
    for cli_name, (engine_key, flow_name) in mapping.items():
        value = raw.get(cli_name)
        if value is not None:
            fields[engine_key] = value
            source[flow_name] = value
    for bool_name in ("enabled", "checked", "selected", "focused", "clickable"):
        value = raw.get(bool_name)
        if value is not None and bool_name != "clickable":
            fields[bool_name] = bool(value)
            source[bool_name] = bool(value)
    if not (set(fields) & {"resource_id", "text", "desc", "role"}):
        return None
    mode = raw.get("mode", "contains")
    case_sensitive = bool(raw.get("case_sensitive", False))
    if mode == "exact":
        match = "exact" if case_sensitive else "caseInsensitiveExact"
    elif mode in ("contains", "regex"):
        match = mode  # flow contains/regex are case-sensitive; close enough
    else:
        return None
    selector = FlowSelector(fields=fields, match=match,
                            source_fields=source)
    if raw.get("index") is not None:
        selector.index = int(raw["index"])
    return selector


def _selector_from_node(node: dict[str, Any],
                        nodes: list[dict[str, Any]]) -> FlowSelector | None:
    """§7.9 priority over the recorded tree: id, unique text, unique desc."""
    def unique(key: str, value: str) -> bool:
        return sum(1 for other in nodes if other.get(key) == value) == 1

    resource_id = node.get("resource_id")
    if resource_id and unique("resource_id", resource_id):
        return FlowSelector(fields={"resource_id": resource_id},
                            source_fields={"id": resource_id})
    text = node.get("text")
    if text and unique("text", text):
        return FlowSelector(fields={"text": text}, source_fields={"text": text})
    desc = node.get("desc")
    if desc and unique("desc", desc):
        return FlowSelector(fields={"desc": desc},
                            source_fields={"description": desc})
    return None


def _looks_credential(previous_tap_node: dict[str, Any] | None) -> bool:
    if not previous_tap_node:
        return False
    for key in ("resource_id", "text", "desc"):
        value = previous_tap_node.get(key)
        if value and _CREDENTIAL_HINT.search(str(value)):
            return True
    return False


def compile_session(session: dict[str, Any], *, name: str | None = None,
                    task: str | None = None,
                    start_seq: int | None = None,
                    end_seq: int | None = None) -> tuple[Flow, dict[str, Any]]:
    """Journal + details -> (validated Flow, quality report)."""
    entries, _total = journal_mod.read(session, max_entries=10_000)
    if start_seq is not None:
        entries = [entry for entry in entries if int(entry.get("seq", 0)) >= start_seq]
    if end_seq is not None:
        entries = [entry for entry in entries if int(entry.get("seq", 0)) <= end_seq]
    details = actions_mod.read_details(session)

    flow = Flow(path="<generated>", name=name or f"Recorded {task or 'session'}",
                app_id=session.get("app_id"))
    flow.flow_id = stable_id(
        "flow", flow.app_id or "unknown", task or flow.name,
        start_seq or "start", end_seq or "end")
    if task:
        flow.tags = [task]
    warnings: list[dict[str, Any]] = []
    quality = {"selectors": {"id": 0, "text": 0, "description": 0,
                             "recorded": 0, "index": 0},
               "secrets": 0, "skipped": 0, "steps": 0}
    secret_count = 0
    last_tap_node: dict[str, Any] | None = None

    def warn(code: str, message: str, **extra: Any) -> None:
        warnings.append({"code": code, "error": message, **extra})
        quality["skipped"] += 1

    for entry in entries:
        if entry.get("kind") == "note":
            flow.steps.append(Step("note", {"text": entry.get("text", "")}))
            continue
        if entry.get("kind") != "action" or not entry.get("ok", False):
            continue
        verb = entry.get("verb", "")
        argv = entry.get("argv") or []
        result = entry.get("result") or {}
        detail = details.get(result.get("detail", ""))

        parsed = _parse_argv(verb, argv) if verb in _VERB_OPTIONS else None
        positionals, options = parsed if parsed is not None else ([], set())
        # The platform the action ran on: the journal entry's own result
        # (every target verb records `platform` there), else the session's.
        platform = result.get("platform") or session.get("platform")

        if verb == "session launch":
            # `session launch` resumes the app where it was unless --fresh;
            # a bare flow `launchApp` is the fresh launch
            flow.steps.append(Step("launchApp", {} if "--fresh" in options
                                   else {"resume": True}))
            flow.app_id = flow.app_id or (positionals[0] if positionals else None)
            dropped = sorted(options & set(_LAUNCH_ONLY_OPTIONS))
            if dropped:
                # launchApp carries no activity, arguments or environment;
                # the launch compiles, these options do not (values stay out)
                warn("launch_args_not_compilable",
                     f"launchApp has no equivalent for {', '.join(dropped)}; "
                     "the launch was compiled without them",
                     seq=entry.get("seq"), options=dropped)
        elif verb == "session clear":
            if platform == "ios":
                # clearState is Android-only (pm clear): emitting it would
                # make the recorded flow unrunnable on the platform it came from
                warn("clear_state_not_compilable_on_ios",
                     "clearState is Android-only; on iOS declare "
                     "'setup: {reset: true}' in the flow header (reinstalls "
                     "from the recorded install path) or reset outside the flow",
                     seq=entry.get("seq"), platform=platform)
            else:
                flow.steps.append(Step("clearState", {}))
        elif verb == "session force-stop":
            flow.steps.append(Step("stopApp", {}))
        elif verb == "session uninstall":
            warn("uninstall_not_compilable",
                 "a flow cannot uninstall the app — replay needs it installed; "
                 "reinstall outside the flow (session start --install) or use "
                 "setup.reset (or clearState on Android) for a clean state",
                 seq=entry.get("seq"))
        elif verb == "open":
            url = positionals[0] if positionals else None
            if not url:
                warn("open_url_not_recoverable",
                     "the journal argv of this 'open' carries no URL",
                     seq=entry.get("seq"))
            elif url_has_scheme(url) is False:
                warn("open_url_not_compilable",
                     f"{url!r} has no scheme; openLink needs an absolute URL",
                     seq=entry.get("seq"))
            else:
                flow.steps.append(Step("openLink", {"url": url}))
        elif verb == "ui tap":
            if not detail or detail.get("coordinate"):
                warn("coordinate_tap_not_compilable",
                     "a coordinate tap has no selector to compile; re-record "
                     "it with a semantic selector",
                     seq=entry.get("seq"))
                last_tap_node = None
                continue
            selector = _selector_from_detail(detail)
            if selector is None and detail.get("node"):
                selector = _selector_from_node(detail["node"],
                                               detail.get("nodes") or [])
                if selector is not None:
                    quality["selectors"]["recorded"] += 1
            elif selector is not None:
                quality["selectors"]["recorded"] += 1
            if selector is None:
                warn("selector_not_recoverable",
                     "no stable selector could be derived for this tap",
                     seq=entry.get("seq"))
                last_tap_node = None
                continue
            if "resource_id" in selector.fields:
                quality["selectors"]["id"] += 1
            elif "text" in selector.fields:
                quality["selectors"]["text"] += 1
            elif "desc" in selector.fields:
                quality["selectors"]["description"] += 1
            if selector.index is not None:
                quality["selectors"]["index"] += 1
            args: dict[str, Any] = {"selector": selector}
            if detail.get("duration_ms"):
                flow.steps.append(Step("longPressOn",
                                       {**args,
                                        "durationMs": detail["duration_ms"]}))
            else:
                flow.steps.append(Step("tapOn", args))
            last_tap_node = detail.get("node")
        elif verb == "ui type":
            if not detail:
                warn("typed_text_not_recorded",
                     "this session predates typed-text detail records",
                     seq=entry.get("seq"))
                continue
            sensitive = bool(detail.get("sensitive")) or _looks_credential(
                last_tap_node)
            if sensitive or detail.get("text") is None:
                secret_count += 1
                variable = f"SECRET_{secret_count}"
                flow.steps.append(Step("inputText",
                                       {"value": f"${{{variable}}}",
                                        "sensitive": True}))
                quality["secrets"] += 1
            else:
                flow.steps.append(Step("inputText", {"value": detail["text"]}))
        elif verb == "ui key":
            key = positionals[0] if positionals else None
            if key == "KEYCODE_BACK":
                flow.steps.append(Step("back", {}))
            elif key:
                flow.steps.append(Step("pressKey", {"key": key}))
        elif verb == "screenshot":
            label = None
            if "--label" in argv:
                position = argv.index("--label")
                if position + 1 < len(argv):
                    label = argv[position + 1]
            flow.steps.append(Step("takeScreenshot",
                                   {"label": label} if label else {}))
        elif verb == "location set":
            coordinates = positionals[0] if positionals else ""
            if "," in coordinates:
                latitude, longitude = coordinates.split(",", 1)
                try:
                    flow.steps.append(Step("setLocation", {
                        "latitude": float(latitude),
                        "longitude": float(longitude)}))
                except ValueError:
                    pass
        elif verb == "permissions":
            if len(positionals) >= 2:
                args = {"action": positionals[0], "service": positionals[1]}
                if len(positionals) >= 3:
                    args["appId"] = positionals[2]
                flow.steps.append(Step("setPermissions", args))
        elif verb == "ui swipe":
            warn("swipe_not_compilable",
                 "point-to-point swipes do not compile; use directional "
                 "swipes in the flow by hand", seq=entry.get("seq"))
        elif any(verb == noise or verb.startswith(noise + " ")
                 or verb.split(" ")[0] == noise for noise in _NOISE_VERBS):
            continue

    # a find that proved something visible right before the end becomes the
    # closing assertion — the cheapest §8.3 "infer assertions" heuristic
    closing = None
    for path, detail in reversed(list(details.items())):
        if detail.get("kind") == "find" and detail.get("count", 0) >= 1:
            closing = _selector_from_detail(detail)
            break
    if closing is not None:
        flow.steps.append(Step("assertVisible", {"selector": closing}))
        # the closing assertion is a selector the flow depends on like any
        # tap — it was proven by a 'ui find', so it counts as recorded
        quality["selectors"]["recorded"] += 1
        if "resource_id" in closing.fields:
            quality["selectors"]["id"] += 1
        elif "text" in closing.fields:
            quality["selectors"]["text"] += 1
        elif "desc" in closing.fields:
            quality["selectors"]["description"] += 1
        if closing.index is not None:
            quality["selectors"]["index"] += 1
    else:
        warnings.append({
            "code": "no_final_assertion",
            "error": "the session ends without a verifying 'ui find'; the "
                     "flow has no closing assertion",
            "hint": "End recordings with 'autonom ui find <selector>' on the "
                    "success state.",
        })

    if not flow.steps or all(step.command == "note" for step in flow.steps):
        raise errors.AutonomError(
            errors.FLOW_CHECK_FAILED,
            "the session contains no compilable actions",
            hint="Drive the app with ui tap/type/find and re-record.",
            warnings=warnings,
        )
    quality["steps"] = len(flow.steps)

    env_hint = {f"SECRET_{i}": "" for i in range(1, secret_count + 1)}
    total_selectors = sum(quality["selectors"].values())
    stable_selectors = (quality["selectors"]["id"]
                        + quality["selectors"]["recorded"])
    # Nothing selected means nothing was proven: a flow with no selector at
    # all (no tap, no closing assertion) is the least trustworthy output,
    # never a perfect 1.0 that skips review.
    confidence = 0.0 if total_selectors == 0 else min(
        1.0, stable_selectors / total_selectors)
    report = {"warnings": warnings, "quality": quality,
              "secrets_required": list(env_hint),
              "range": {"start_seq": start_seq, "end_seq": end_seq},
              "provenance": {
                  "session_id": session.get("session_id"),
                  "journal": str(journal_mod.journal_path(session)),
                  "compiler": "autonom.teach/v1",
              },
              "confidence": round(confidence, 3),
              "review_required": bool(warnings or confidence < 1.0
                                      or total_selectors == 0)}
    return flow, report


def compile_to_text(session: dict[str, Any], *, name: str | None = None,
                    task: str | None = None,
                    start_seq: int | None = None,
                    end_seq: int | None = None) -> tuple[str, dict[str, Any]]:
    flow, report = compile_session(session, name=name, task=task,
                                   start_seq=start_seq, end_seq=end_seq)
    text = emit_flow(flow)
    build_flow(parse_document(text, "<generated>"))  # never emit the unparseable
    return text, report
