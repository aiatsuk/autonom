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
from .schema import Flow, FlowSelector, Step, build_flow

_PLAIN_TEXT_RE = re.compile(r"^[^.^$*+?()\[\]{}|\\]*$")
_JS_INTERP_RE = re.compile(r"\$\{[^}]*[^A-Za-z0-9_}][^}]*\}")

_CORE_HEADER = ("appId", "name", "tags", "env")

_UNSUPPORTED_HINTS = {
    "runScript": "Replace runScript with a deterministic subflow or execute it outside Flow v1",
    "evalScript": "Flow v1 has no script engine, by design",
    "repeat": "unbounded loops are not part of Flow v1",
    "copyTextFrom": "value extraction is not part of the Core Profile",
    "pasteText": "value extraction is not part of the Core Profile",
    "inputRandomText": "flows are deterministic; pass the value via env or --secret",
    "inputRandomNumber": "flows are deterministic; pass the value via env or --secret",
    "inputRandomEmail": "flows are deterministic; pass the value via env or --secret",
    "inputRandomPersonName": "flows are deterministic; pass the value via env or --secret",
    "hideKeyboard": "no reliable cross-platform substrate; press KEYCODE_BACK on Android",
    "waitForAnimationToEnd": "use waitUntil with an explicit timeoutMs",
    "scroll": "use swipe with a direction, or scrollUntilVisible",
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
    if isinstance(node, Scalar):
        text, mode = _import_pattern(_no_js(node.text, path, node.line, node.col))
        return FlowSelector(fields={"text": text}, match=mode,
                            source_fields={"text": text},
                            line=node.line, col=node.col)
    selector = FlowSelector(line=node.line, col=node.col)
    modes = set()
    for key, value in node.pairs:
        name = key.text
        if name in ("text", "id"):
            raw = _scalar_text(value, path)
            pattern, mode = _import_pattern(raw)
            field = "text" if name == "text" else "resource_id"
            source = "text" if name == "text" else "id"
            selector.fields[field] = pattern
            selector.source_fields[source] = pattern
            modes.add(mode)
        elif name == "index":
            selector.index = int(_scalar_text(value, path))
        elif name == "enabled":
            flag = _scalar_text(value, path) == "true"
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
    return selector


def _scalar_text(node, path: str) -> str:
    if not isinstance(node, Scalar):
        raise errors.AutonomError(
            errors.UNSUPPORTED_FLOW_COMMAND,
            f"{path}:{getattr(node, 'line', 0)}: nested structures here are "
            "outside the Core Profile",
            file=path, line=getattr(node, "line", 0),
        )
    return _no_js(node.text, path, node.line, node.col)


def _steps_from(sequence: Sequence, path: str) -> list[Step]:
    return [_step_from(item, path) for item in sequence.items]


def _step_from(item, path: str) -> Step:
    if isinstance(item, Scalar):
        name, line, col = item.text, item.line, item.col
        if name in ("launchApp", "stopApp", "clearState", "back",
                    "takeScreenshot", "eraseText"):
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

    if name in ("tapOn", "longPressOn", "assertVisible", "assertNotVisible"):
        selector = _selector_from(value, path)
        return Step(name, {"selector": selector}, line, col)
    if name == "doubleTapOn":
        return Step("doubleTapOn", {"selector": _selector_from(value, path)},
                    line, col)
    if name == "inputText":
        return Step("inputText", {"value": _scalar_text(value, path)}, line, col)
    if name == "openLink":
        return Step("openLink", {"url": _scalar_text(value, path)}, line, col)
    if name == "takeScreenshot":
        return Step("takeScreenshot", {"label": _scalar_text(value, path)},
                    line, col)
    if name == "pressKey":
        return Step("pressKey", {"key": _scalar_text(value, path)}, line, col)
    if name == "eraseText":
        return Step("eraseText", {"chars": int(_scalar_text(value, path))},
                    line, col)
    if name == "launchApp":
        if isinstance(value, Scalar):  # launchApp: com.example — appId override
            _refuse(path, line, col, "launchApp with an inline appId",
                    "Set appId in the header; per-step app switching is not "
                    "part of the Core Profile.")
        args: dict = {}
        for arg_key, arg_value in value.pairs:
            if arg_key.text == "clearState":
                args["clearState"] = _scalar_text(arg_value, path) == "true"
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"launchApp.{arg_key.text}",
                        "Core Profile launchApp supports clearState only.")
        return Step("launchApp", args, line, col)
    if name == "swipe":
        if isinstance(value, Scalar):
            _refuse(path, line, col, "swipe shorthand",
                    "Use swipe with a direction: swipe: {direction: up}.")
        args = {}
        for arg_key, arg_value in value.pairs:
            if arg_key.text == "direction":
                args["direction"] = _scalar_text(arg_value, path).lower()
            elif arg_key.text == "duration":
                args["durationMs"] = int(_scalar_text(arg_value, path))
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"swipe.{arg_key.text}",
                        "Core Profile swipe supports direction and duration; "
                        "start/end points do not import.")
        if "direction" not in args:
            _refuse(path, line, col, "swipe without a direction",
                    "Point-to-point swipes do not import.")
        return Step("swipe", args, line, col)
    if name == "extendedWaitUntil":
        args = {}
        for arg_key, arg_value in value.pairs:
            if arg_key.text in ("visible", "notVisible"):
                args[arg_key.text] = _selector_from(arg_value, path)
            elif arg_key.text == "timeout":
                args["timeoutMs"] = int(_scalar_text(arg_value, path))
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"extendedWaitUntil.{arg_key.text}", "")
        args.setdefault("timeoutMs", 10_000)
        return Step("waitUntil", args, line, col)
    if name == "runFlow":
        if isinstance(value, Scalar):
            return Step("runFlow", {"file": _scalar_text(value, path)}, line, col)
        args = {}
        for arg_key, arg_value in value.pairs:
            if arg_key.text == "file":
                args["file"] = _scalar_text(arg_value, path)
            elif arg_key.text == "env":
                env = {}
                for env_key, env_value in arg_value.pairs:
                    env[env_key.text] = _scalar_text(env_value, path)
                args["env"] = env
            elif arg_key.text == "when":
                args["when"] = _when_from(arg_value, path)
            else:
                _refuse(path, arg_key.line, arg_key.col,
                        f"runFlow.{arg_key.text}",
                        "Core Profile runFlow supports file, env, when.")
        if "file" not in args:
            _refuse(path, line, col, "runFlow without a file",
                    "Inline commands do not import; use a subflow file.")
        return Step("runFlow", args, line, col)
    _refuse(path, line, col, name, "not a Core Profile command")


def _when_from(node, path: str):
    from .schema import WhenClause
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
    document = parse_document(text, path)
    flow = Flow(path=path, name="")
    for key, value in document.header.pairs:
        name = key.text
        if name not in _CORE_HEADER:
            _refuse(path, key.line, key.col, f"header field {name}",
                    "Core Profile header: appId, name, tags, env.")
        if name == "appId":
            flow.app_id = _scalar_text(value, path)
        elif name == "name":
            flow.name = _scalar_text(value, path)
        elif name == "tags":
            if isinstance(value, Sequence):
                flow.tags = [_scalar_text(item, path) for item in value.items]
        elif name == "env":
            for env_key, env_value in value.pairs:
                flow.env[env_key.text] = _scalar_text(env_value, path)
    if not flow.name:
        flow.name = "Imported Maestro flow"
    flow.steps = _steps_from(document.commands, path)

    canonical_text = emit_flow(flow)
    # The emitted text must stand on its own — parse and build it back.
    build_flow(parse_document(canonical_text, path))
    return canonical_text


# --- export ------------------------------------------------------------------


def _export_pattern(selector: FlowSelector, path: str) -> list[tuple[str, str]]:
    """Autonom selector -> Maestro (field, regex) pairs."""
    pairs: list[tuple[str, str]] = []
    for source, value in selector.source_fields.items():
        if source in ("text", "id"):
            if selector.match == "exact":
                pattern = re.escape(str(value))
            elif selector.match == "caseInsensitiveExact":
                pattern = f"(?i){re.escape(str(value))}"
            elif selector.match == "contains":
                pattern = f".*{re.escape(str(value))}.*"
            else:  # regex — ours is search; anchor for Maestro's full match
                pattern = f".*(?:{value}).*"
            pairs.append((source, pattern))
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
            lines.append("- swipe:")
            lines.append(f"    direction: {step.args['direction'].upper()}")
            if "durationMs" in step.args:
                lines.append(f"    duration: {step.args['durationMs']}")
            continue
        if command == "runFlow":
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
            hint="retry, group, setOrientation, scrollUntilVisible, and "
                 "assertEnabled/Checked stay Autonom-only.",
            file=path, line=step.line, command=command,
        )
    return "\n".join(lines) + "\n"
