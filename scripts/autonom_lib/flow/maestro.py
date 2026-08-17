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
- ``extendedWaitUntil`` becomes ``waitUntil``; ``takeScreenshot``'s path
  becomes a label; ``launchApp.clearState`` carries over.
- JavaScript interpolation (``${output.x}``) has no Autonom equivalent and
  is refused, not approximated.

On export, ``match: exact`` text is regex-escaped so Maestro's regex
matching stays exact; Autonom-only commands refuse (evidence commands
``checkpoint``/``note`` become comments instead — they carry no behavior).
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
    "waitForAnimationToEnd": "use waitUntil with an explicit timeoutMs",
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


def _import_pattern(pattern: str) -> tuple[str, str]:
    """Maestro full-match regex -> (autonom text, match mode)."""
    if _PLAIN_TEXT_RE.match(pattern):
        return pattern, "exact"
    return f"^(?:{pattern})$", "regex"


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
    if "regex" in modes:
        selector.match = "regex"
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
    return [_step_from(item, path) for item in sequence.items]


def _step_from(item, path: str) -> Step:
    if isinstance(item, Scalar):
        name, line, col = item.text, item.line, item.col
        if name in ("launchApp", "stopApp", "clearState", "back",
                    "takeScreenshot", "eraseText", "scroll", "pasteText"):
            return Step(name, {}, line, col)
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
            elif arg_key.text == "label":
                args["label"] = _scalar_text(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"launchApp.{arg_key.text}",
                        "Core Profile launchApp supports clearState and label.")
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


def _export_pattern(selector: FlowSelector, path: str) -> list[tuple[str, str]]:
    """Autonom selector -> Maestro (field, regex) pairs."""
    pairs: list[tuple[str, str]] = []
    for source, value in selector.source_fields.items():
        if source in ("text", "id", "visibleText"):
            if selector.match == "exact":
                pattern = re.escape(str(value))
            elif selector.match == "caseInsensitiveExact":
                pattern = f"(?i){re.escape(str(value))}"
            elif selector.match == "contains":
                pattern = f".*{re.escape(str(value))}.*"
            else:  # regex — ours is search; anchor for Maestro's full match
                pattern = f".*(?:{value}).*"
            # `visibleText` IS Maestro's `text` (the label union); our strict
            # `text` exports as `text` too — the closest Maestro can express.
            pairs.append(("text" if source == "visibleText" else source,
                          pattern))
        elif source in ("enabled",):
            pairs.append((source, "true" if value else "false"))
        else:
            raise errors.AutonomError(
                errors.UNSUPPORTED_FLOW_COMMAND,
                f"{path}: selector field {source!r} has no Maestro Core "
                "Profile equivalent",
                hint="description/role/state/relational selectors do not export.",
                file=path,
            )
    return pairs


def export_flow(flow: Flow, path: str) -> str:
    """Autonom Flow -> Maestro Core Profile YAML."""
    lines: list[str] = [f"appId: {flow.app_id or 'com.example.app'}"]
    if flow.name:
        lines.append(f"name: {flow.name}")
    if flow.tags:
        lines.append("tags:")
        lines.extend(f"  - {tag}" for tag in flow.tags)
    if flow.env:
        lines.append("env:")
        lines.extend(f"  {key}: {value}" for key, value in flow.env.items())
    lines.append("---")

    def emit_selector(indent: str, selector: FlowSelector) -> None:
        for field, pattern in _export_pattern(selector, path):
            if any(ch in pattern for ch in ":#'\""):
                quoted = pattern.replace("'", "''")
                lines.append(f"{indent}{field}: '{quoted}'")
            else:
                lines.append(f"{indent}{field}: {pattern}")
        if selector.index is not None:
            lines.append(f"{indent}index: {selector.index}")

    for step in flow.steps:
        command = step.command
        if command in ("checkpoint", "note"):
            detail = step.args.get("name") or step.args.get("text") or ""
            lines.append(f"# autonom {command}: {detail}")
            continue
        if command in ("launchApp",) and not step.args.get("clearState"):
            lines.append("- launchApp")
            continue
        if command == "launchApp":
            lines.append("- launchApp:")
            lines.append("    clearState: true")
            continue
        if command in ("stopApp", "clearState", "back", "eraseText"):
            lines.append(f"- {command}")
            continue
        if command in ("tapOn", "longPressOn", "assertVisible", "assertNotVisible",
                       "doubleTapOn"):
            if step.args.get("repeat"):
                raise errors.AutonomError(
                    errors.UNSUPPORTED_FLOW_COMMAND,
                    f"{path}: tapOn repeat/delayMs does not export yet",
                    file=path, line=step.line, command=command,
                )
            selector = step.selector
            if selector.relations:
                raise errors.AutonomError(
                    errors.UNSUPPORTED_FLOW_COMMAND,
                    f"{path}: relational selectors do not export to the "
                    "Maestro Core Profile",
                    file=path, line=step.line,
                )
            lines.append(f"- {command}:")
            emit_selector("    ", selector)
            continue
        if command == "inputText":
            lines.append(f"- inputText: {step.args['value']}")
            continue
        if command == "openLink":
            lines.append(f"- openLink: {step.args['url']}")
            continue
        if command == "pressKey":
            lines.append(f"- pressKey: {step.args['key']}")
            continue
        if command == "takeScreenshot":
            label = step.args.get("label")
            lines.append(f"- takeScreenshot: {label}" if label
                         else "- takeScreenshot")
            continue
        if command == "waitUntil":
            lines.append("- extendedWaitUntil:")
            for arm in ("visible", "notVisible"):
                if arm in step.args:
                    lines.append(f"    {arm}:")
                    emit_selector("        ", step.args[arm])
            lines.append(f"    timeout: {step.args['timeoutMs']}")
            continue
        if command == "swipe":
            if "from" in step.args:
                raise errors.AutonomError(
                    errors.UNSUPPORTED_FLOW_COMMAND,
                    f"{path}: swipe from: does not export yet",
                    file=path, line=step.line, command=command,
                )
            lines.append("- swipe:")
            lines.append(f"    direction: {step.args['direction'].upper()}")
            if "durationMs" in step.args:
                lines.append(f"    duration: {step.args['durationMs']}")
            continue
        if command == "runFlow":
            if "commands" in step.args:
                raise errors.AutonomError(
                    errors.UNSUPPORTED_FLOW_COMMAND,
                    f"{path}: inline runFlow commands do not export yet",
                    hint="Extract the inline body into a subflow file.",
                    file=path, line=step.line, command=command,
                )
            if step.args.get("env"):
                lines.append("- runFlow:")
                lines.append(f"    file: {step.args['file']}")
                lines.append("    env:")
                lines.extend(f"      {k}: {v}"
                             for k, v in step.args["env"].items())
            else:
                lines.append(f"- runFlow: {step.args['file']}")
            continue
        raise errors.AutonomError(
            errors.UNSUPPORTED_FLOW_COMMAND,
            f"{path}: {command!r} has no Maestro Core Profile equivalent",
            hint="retry, group, setOrientation, scrollUntilVisible, "
                 "assertEnabled/Checked stay Autonom-only; the 0.28.1 "
                 "commands (scroll, repeat, clipboard variables) do not "
                 "export yet.",
            file=path, line=step.line, command=command,
        )
    return "\n".join(lines) + "\n"
