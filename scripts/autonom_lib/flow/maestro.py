"""Maestro Core Profile import/export (research doc §15).

Autonom does not promise full Maestro compatibility — it supports a
documented **Core Profile** and refuses everything outside it loudly, with
the file position and a hint. An ambiguous conversion never produces a file
that silently means something else.

Semantics preserved on import:

- Maestro treats ``text``/``id`` as full-match **regex**. A pattern with no
  regex metacharacters imports as ``match: exact`` (identical semantics);
  anything else imports as ``match: regex`` wrapped in ``^(?:...)$`` because
  Autonom's regex mode is a *search*, not a full match.
- ``extendedWaitUntil`` becomes ``waitUntil`` (an optional one only right
  before an optional tap on the same element, folded into that tap's
  ``timeoutMs``); ``waitForAnimationToEnd`` becomes ``waitForSettled``;
  ``takeScreenshot``'s path becomes a label; ``launchApp.clearState``
  carries over.
- JavaScript interpolation (``${output.x}``) has no Autonom equivalent and
  is refused, not approximated.

On export, ``match: exact`` text is regex-escaped so Maestro's regex
matching stays exact; a tap's ``timeoutMs`` becomes an ``extendedWaitUntil``
on the same element before the tap; Autonom-only commands refuse (evidence
commands ``checkpoint``/``note`` become comments instead — they carry no
behavior).
Export collects **every** refusal (each with its line) before failing, so
one pass shows the whole gap, and every scalar is YAML-quoted when plain
YAML would read it as something else.

Round trips are stable: an escape-only pattern (``Sign\\ in``,
``Save \\(draft\\)``) imports as the exact text it spells, and export
strips the importer's own ``^(?:...)$`` wrapper instead of wrapping it again,
so import → export → import yields the same selector every time.
"""
from __future__ import annotations

import re

from .. import errors
from . import FLOW_SCHEMA_ID
from .canonical import emit_flow
from .parser import FlowDocument, Mapping, Scalar, Sequence, parse_document
from .schema import Flow, FlowSelector, REGISTRY, Step, build_flow

_PLAIN_TEXT_RE = re.compile(r"^[^.^$*+?()\[\]{}|\\]*$")
_JS_INTERP_RE = re.compile(r"\$\{[^}]*[^A-Za-z0-9_}][^}]*\}")
_SCHEMA_LINE_RE = re.compile(r"^schema\s*:")

_CORE_HEADER = ("appId", "name", "tags", "env", "properties",
                "onFlowStart", "onFlowComplete", "url")
_TAP_COMMANDS = ("tapOn", "longPressOn", "doubleTapOn")
_IMPORTED_OPTIONAL_REASON = "optional in the Maestro source"


def is_maestro_document(text: str) -> bool:
    """True when the header before ``---`` carries no ``schema:`` field.

    Flow v1 requires ``schema: autonom.dev/flow/v1`` in the header; a Maestro
    file never has one. A file with no ``---`` at all is left to the strict
    parser, whose missing-separator error fits both formats.
    """
    for line in text.split("\n"):
        if line.strip() == "---":
            return True
        if _SCHEMA_LINE_RE.match(line):
            return False
    return False

_UNSUPPORTED_HINTS = {
    "runScript": "Replace runScript with a deterministic subflow or execute it outside Flow v1",
    "evalScript": "Flow v1 has no script engine, by design",
    "inputRandomText": "flows are deterministic; pass the value via env or --secret",
    "inputRandomNumber": "flows are deterministic; pass the value via env or --secret",
    "inputRandomEmail": "flows are deterministic; pass the value via env or --secret",
    "inputRandomPersonName": "flows are deterministic; pass the value via env or --secret",
    "hideKeyboard": "no reliable cross-platform substrate; press KEYCODE_BACK on Android",
    "travel": "location simulation imports only as setLocation",
    "startRecording": "recording is session-level: autonom record start",
    "stopRecording": "recording is session-level: autonom record stop",
    "setAirplaneMode": "no substrate in the Core Profile",
    "toggleAirplaneMode": "no substrate in the Core Profile",
    "assertTrue": "JavaScript assertions are not part of Flow v1",
    "evalCondition": "JavaScript conditions are not part of Flow v1",
}


def _refuse(path: str, line: int, col: int, command: str, hint: str) -> None:
    raise errors.AutonomError(
        errors.UNSUPPORTED_FLOW_COMMAND,
        f"{path}:{line}:{col}: {command!r} is outside the Maestro Core Profile",
        hint=hint, file=path, line=line, column=col, command=command,
    )


def _no_js(text: str, path: str, line: int, col: int) -> str:
    if _JS_INTERP_RE.search(text):
        raise errors.AutonomError(
            errors.UNSUPPORTED_FLOW_COMMAND,
            f"{path}:{line}:{col}: JavaScript interpolation has no Autonom "
            f"equivalent: {text!r}",
            hint="Only ${NAME} environment interpolation carries over.",
            file=path, line=line, column=col,
        )
    return text


# A pattern made only of literal characters and backslash-escaped
# punctuation (`Sign\ in`, `Price: \$5`) is a literal in disguise — our own
# export writes exact text that way. `\d`, `\w`, `\1` are real regex.
_ESCAPED_LITERAL_RE = re.compile(r"^(?:[^.^$*+?()\[\]{}|\\]|\\[^A-Za-z0-9])*$")
_ESCAPE_RE = re.compile(r"\\(.)", re.S)


def _literal_of(pattern: str) -> str | None:
    """The plain text a regex spells when it is literal-only, else None."""
    if _PLAIN_TEXT_RE.match(pattern):
        return pattern
    if _ESCAPED_LITERAL_RE.match(pattern):
        return _ESCAPE_RE.sub(r"\1", pattern)
    return None


def _anchored(pattern: str) -> str:
    """Maestro full match -> our search regex (a leading (?i) stays first)."""
    if pattern.startswith("(?i)"):
        return f"(?i)^(?:{pattern[4:]})$"
    return f"^(?:{pattern})$"


def _import_pattern(pattern: str) -> tuple[str, str]:
    """Maestro full-match regex -> (autonom text, match mode)."""
    literal = _literal_of(pattern)
    if literal is not None:
        return literal, "exact"
    # the shapes our own export writes for the other literal modes
    if pattern.startswith("(?i)"):
        literal = _literal_of(pattern[4:])
        if literal is not None:
            return literal, "caseInsensitiveExact"
    if pattern.startswith(".*") and pattern.endswith(".*") and len(pattern) > 4:
        literal = _literal_of(pattern[2:-2])
        if literal:
            return literal, "contains"
    return _anchored(pattern), "regex"


def _selector_from(node, path: str) -> FlowSelector:
    """A Maestro selector: a bare scalar or fields directly on the command."""
    selector, extras = _selector_with_extras(node, path)
    if extras:
        name = next(iter(extras))
        _refuse(path, node.line, node.col, f"selector field {name}",
                "label/optional belong on the command, not inside a "
                "condition selector.")
    return selector


def _selector_with_extras(node, path: str) -> tuple[FlowSelector, dict]:
    """Split a Maestro selector map into selector fields and command extras.

    Maestro puts ``label``/``optional`` on the same map as the selector
    fields; Autonom keeps them as command arguments.
    """
    if isinstance(node, Scalar):
        text, mode = _import_pattern(_no_js(node.text, path, node.line, node.col))
        return FlowSelector(fields={"visible_text": text}, match=mode,
                            source_fields={"visibleText": text},
                            line=node.line, col=node.col), {}
    _require_mapping(node, path, "selector")
    selector = FlowSelector(line=node.line, col=node.col)
    extras: dict = {}
    modes = set()
    raw_patterns: dict = {}  # engine field -> (source field, Maestro pattern)
    for key, value in node.pairs:
        name = key.text
        if name == "label":
            extras["label"] = _scalar_text(value, path)
        elif name == "optional":
            extras["optional"] = _bool_text(value, path, "optional")
        elif name == "repeat":
            extras["repeat"] = _int_text(value, path, "tap repeat")
        elif name == "delay":
            extras["delayMs"] = _int_text(value, path, "tap delay")
        elif name in ("text", "id"):
            raw = _scalar_text(value, path)
            pattern, mode = _import_pattern(raw)
            # Maestro's `text` matches the union of text / hintText /
            # accessibilityText (Filters.kt) — that is our `visibleText`,
            # not the strict `text` attribute. Importing it as `text` would
            # silently fail to match every Flutter/iOS label.
            field = "visible_text" if name == "text" else "resource_id"
            source = "visibleText" if name == "text" else "id"
            selector.fields[field] = pattern
            selector.source_fields[source] = pattern
            raw_patterns[field] = (source, raw)
            modes.add(mode)
        elif name == "index":
            selector.index = _int_text(value, path, "selector index")
        elif name == "enabled":
            flag = _bool_text(value, path, "selector field enabled")
            selector.fields["enabled"] = flag
            selector.source_fields["enabled"] = flag
        else:
            _refuse(path, key.line, key.col, f"selector field {name}",
                    "Core Profile selectors: text, id, index, enabled.")
    if len(modes) == 1:
        selector.match = modes.pop()
    elif modes:
        # One selector carries one match mode. Mixed fields (an exact id
        # beside a regex text) all fall back to anchored regex so the exact
        # one keeps its full-match meaning instead of becoming a search.
        selector.match = "regex"
        for field, (source, raw) in raw_patterns.items():
            selector.fields[field] = _anchored(raw)
            selector.source_fields[source] = _anchored(raw)
    else:
        selector.match = "exact"
    if not selector.fields:
        _refuse(path, node.line, node.col, "empty selector",
                "Give the element a text or id.")
    if not ({"visible_text", "text", "resource_id"} & set(selector.fields)):
        _refuse(path, node.line, node.col, "selector without text or id",
                "State fields alone cannot identify an element; give it a "
                "text or id.")
    return selector, extras


def _apply_extras(command: str, args: dict, extras: dict, path: str,
                  line: int, col: int) -> dict:
    """Fold Maestro label/optional/repeat/delay into command arguments."""
    if "label" in extras:
        args["label"] = extras["label"]
    if extras.get("optional"):
        if command not in _TAP_COMMANDS:
            _refuse(path, line, col, f"optional on {command}",
                    "Autonom allows optional only on tap commands, and an "
                    "optional assertion is refused by design.")
        args["optional"] = True
        args["reason"] = _IMPORTED_OPTIONAL_REASON
    for name in ("repeat", "delayMs"):
        if name in extras:
            if command != "tapOn":
                _refuse(path, line, col, f"{name} on {command}",
                        "Repeated taps import on tapOn only.")
            args[name] = extras[name]
    return args


def _scalar_text(node, path: str) -> str:
    if not isinstance(node, Scalar):
        raise errors.AutonomError(
            errors.UNSUPPORTED_FLOW_COMMAND,
            f"{path}:{getattr(node, 'line', 0)}: nested structures here are "
            "outside the Core Profile",
            file=path, line=getattr(node, "line", 0),
        )
    return _no_js(node.text, path, node.line, node.col)


def _int_text(node, path: str, what: str) -> int:
    raw = _scalar_text(node, path)
    try:
        return int(raw)
    except ValueError:
        _refuse(path, getattr(node, "line", 0), getattr(node, "col", 0), what,
                f"expected an integer, got {raw!r}")
    raise AssertionError("unreachable")  # _refuse always raises


# Maestro's YAML layer (snakeyaml, YAML 1.1) accepts these boolean spellings;
# importing `True`/`yes`/`on` as anything but a boolean would silently flip
# semantics (an optional step becoming required, enabled becoming false).
_TRUE_WORDS = ("true", "yes", "on")
_FALSE_WORDS = ("false", "no", "off")


def _bool_text(node, path: str, what: str) -> bool:
    raw = _scalar_text(node, path)
    lowered = raw.lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    _refuse(path, getattr(node, "line", 0), getattr(node, "col", 0), what,
            f"expected a boolean, got {raw!r}")
    raise AssertionError("unreachable")


def _require_mapping(node, path: str, what: str) -> Mapping:
    if not isinstance(node, Mapping):
        _refuse(path, getattr(node, "line", 0), getattr(node, "col", 0), what,
                f"{what} takes a mapping of key: value pairs.")
    return node


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_into(target: dict, node, path: str) -> None:
    for env_key, env_value in _require_mapping(node, path, "env").pairs:
        if not _ENV_NAME_RE.match(env_key.text):
            _refuse(path, env_key.line, env_key.col,
                    f"env name {env_key.text}",
                    "Env names match [A-Za-z_][A-Za-z0-9_]*.")
        target[env_key.text] = _scalar_text(env_value, path)


def _steps_from(sequence: Sequence, path: str) -> list[Step]:
    return _fold_optional_waits(
        [_step_from(item, path) for item in sequence.items], path)


# what the importer keeps from `extendedWaitUntil {visible, timeout, optional}`
_FOLDABLE_WAIT_ARGS = frozenset({"visible", "timeoutMs", "optional"})


def _same_selector(left: FlowSelector, right: FlowSelector) -> bool:
    return (left.fields == right.fields and left.match == right.match
            and left.index == right.index and left.relations == right.relations)


def _fold_optional_waits(steps: list[Step], path: str) -> list[Step]:
    """``extendedWaitUntil {visible: X, optional}`` + optional tap on X ->
    one optional tap with ``timeoutMs``.

    That pair is what ``flow export`` writes for an optional tap with a
    timeout, and only that exact shape folds: a wait carrying anything
    besides ``visible``, ``timeout`` and ``optional`` (a ``notVisible`` arm,
    a label) would lose it in the fold. Autonom waits are assertions and
    cannot be optional, so every other optional wait refuses instead of
    silently becoming a required one — or vanishing.
    """
    out: list[Step] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if step.command != "waitUntil" or not step.args.get("optional"):
            out.append(step)
            index += 1
            continue
        tap = steps[index + 1] if index + 1 < len(steps) else None
        if (tap is None or "visible" not in step.args
                or set(step.args) - _FOLDABLE_WAIT_ARGS
                or tap.command not in _TAP_COMMANDS
                or not tap.args.get("optional") or "timeoutMs" in tap.args
                or not _same_selector(step.args["visible"],
                                      tap.args["selector"])):
            _refuse(path, step.line, step.col, "optional extendedWaitUntil",
                    "Autonom waits cannot be optional. An optional wait with "
                    "only visible and timeout imports right before an "
                    "optional tap on the same element, where it becomes that "
                    "tap's timeoutMs.")
        tap.args["timeoutMs"] = step.args["timeoutMs"]
        out.append(tap)
        index += 2
    return out


def _step_from(item, path: str) -> Step:
    if isinstance(item, Scalar):
        name, line, col = item.text, item.line, item.col
        if name in ("launchApp", "stopApp", "clearState", "back",
                    "takeScreenshot", "eraseText", "scroll", "pasteText"):
            return Step(name, {}, line, col)
        if name == "waitForAnimationToEnd":
            # Maestro waits for a static screen and never fails the flow;
            # waitForSettled is the same contract over the UI tree
            return Step("waitForSettled", {}, line, col)
        if name == "hideKeyboard" or name in _UNSUPPORTED_HINTS:
            _refuse(path, line, col, name,
                    _UNSUPPORTED_HINTS.get(name, "outside the Core Profile"))
        _refuse(path, line, col, name, "not a Core Profile command")
    if isinstance(item, Sequence) or len(item.pairs) != 1:
        raise errors.AutonomError(
            errors.UNSUPPORTED_FLOW_COMMAND,
            f"{path}:{item.line}: expected one command per '-' item",
            file=path, line=item.line,
        )
    key, value = item.pairs[0]
    name, line, col = key.text, key.line, key.col

    if name in _UNSUPPORTED_HINTS:
        _refuse(path, line, col, name, _UNSUPPORTED_HINTS[name])

    if name in (*_TAP_COMMANDS, "assertVisible", "assertNotVisible",
                "copyTextFrom"):
        selector, extras = _selector_with_extras(value, path)
        args = _apply_extras(name, {"selector": selector}, extras,
                             path, line, col)
        return Step(name, args, line, col)
    if name == "setClipboard":
        if isinstance(value, Mapping):
            args = {}
            for arg_key, arg_value in value.pairs:
                if arg_key.text == "text":
                    args["value"] = _scalar_text(arg_value, path)
                elif arg_key.text == "label":
                    args["label"] = _scalar_text(arg_value, path)
                else:
                    _refuse(path, arg_key.line, arg_key.col,
                            f"setClipboard.{arg_key.text}",
                            "Core Profile setClipboard supports text and "
                            "label.")
            if "value" not in args:
                _refuse(path, line, col, "setClipboard without text",
                        "Give setClipboard a text value.")
            return Step("setClipboard", args, line, col)
        return Step("setClipboard", {"value": _scalar_text(value, path)},
                    line, col)
    if name == "pasteText":
        args = {}
        for arg_key, arg_value in _require_mapping(value, path,
                                                   "pasteText").pairs:
            if arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"pasteText.{arg_key.text}",
                        "Core Profile pasteText supports label only.")
        return Step("pasteText", args, line, col)
    if name == "repeat":
        args = {}
        for arg_key, arg_value in _require_mapping(value, path, "repeat").pairs:
            if arg_key.text == "times":
                args["times"] = _int_text(arg_value, path, "repeat.times")
            elif arg_key.text == "while":
                args["while"] = _while_from(arg_value, path)
            elif arg_key.text == "commands":
                if not isinstance(arg_value, Sequence):
                    _refuse(path, arg_key.line, arg_key.col, "repeat.commands",
                            "repeat.commands must be a list of commands.")
                args["commands"] = _steps_from(arg_value, path)
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"repeat.{arg_key.text}",
                        "Core Profile repeat supports times, while, commands, "
                        "and label.")
        if "times" not in args:
            _refuse(path, line, col, "repeat without times",
                    "An unbounded repeat does not import; give it a finite "
                    "times (Autonom caps it at 25).")
        if args["times"] > 25:
            _refuse(path, line, col, f"repeat.times: {args['times']}",
                    "Autonom bounds repeat at 25 iterations.")
        if not args.get("commands"):
            _refuse(path, line, col, "repeat without commands",
                    "Give repeat a commands list.")
        return Step("repeat", args, line, col)
    if name == "inputText":
        if isinstance(value, Mapping):
            args = {}
            for arg_key, arg_value in value.pairs:
                if arg_key.text == "text":
                    args["value"] = _scalar_text(arg_value, path)
                elif arg_key.text == "label":
                    args["label"] = _scalar_text(arg_value, path)
                else:
                    _refuse(path, arg_key.line, arg_key.col,
                            f"inputText.{arg_key.text}",
                            "Core Profile inputText supports text and label.")
            if "value" not in args:
                _refuse(path, line, col, "inputText without text",
                        "Give inputText a text value.")
            return Step("inputText", args, line, col)
        return Step("inputText", {"value": _scalar_text(value, path)}, line, col)
    if name == "openLink":
        if isinstance(value, Mapping):
            args = {}
            for arg_key, arg_value in value.pairs:
                if arg_key.text == "link":
                    args["url"] = _scalar_text(arg_value, path)
                elif arg_key.text == "label":
                    args["label"] = _scalar_text(arg_value, path)
                else:
                    _refuse(path, arg_key.line, arg_key.col,
                            f"openLink.{arg_key.text}",
                            "Core Profile openLink supports the link only; "
                            "browser/autoVerify do not import.")
            if "url" not in args:
                _refuse(path, line, col, "openLink without a link",
                        "Give openLink a link.")
            return Step("openLink", args, line, col)
        return Step("openLink", {"url": _scalar_text(value, path)}, line, col)
    if name == "takeScreenshot":
        if isinstance(value, Mapping):
            args = {}
            for arg_key, arg_value in value.pairs:
                if arg_key.text in ("path", "label"):
                    if "label" in args:
                        _refuse(path, arg_key.line, arg_key.col,
                                "takeScreenshot with both path and label",
                                "Autonom screenshots are evidence-dir owned; "
                                "the path becomes the label — give one name.")
                    args["label"] = _scalar_text(arg_value, path)
                else:
                    _refuse(path, arg_key.line, arg_key.col,
                            f"takeScreenshot.{arg_key.text}",
                            "Core Profile takeScreenshot maps the path to a "
                            "label; cropOn does not import.")
            return Step("takeScreenshot", args, line, col)
        return Step("takeScreenshot", {"label": _scalar_text(value, path)},
                    line, col)
    if name == "pressKey":
        return Step("pressKey", {"key": _scalar_text(value, path)}, line, col)
    if name == "eraseText":
        if isinstance(value, Mapping):
            args = {}
            for arg_key, arg_value in value.pairs:
                if arg_key.text == "charactersToErase":
                    args["chars"] = _int_text(arg_value, path,
                                              "eraseText.charactersToErase")
                elif arg_key.text == "label":
                    args["label"] = _scalar_text(arg_value, path)
                else:
                    _refuse(path, arg_key.line, arg_key.col,
                            f"eraseText.{arg_key.text}",
                            "Core Profile eraseText supports "
                            "charactersToErase and label.")
            return Step("eraseText", args, line, col)
        return Step("eraseText", {"chars": _int_text(value, path, "eraseText")},
                    line, col)
    if name == "scrollUntilVisible":
        if isinstance(value, Scalar):
            _refuse(path, line, col, "scrollUntilVisible shorthand",
                    "Use the map form with an element selector.")
        args = {}
        for arg_key, arg_value in _require_mapping(value, path,
                                                   "scrollUntilVisible").pairs:
            if arg_key.text == "element":
                args["selector"] = _selector_from(arg_value, path)
            elif arg_key.text == "direction":
                args["direction"] = _scalar_text(arg_value, path).lower()
            elif arg_key.text == "centerElement":
                args["centerElement"] = _bool_text(
                    arg_value, path, "scrollUntilVisible.centerElement")
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"scrollUntilVisible.{arg_key.text}",
                        "Maestro's time-based scrolling maps to Autonom's "
                        "bounded maxSwipes; tune maxSwipes in the imported "
                        "flow instead of timeout/speed.")
        if "selector" not in args:
            _refuse(path, line, col, "scrollUntilVisible without an element",
                    "Give scrollUntilVisible an element selector.")
        return Step("scrollUntilVisible", args, line, col)
    if name == "retry":
        if isinstance(value, Scalar):
            _refuse(path, line, col, "retry shorthand",
                    "Use the map form with a commands list.")
        args = {}
        for arg_key, arg_value in _require_mapping(value, path, "retry").pairs:
            if arg_key.text == "maxRetries":
                retries = _int_text(arg_value, path, "retry.maxRetries")
                if retries < 0:
                    _refuse(path, arg_key.line, arg_key.col,
                            f"retry.maxRetries: {retries}",
                            "maxRetries cannot be negative.")
                if retries + 1 > 3:
                    _refuse(path, arg_key.line, arg_key.col,
                            f"retry.maxRetries: {retries}",
                            "Autonom caps retry at 3 attempts total "
                            "(maxRetries 2); retrying more hides defects.")
                args["maxAttempts"] = retries + 1
            elif arg_key.text == "commands":
                if not isinstance(arg_value, Sequence):
                    _refuse(path, arg_key.line, arg_key.col, "retry.commands",
                            "retry.commands must be a list of commands.")
                args["commands"] = _steps_from(arg_value, path)
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"retry.{arg_key.text}",
                        "Core Profile retry supports maxRetries, commands, "
                        "and label; file subflows do not import into retry.")
        if not args.get("commands"):
            _refuse(path, line, col, "retry without commands",
                    "Inline the retried commands; retry over a file does "
                    "not import.")
        args.setdefault("maxAttempts", 2)  # Maestro default maxRetries: 1
        for sub in args["commands"]:
            if sub.command in ("runFlow", "retry", "repeat"):
                _refuse(path, sub.line, sub.col, f"{sub.command} inside retry",
                        "Autonom retry blocks stay small and atomic; nested "
                        "composition does not import into retry.")
            if REGISTRY[sub.command].mutating:
                # Maestro retries mutations by default; Autonom demands the
                # intent be explicit — and the imported file shows it.
                args["allowMutations"] = True
        return Step("retry", args, line, col)
    if name in ("stopApp", "clearState") and isinstance(value, Scalar):
        _refuse(path, line, col, f"{name} with an inline appId",
                "Set appId in the header; per-step app switching is not "
                "part of the Core Profile.")
    if name == "launchApp":
        if isinstance(value, Scalar):  # launchApp: com.example — appId override
            _refuse(path, line, col, "launchApp with an inline appId",
                    "Set appId in the header; per-step app switching is not "
                    "part of the Core Profile.")
        args: dict = {}
        for arg_key, arg_value in _require_mapping(value, path,
                                                   "launchApp").pairs:
            if arg_key.text == "clearState":
                args["clearState"] = _bool_text(arg_value, path,
                                                "launchApp.clearState")
            elif arg_key.text == "stopApp":
                # Maestro stops the app before launching by default — that
                # is our fresh launch; `stopApp: false` is our resume.
                if not _bool_text(arg_value, path, "launchApp.stopApp"):
                    args["resume"] = True
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"launchApp.{arg_key.text}",
                        "Core Profile launchApp supports clearState, stopApp, "
                        "and label.")
        return Step("launchApp", args, line, col)
    if name == "swipe":
        if isinstance(value, Scalar):
            _refuse(path, line, col, "swipe shorthand",
                    "Use swipe with a direction: swipe: {direction: up}.")
        args = {}
        for arg_key, arg_value in _require_mapping(value, path, "swipe").pairs:
            if arg_key.text == "direction":
                args["direction"] = _scalar_text(arg_value, path).lower()
            elif arg_key.text == "duration":
                args["durationMs"] = _int_text(arg_value, path, "swipe.duration")
            elif arg_key.text == "from":
                args["from"] = _selector_from(arg_value, path)
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"swipe.{arg_key.text}",
                        "Core Profile swipe supports direction, from, and "
                        "duration; start/end points do not import.")
        if "direction" not in args:
            _refuse(path, line, col, "swipe without a direction",
                    "Point-to-point swipes do not import.")
        return Step("swipe", args, line, col)
    if name == "waitForAnimationToEnd":
        args = {}
        if not isinstance(value, Mapping):
            _refuse(path, line, col, "waitForAnimationToEnd shorthand",
                    "Use the bare command or the map form with timeout.")
        for arg_key, arg_value in value.pairs:
            if arg_key.text == "timeout":
                args["timeoutMs"] = _int_text(arg_value, path,
                                              "waitForAnimationToEnd.timeout")
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"waitForAnimationToEnd.{arg_key.text}",
                        "Core Profile waitForAnimationToEnd supports timeout "
                        "and label.")
        return Step("waitForSettled", args, line, col)
    if name == "extendedWaitUntil":
        args = {}
        for arg_key, arg_value in _require_mapping(value, path,
                                                   "extendedWaitUntil").pairs:
            if arg_key.text in ("visible", "notVisible"):
                args[arg_key.text] = _selector_from(arg_value, path)
            elif arg_key.text == "timeout":
                args["timeoutMs"] = _int_text(arg_value, path,
                                              "extendedWaitUntil.timeout")
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            elif arg_key.text == "optional":
                # only legal right before an optional tap on the same
                # element (_fold_optional_waits); refused anywhere else
                if _bool_text(arg_value, path, "extendedWaitUntil.optional"):
                    args["optional"] = True
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"extendedWaitUntil.{arg_key.text}", "")
        args.setdefault("timeoutMs", 10_000)
        return Step("waitUntil", args, line, col)
    if name == "runFlow":
        if isinstance(value, Scalar):
            return Step("runFlow", {"file": _scalar_text(value, path)}, line, col)
        args = {}
        for arg_key, arg_value in _require_mapping(value, path,
                                                   "runFlow").pairs:
            if arg_key.text == "file":
                args["file"] = _scalar_text(arg_value, path)
            elif arg_key.text == "commands":
                if not isinstance(arg_value, Sequence):
                    _refuse(path, arg_key.line, arg_key.col,
                            "runFlow.commands",
                            "runFlow.commands must be a list of commands.")
                args["commands"] = _steps_from(arg_value, path)
            elif arg_key.text == "env":
                env: dict = {}
                _env_into(env, arg_value, path)
                args["env"] = env
            elif arg_key.text == "when":
                args["when"] = _when_from(arg_value, path)
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"runFlow.{arg_key.text}",
                        "Core Profile runFlow supports file, env, when, label.")
        present = [n for n in ("file", "commands") if n in args]
        if len(present) != 1:
            _refuse(path, line, col, "runFlow needs exactly one of file or "
                                     "commands", "")
        if "commands" in args and not args["commands"]:
            _refuse(path, line, col, "runFlow with empty commands",
                    "Give the inline subflow at least one command.")
        return Step("runFlow", args, line, col)
    _refuse(path, line, col, name, "not a Core Profile command")


def _while_from(node, path: str):
    from .schema import WhenClause
    _require_mapping(node, path, "while")
    when = WhenClause(line=node.line, col=node.col)
    for key, value in node.pairs:
        if key.text == "visible":
            when.visible = _selector_from(value, path)
        elif key.text == "notVisible":
            when.not_visible = _selector_from(value, path)
        else:
            _refuse(path, key.line, key.col, f"while.{key.text}",
                    "repeat.while imports visible/notVisible only; a JS "
                    "'true:' condition has no Autonom equivalent.")
    return when


def _when_from(node, path: str):
    from .schema import WhenClause
    _require_mapping(node, path, "when")
    when = WhenClause(line=node.line, col=node.col)
    for key, value in node.pairs:
        if key.text == "platform":
            when.platform = _scalar_text(value, path).lower()
        elif key.text == "visible":
            when.visible = _selector_from(value, path)
        elif key.text == "notVisible":
            when.not_visible = _selector_from(value, path)
        else:
            _refuse(path, key.line, key.col, f"when.{key.text}",
                    "Core Profile conditions: platform, visible, notVisible.")
    return when


def import_flow(text: str, path: str) -> str:
    """Maestro YAML -> canonical Autonom Flow v1 text (validated)."""
    # JS interpolation would otherwise die in the strict parser with a
    # generic message; name the real problem first, with its position.
    for line_number, line in enumerate(text.split("\n"), start=1):
        match = _JS_INTERP_RE.search(line)
        if match:
            raise errors.AutonomError(
                errors.UNSUPPORTED_FLOW_COMMAND,
                f"{path}:{line_number}:{match.start() + 1}: JavaScript "
                f"interpolation has no Autonom equivalent: {match.group(0)!r}",
                hint="Only ${NAME} environment interpolation carries over.",
                file=path, line=line_number, column=match.start() + 1,
            )
    document = parse_document(text, path, allow_flow_mappings=True)
    flow = Flow(path=path, name="")
    for key, value in document.header.pairs:
        name = key.text
        if name not in _CORE_HEADER:
            _refuse(path, key.line, key.col, f"header field {name}",
                    "Core Profile header: appId, name, tags, env, properties, "
                    "onFlowStart, onFlowComplete.")
        if name == "url":
            _refuse(path, key.line, key.col, "header field url",
                    "Autonom has no web target; Maestro web flows do not "
                    "import.")
        if name == "appId":
            flow.app_id = _scalar_text(value, path)
        elif name == "name":
            flow.name = _scalar_text(value, path)
        elif name == "tags":
            if not isinstance(value, Sequence):
                _refuse(path, key.line, key.col, "header field tags",
                        "tags must be a list.")
            flow.tags = [_scalar_text(item, path) for item in value.items]
        elif name == "env":
            _env_into(flow.env, value, path)
        elif name == "properties":
            for prop_key, prop_value in _require_mapping(
                    value, path, "header field properties").pairs:
                flow.properties[prop_key.text] = _scalar_text(prop_value, path)
        elif name in ("onFlowStart", "onFlowComplete"):
            if not isinstance(value, Sequence):
                _refuse(path, key.line, key.col, f"header field {name}",
                        f"{name} must be a list of commands.")
            steps = _steps_from(value, path)
            if name == "onFlowStart":
                flow.on_flow_start = steps
            else:
                flow.on_flow_complete = steps
    if not flow.name:
        flow.name = "Imported Maestro flow"
    flow.steps = _steps_from(document.commands, path)

    canonical_text = emit_flow(flow)
    # The emitted text must stand on its own — parse and build it back. An
    # error escaping here is an importer gap (source-side validation should
    # have refused first), so never present canonical-text coordinates as if
    # they were positions in the user's Maestro file.
    try:
        build_flow(parse_document(canonical_text, path))
    except errors.AutonomError as exc:
        raise errors.AutonomError(
            errors.UNSUPPORTED_FLOW_COMMAND,
            f"{path}: the converted flow failed Flow v1 validation: "
            f"{exc.message}",
            hint="Positions inside the quoted message refer to the converted "
                 "text, not the source file.",
            file=path, detail=exc.message,
        )
    return canonical_text


# --- export ------------------------------------------------------------------

_REGEX_META_RE = re.compile(r"([.^$*+?()\[\]{}|\\])")
# YAML 1.1 (Maestro's snakeyaml) resolves these plain scalars to non-strings
_YAML_SPECIAL_WORDS = {"y", "n", "yes", "no", "true", "false", "on", "off",
                       "null", "~", "=", "<<"}
_YAML_NUMBERISH_RE = re.compile(r"^[-+]?(?:\d|\.\d|\.inf$|\.nan$)", re.I)
_YAML_UNSAFE_FIRST = set("-?:,[]{}#&*!|>'\"%@`")


def _yaml(value) -> str:
    """One scalar in a form every YAML reader (and our parser) reads back
    as exactly this string. Quoting is minimal: plain when plain is safe."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in "\n\r\t"):
        escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r")
                   .replace("\t", "\\t"))
        return f'"{escaped}"'
    needs = (
        text == "" or text != text.strip(" ")
        or text[0] in _YAML_UNSAFE_FIRST
        or ": " in text or text.endswith(":") or " #" in text
        or text.lower() in _YAML_SPECIAL_WORDS
        or bool(_YAML_NUMBERISH_RE.match(text))
    )
    if not needs:
        return text
    return "'" + text.replace("'", "''") + "'"


def _comment(text) -> str:
    """A value folded into a single comment line."""
    return " ".join(str(text).split())


def _escape_literal(text: str) -> str:
    """Regex-escape only real metacharacters: `Sign in` stays `Sign in`
    (Python's re.escape also escapes the space, which then looked like a
    pattern on the way back in)."""
    return _REGEX_META_RE.sub(r"\\\1", text)


def _group_end(pattern: str, open_index: int) -> int | None:
    """Index of the ')' closing the group opened at ``open_index``."""
    depth = 0
    i = open_index
    in_class = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
            if pattern[i + 1:i + 2] == "]":
                i += 1  # a leading ']' is a literal inside the class
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _unanchored(value: str) -> str | None:
    """The Maestro pattern inside our importer's ``^(?:...)$`` wrapper."""
    flags = ""
    body = value
    if body.startswith("(?i)"):
        flags, body = "(?i)", body[4:]
    if not (body.startswith("^(?:") and body.endswith(")$")):
        return None
    if _group_end(body, 1) != len(body) - 2:
        return None  # `^(?:a)|(?:b)$` is not one wrapped group
    return flags + body[4:-2]


def _export_value(match: str, value) -> str:
    text = str(value)
    if match == "exact":
        return _escape_literal(text)
    if match == "caseInsensitiveExact":
        return f"(?i){_escape_literal(text)}"
    if match == "contains":
        return f".*{_escape_literal(text)}.*"
    # regex — ours is a search. The importer's own full-match wrapper goes
    # back to the pattern Maestro wrote; any other regex is widened to a
    # full match so it keeps meaning "found anywhere".
    inner = _unanchored(text)
    if inner is not None:
        return inner
    return f".*(?:{text}).*"


class _Refusals:
    """Every export refusal in one pass, each with its source line."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.items: list[dict] = []

    def add(self, line: int, command: str, message: str, hint: str = "") -> None:
        self.items.append({"line": line, "command": command,
                           "message": f"{self.path}:{line}: {message}",
                           "hint": hint})

    def raise_if_any(self) -> None:
        if not self.items:
            return
        first = self.items[0]
        message = first["message"]
        if len(self.items) > 1:
            message += (f" (and {len(self.items) - 1} more refusal(s); "
                        "see 'refusals')")
        raise errors.AutonomError(
            errors.UNSUPPORTED_FLOW_COMMAND, message, hint=first["hint"],
            file=self.path, line=first["line"], command=first["command"],
            refusals=self.items,
        )


_TIMED_ARMS = {"assertVisible": "visible", "assertNotVisible": "notVisible"}
_PLATFORMS = {"android": "Android", "ios": "iOS"}


class _Exporter:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lines: list[str] = []
        self.refusals = _Refusals(path)

    # -- pieces ---------------------------------------------------------------

    def selector(self, indent: str, selector: FlowSelector, line: int,
                 command: str) -> list[str] | None:
        """Selector lines, or None after recording a refusal."""
        if selector.relations:
            self.refusals.add(line, command,
                              "relational selectors do not export to the "
                              "Maestro Core Profile",
                              "Maestro Core Profile selectors are text, id, "
                              "index, and enabled.")
            return None
        out: list[str] = []
        written: set = set()
        for source, value in selector.source_fields.items():
            if source in ("text", "id", "visibleText"):
                # `visibleText` IS Maestro's `text` (the label union); our
                # strict `text` exports as `text` too — the closest Maestro
                # can express.
                field = "id" if source == "id" else "text"
                if field in written:
                    self.refusals.add(line, command,
                                      "a selector with both text and "
                                      "visibleText has no Maestro form",
                                      "Maestro has one text field; keep one "
                                      "of the two before exporting.")
                    return None
                written.add(field)
                out.append(f"{indent}{field}: "
                           f"{_yaml(_export_value(selector.match, value))}")
            elif source == "enabled":
                out.append(f"{indent}enabled: {'true' if value else 'false'}")
            else:
                self.refusals.add(line, command,
                                  f"selector field {source!r} has no Maestro "
                                  "Core Profile equivalent",
                                  "description/role/state/relational "
                                  "selectors do not export.")
                return None
        if selector.index is not None:
            out.append(f"{indent}index: {selector.index}")
        return out

    def mapping(self, prefix: str, command: str, fields: list[str]) -> None:
        if not fields:
            self.lines.append(f"{prefix}- {command}")
            return
        self.lines.append(f"{prefix}- {command}:")
        self.lines.extend(f"{prefix}    {item}" for item in fields)

    def when(self, indent: str, when, line: int, command: str) -> list[str] | None:
        out = [f"{indent}when:"]
        inner = indent + "  "
        if when.env_equals:
            self.refusals.add(line, command,
                              "runFlow.when.envEquals has no Maestro "
                              "Core Profile equivalent",
                              "Maestro conditions on env are JavaScript; "
                              "Core Profile conditions are platform, "
                              "visible, notVisible.")
            return None
        if when.platform:
            out.append(f"{inner}platform: "
                       f"{_PLATFORMS.get(when.platform, when.platform)}")
        for arm, selector in (("visible", when.visible),
                              ("notVisible", when.not_visible)):
            if selector is None:
                continue
            body = self.selector(inner + "  ", selector, line, command)
            if body is None:
                return None
            out.append(f"{inner}{arm}:")
            out.extend(body)
        return out

    # -- steps ----------------------------------------------------------------

    def step(self, prefix: str, step: Step) -> None:
        command, args, line = step.command, step.args, step.line
        lines = self.lines
        body = prefix + "    "

        def refuse(message: str, hint: str = "") -> None:
            self.refusals.add(line, command, message, hint)

        if command in ("checkpoint", "note"):
            detail = args.get("name") or args.get("text") or ""
            lines.append(f"{prefix}# autonom {command}: {_comment(detail)}")
            return
        if "postcondition" in args:
            refuse(f"{command}.postcondition has no Maestro equivalent",
                   "Maestro cannot verify a postcondition on the command "
                   "itself; follow it with an assertVisible, or drop the "
                   "postcondition before exporting.")
            return
        label = args.get("label")
        label_field = [f"label: {_yaml(label)}"] if label else []

        if command == "launchApp":
            fields = []
            if args.get("clearState"):
                fields.append("clearState: true")
            if args.get("resume"):
                # Maestro's launchApp stops the app first (stopApp: true) —
                # our fresh launch. A bare `- launchApp` would silently turn
                # a resume into a restart.
                fields.append("stopApp: false")
            self.mapping(prefix, command, fields + label_field)
            return
        if command == "eraseText":
            chars = args.get("chars")
            # Maestro's bare `- eraseText` erases everything; ours erases a
            # count. Emitting the bare form would silently change the flow.
            if label:
                fields = ([f"charactersToErase: {chars}"]
                          if chars is not None else [])
                self.mapping(prefix, command, fields + label_field)
            else:
                lines.append(f"{prefix}- eraseText: {chars}" if chars is not None
                             else f"{prefix}- eraseText")
            return
        if command in ("stopApp", "clearState", "back"):
            lines.append(f"{prefix}- {command}")
            if label:
                lines.append(f"{prefix}  # label: {_comment(label)}")
            return
        if command in ("tapOn", "longPressOn", "assertVisible",
                       "assertNotVisible", "doubleTapOn"):
            if args.get("repeat") or "delayMs" in args:
                refuse("tapOn repeat/delayMs does not export yet",
                       "Unroll the repeated tap into separate tapOn steps "
                       "before exporting.")
                return
            if command == "longPressOn" and "durationMs" in args:
                refuse("longPressOn.durationMs has no Maestro equivalent",
                       "Maestro long-presses for its own fixed duration; drop "
                       "durationMs before exporting.")
                return
            if "timeoutMs" in args:
                # Maestro's per-command lookup timeout lives on
                # extendedWaitUntil, not on tapOn/assertVisible. A visibility
                # assertion *is* an extendedWaitUntil, so export it as one and
                # keep the timeout. A tap's timeoutMs bounds the wait for its
                # target to appear: in Maestro that is an extendedWaitUntil
                # on the same element, then the tap. An optional tap makes
                # the wait optional too (the import folds the pair back).
                arm = _TIMED_ARMS.get(command)
                selector = self.selector(body + "    ", step.selector, line,
                                         command)
                if selector is None:
                    return
                lines.append(f"{prefix}- extendedWaitUntil:")
                lines.append(f"{body}{arm or 'visible'}:")
                lines.extend(selector)
                lines.append(f"{body}timeout: {args['timeoutMs']}")
                if arm is not None:
                    lines.extend(f"{body}{item}" for item in label_field)
                    return
                if args.get("optional"):
                    lines.append(f"{body}optional: true")
            selector = self.selector(body, step.selector, line, command)
            if selector is None:
                return
            lines.append(f"{prefix}- {command}:")
            lines.extend(selector)
            # Maestro carries these on the selector map itself
            lines.extend(f"{body}{item}" for item in label_field)
            if args.get("optional"):
                lines.append(f"{body}optional: true")
                if args.get("reason"):
                    lines.append(f"{body}# reason: {_comment(args['reason'])}")
            return
        if command == "inputText":
            if "timeoutMs" in args:
                refuse("inputText.timeoutMs has no Maestro equivalent",
                       "timeoutMs bounds Autonom's focused-field wait; Maestro "
                       "types without waiting for focus. Drop it before "
                       "exporting.")
                return
            # requireFocus needs no mapping: Maestro never checks focus, which
            # is exactly what `requireFocus: false` asks for, and the default
            # focus check is an Autonom-side safety net with no Maestro form.
            if args.get("sensitive"):
                lines.append(f"{prefix}# autonom: sensitive input — Maestro "
                             "does not redact it")
            if label:
                self.mapping(prefix, command,
                             [f"text: {_yaml(args['value'])}"] + label_field)
            else:
                lines.append(f"{prefix}- inputText: {_yaml(args['value'])}")
            return
        if command == "openLink":
            if label:
                self.mapping(prefix, command,
                             [f"link: {_yaml(args['url'])}"] + label_field)
            else:
                lines.append(f"{prefix}- openLink: {_yaml(args['url'])}")
            return
        if command == "pressKey":
            lines.append(f"{prefix}- pressKey: {_yaml(args['key'])}")
            if label:
                lines.append(f"{prefix}  # label: {_comment(label)}")
            return
        if command == "takeScreenshot":
            lines.append(f"{prefix}- takeScreenshot: {_yaml(label)}" if label
                         else f"{prefix}- takeScreenshot")
            return
        if command == "waitForSettled":
            if "quietMs" in args:
                refuse("waitForSettled.quietMs has no Maestro equivalent",
                       "Maestro's waitForAnimationToEnd has no quiet window; "
                       "drop quietMs before exporting.")
                return
            fields = ([f"timeout: {args['timeoutMs']}"]
                      if "timeoutMs" in args else [])
            self.mapping(prefix, "waitForAnimationToEnd", fields + label_field)
            return
        if command == "waitUntil":
            arms: list[str] = []
            for arm in ("visible", "notVisible"):
                if arm in args:
                    selector = self.selector(body + "    ", args[arm], line,
                                             command)
                    if selector is None:
                        return
                    arms.append(f"{body}{arm}:")
                    arms.extend(selector)
            lines.append(f"{prefix}- extendedWaitUntil:")
            lines.extend(arms)
            lines.append(f"{body}timeout: {args['timeoutMs']}")
            lines.extend(f"{body}{item}" for item in label_field)
            return
        if command == "swipe":
            if "from" in args:
                refuse("swipe from: does not export yet",
                       "Swipe by direction only, or drop 'from' before "
                       "exporting.")
                return
            fields = [f"direction: {args['direction'].upper()}"]
            if "durationMs" in args:
                fields.append(f"duration: {args['durationMs']}")
            self.mapping(prefix, command, fields + label_field)
            return
        if command == "runFlow":
            if "commands" in args:
                refuse("inline runFlow commands do not export yet",
                       "Extract the inline body into a subflow file.")
                return
            fields = [f"file: {_yaml(args['file'])}"]
            if args.get("env"):
                fields.append("env:")
                fields.extend(f"  {key}: {_yaml(value)}"
                              for key, value in args["env"].items())
            if args.get("when") is not None:
                when = self.when("", args["when"], line, command)
                if when is None:
                    return
                fields.extend(when)
            if len(fields) == 1 and not label:
                lines.append(f"{prefix}- runFlow: {_yaml(args['file'])}")
            else:
                self.mapping(prefix, command, fields + label_field)
            return
        refuse(f"{command!r} has no Maestro Core Profile equivalent",
               "group, setOrientation and assertEnabled/Checked are "
               "Autonom-only; retry, scrollUntilVisible, scroll, repeat and "
               "the clipboard commands exist in Maestro and import, but do "
               "not export yet.")


def export_flow(flow: Flow, path: str) -> str:
    """Autonom Flow -> Maestro Core Profile YAML.

    Every step is examined before failing: the refusal names the first
    problem, and ``refusals`` in the error carries all of them with lines.
    """
    exporter = _Exporter(path)
    lines = exporter.lines
    lines.append(f"appId: {_yaml(flow.app_id or 'com.example.app')}")
    if flow.name:
        lines.append(f"name: {_yaml(flow.name)}")
    if flow.tags:
        lines.append("tags:")
        lines.extend(f"  - {_yaml(tag)}" for tag in flow.tags)
    if flow.env:
        lines.append("env:")
        lines.extend(f"  {key}: {_yaml(value)}" for key, value in flow.env.items())
    if flow.properties:
        lines.append("properties:")
        lines.extend(f"  {key}: {_yaml(value)}"
                     for key, value in flow.properties.items())
    for hook, steps in (("onFlowStart", flow.on_flow_start),
                        ("onFlowComplete", flow.on_flow_complete)):
        if steps:
            lines.append(f"{hook}:")
            for step in steps:
                exporter.step("  ", step)
    lines.append("---")
    for step in flow.steps:
        exporter.step("", step)
    exporter.refusals.raise_if_any()
    return "\n".join(lines) + "\n"
