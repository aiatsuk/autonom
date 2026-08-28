"""Flow v1 typed model: node tree → dataclasses, via the command registry.

Single source of truth for the language surface: header fields, the command
registry (name → arg spec, shorthand rule, mutating flag), selector fields
and match modes, and the failure-class table. The docs gate
(``tests/test_docs_flow_surface.py``) enumerates these tables against
``docs/FLOW.md`` in both directions, the same way the CLI surface is pinned.

Everything here is pure data transformation with positioned errors — no
filesystem access (that is ``validator.py``) and no device knowledge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import FLOW_SCHEMA_ID
from .. import errors
from .parser import FlowDocument, Mapping, Scalar, Sequence
from ..providers import SEMANTIC_CAPABILITIES

# --- Failure classes ---------------------------------------------------------
TEST_FAILURE = "test_failure"
FLOW_DEFINITION = "flow_definition"
INFRASTRUCTURE = "infrastructure"

_FAILURE_CLASS_BY_CODE = {
    # The app under test did not behave as the flow asserts.
    errors.FLOW_ASSERTION_TIMEOUT: TEST_FAILURE,
    errors.FLOW_COPY_EMPTY: TEST_FAILURE,
    errors.NO_MATCHING_NODE: TEST_FAILURE,
    errors.AMBIGUOUS_SELECTOR: TEST_FAILURE,
    errors.SELECTOR_INDEX_OUT_OF_RANGE: TEST_FAILURE,
    errors.COORDINATE_SPACE_MISMATCH: TEST_FAILURE,
    # The flow file itself is wrong for this target or malformed.
    errors.FLOW_PARSE_ERROR: FLOW_DEFINITION,
    errors.FLOW_SCHEMA_UNSUPPORTED: FLOW_DEFINITION,
    errors.FLOW_HEADER_INVALID: FLOW_DEFINITION,
    errors.FLOW_UNKNOWN_COMMAND: FLOW_DEFINITION,
    errors.FLOW_COMMAND_INVALID: FLOW_DEFINITION,
    errors.FLOW_SELECTOR_INVALID: FLOW_DEFINITION,
    errors.FLOW_OPTIONAL_ASSERTION_FORBIDDEN: FLOW_DEFINITION,
    errors.FLOW_VAR_UNDEFINED: FLOW_DEFINITION,
    errors.FLOW_VAR_CONFLICT: FLOW_DEFINITION,
    errors.FLOW_REPEAT_INVALID: FLOW_DEFINITION,
    errors.FLOW_FILE_NOT_FOUND: FLOW_DEFINITION,
    errors.FLOW_PATH_ESCAPES_WORKSPACE: FLOW_DEFINITION,
    errors.FLOW_CYCLE_DETECTED: FLOW_DEFINITION,
    errors.FLOW_CHECK_FAILED: FLOW_DEFINITION,
    errors.FLOW_REPLAY_STEP_NOT_REACHED: FLOW_DEFINITION,
    errors.UNSUPPORTED_ON_PLATFORM: FLOW_DEFINITION,
    errors.UNSUPPORTED_KEY_FOR_PLATFORM: FLOW_DEFINITION,
}


def failure_class(code: str) -> str:
    """Classify an error code; unknown codes never blame the app."""
    return _FAILURE_CLASS_BY_CODE.get(code, INFRASTRUCTURE)


# --- Selector surface --------------------------------------------------------
# flow field name -> selector.py key
SELECTOR_STRING_FIELDS = {
    "id": "resource_id",
    "text": "text",
    # the label a user/screen reader sees, wherever the platform stores it
    # (Android `text`, Flutter/iOS accessibility label) — one cross-platform
    # field, and the exact equivalent of Maestro's `text`
    "visibleText": "visible_text",
    "description": "desc",
    "role": "role",
}
SELECTOR_BOOL_FIELDS = ("enabled", "checked", "selected", "focused")
# flow relational name -> engine relation key. The geometric four need a
# unique anchor; childOf walks ancestors, containsChild checks direct children.
SELECTOR_RELATIONAL_FIELDS = {
    "above": "above",
    "below": "below",
    "leftOf": "left_of",
    "rightOf": "right_of",
    "childOf": "child_of",
    "containsChild": "contains_child",
    "containsDescendants": "contains_descendants",
}
# Recognized-but-deferred fields are rejected with a pointed hint so demand is
# measurable and nothing is silently ignored.
SELECTOR_DEFERRED_FIELDS = {
    "point": "raw coordinates are not a flow selector; use 'autonom ui tap --x --y' for one-off taps",
    "bounds": "bounds are diagnostic output, not a selector",
}
# flow match mode -> (selector.py mode, case_sensitive)
_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

MATCH_MODES = {
    "exact": ("exact", True),
    "caseInsensitiveExact": ("exact", False),
    "contains": ("contains", True),
    "regex": ("regex", True),
}
DEFAULT_MATCH = "exact"

# --- Command registry --------------------------------------------------------


@dataclass(frozen=True)
class ArgSpec:
    name: str
    kind: str  # str | bool | int | float | selector | env | when | commands
    required: bool = False
    choices: tuple = ()


@dataclass(frozen=True)
class CommandSpec:
    name: str
    mutating: bool
    args: tuple = ()
    bare: bool = False           # `- name` with no arguments is legal
    shorthand: str | None = None  # arg that `- name: scalar` binds; "selector.text" nests
    assertion: bool = False       # optional: forbidden; polls until timeout
    optional_allowed: bool = False
    since: str = "0.20.1"         # slice whose executor first runs it


_LABEL = ArgSpec("label", "str")
_TIMEOUT = ArgSpec("timeoutMs", "int")
_POSTCONDITION = ArgSpec("postcondition", "selector")
_OPTIONAL = (ArgSpec("optional", "bool"), ArgSpec("reason", "str"))

REGISTRY: dict[str, CommandSpec] = {spec.name: spec for spec in [
    # lifecycle
    CommandSpec("launchApp", True, bare=True,
                args=(ArgSpec("clearState", "bool"), _LABEL, _POSTCONDITION)),
    CommandSpec("stopApp", True, bare=True, args=(_LABEL, _POSTCONDITION)),
    CommandSpec("clearState", True, bare=True, args=(_LABEL, _POSTCONDITION)),
    CommandSpec("openLink", True, shorthand="url",
                args=(ArgSpec("url", "str", required=True), _LABEL, _POSTCONDITION)),
    # UI actions
    CommandSpec("tapOn", True, shorthand="selector.text", optional_allowed=True,
                args=(ArgSpec("selector", "selector", required=True),
                      ArgSpec("repeat", "int"), ArgSpec("delayMs", "int"),
                      _LABEL, _TIMEOUT, _POSTCONDITION) + _OPTIONAL),
    CommandSpec("longPressOn", True, shorthand="selector.text",
                optional_allowed=True, since="0.21.0",
                args=(ArgSpec("selector", "selector", required=True),
                      ArgSpec("durationMs", "int"),
                      _LABEL, _TIMEOUT, _POSTCONDITION) + _OPTIONAL),
    CommandSpec("doubleTapOn", True, shorthand="selector.text",
                optional_allowed=True, since="0.21.0",
                args=(ArgSpec("selector", "selector", required=True),
                      _LABEL, _TIMEOUT, _POSTCONDITION) + _OPTIONAL),
    CommandSpec("inputText", True, shorthand="value",
                args=(ArgSpec("value", "str", required=True),
                      ArgSpec("sensitive", "bool"), _LABEL, _POSTCONDITION)),
    CommandSpec("eraseText", True, bare=True,
                args=(ArgSpec("chars", "int"), _LABEL, _POSTCONDITION)),
    CommandSpec("pressKey", True, shorthand="key",
                args=(ArgSpec("key", "str", required=True), _LABEL, _POSTCONDITION)),
    CommandSpec("back", True, bare=True, args=(_LABEL, _POSTCONDITION)),
    CommandSpec("swipe", True, shorthand="direction",
                args=(ArgSpec("direction", "str", required=True,
                              choices=("up", "down", "left", "right")),
                      ArgSpec("from", "selector"),
                      ArgSpec("durationMs", "int"), _LABEL, _POSTCONDITION)),
    CommandSpec("scroll", True, bare=True, since="0.28.1",
                args=(_LABEL, _POSTCONDITION)),
    CommandSpec("scrollUntilVisible", True, since="0.20.2",
                args=(ArgSpec("selector", "selector", required=True),
                      ArgSpec("direction", "str",
                              choices=("up", "down", "left", "right")),
                      ArgSpec("maxSwipes", "int"),
                      ArgSpec("centerElement", "bool"), _LABEL, _POSTCONDITION)),
    CommandSpec("copyTextFrom", False, shorthand="selector.text", since="0.28.1",
                args=(ArgSpec("selector", "selector", required=True),
                      ArgSpec("into", "str"), ArgSpec("sensitive", "bool"),
                      _TIMEOUT, _LABEL)),
    CommandSpec("setClipboard", False, shorthand="value", since="0.28.1",
                args=(ArgSpec("value", "str", required=True),
                      ArgSpec("into", "str"), ArgSpec("sensitive", "bool"),
                      _LABEL)),
    CommandSpec("pasteText", True, bare=True, since="0.28.1",
                args=(_LABEL, _POSTCONDITION)),
    # assertions and waits
    CommandSpec("assertVisible", False, shorthand="selector.text", assertion=True,
                args=(ArgSpec("selector", "selector", required=True),
                      _TIMEOUT, _LABEL)),
    CommandSpec("assertNotVisible", False, shorthand="selector.text", assertion=True,
                args=(ArgSpec("selector", "selector", required=True),
                      _TIMEOUT, _LABEL)),
    CommandSpec("assertEnabled", False, assertion=True, since="0.20.2",
                args=(ArgSpec("selector", "selector", required=True),
                      _TIMEOUT, _LABEL)),
    CommandSpec("assertChecked", False, assertion=True, since="0.20.2",
                args=(ArgSpec("selector", "selector", required=True),
                      _TIMEOUT, _LABEL)),
    CommandSpec("waitUntil", False, assertion=True,
                args=(ArgSpec("visible", "selector"),
                      ArgSpec("notVisible", "selector"),
                      ArgSpec("timeoutMs", "int", required=True), _LABEL)),
    # device state
    CommandSpec("setLocation", True, since="0.20.2",
                args=(ArgSpec("latitude", "float", required=True),
                      ArgSpec("longitude", "float", required=True), _LABEL,
                      _POSTCONDITION)),
    CommandSpec("setPermissions", True, since="0.20.2",
                args=(ArgSpec("action", "str", required=True,
                              choices=("grant", "revoke", "reset")),
                      ArgSpec("service", "str", required=True),
                      ArgSpec("appId", "str"), _LABEL, _POSTCONDITION)),
    CommandSpec("addMedia", True, shorthand="path", since="0.20.2",
                args=(ArgSpec("path", "str", required=True), _LABEL,
                      _POSTCONDITION)),
    CommandSpec("setOrientation", True, shorthand="orientation", since="0.21.0",
                args=(ArgSpec("orientation", "str", required=True,
                              choices=("portrait", "landscape",
                                       "portrait-reversed", "landscape-reversed")),
                      _LABEL, _POSTCONDITION)),
    # composition
    CommandSpec("runFlow", True, shorthand="file", since="0.20.2",
                args=(ArgSpec("file", "str"),
                      ArgSpec("commands", "commands"),
                      ArgSpec("env", "env"), ArgSpec("when", "when"), _LABEL)),
    CommandSpec("repeat", False, since="0.28.1",
                args=(ArgSpec("commands", "commands", required=True),
                      ArgSpec("times", "int", required=True),
                      ArgSpec("while", "when"), _LABEL)),
    CommandSpec("retry", False, since="0.21.0",
                args=(ArgSpec("commands", "commands", required=True),
                      ArgSpec("maxAttempts", "int"),
                      ArgSpec("onlyOn", "strlist"),
                      ArgSpec("allowMutations", "bool"), _LABEL)),
    CommandSpec("group", False, since="0.21.0",
                args=(ArgSpec("commands", "commands", required=True),
                      ArgSpec("label", "str", required=True))),
    # evidence
    CommandSpec("takeScreenshot", False, bare=True, shorthand="label",
                args=(_LABEL,)),
    CommandSpec("checkpoint", False, shorthand="name",
                args=(ArgSpec("name", "str", required=True),)),
    CommandSpec("note", False, shorthand="text",
                args=(ArgSpec("text", "str", required=True),)),
]}

# Known names we deliberately do not run, each with a pointed hint. An unknown
# command is never ignored (research doc §17, Phase 1 exit criterion).
DEFERRED_COMMANDS = {
    "waitForIdle": "no idle signal exists on either backend; use waitUntil with an explicit timeoutMs",
    "extendedWaitUntil": "use waitUntil with an explicit timeoutMs",
    "runScript": "Flow v1 has no script engine, by design; run scripts outside the flow",
    "evalScript": "Flow v1 has no script engine, by design",
}

# --- Header surface ----------------------------------------------------------
HEADER_FIELDS = (
    "schema", "appId", "name", "id", "description", "tags", "properties",
    "env", "requires", "sideEffects", "setup", "evidence",
    "onFlowStart", "onFlowComplete",
)
EVIDENCE_MODES = ("minimal", "on-failure", "always", "custom")
EVIDENCE_KINDS = ("screenshot", "hierarchy", "logs", "crashes", "network")
EVIDENCE_BODIES = ("preview", "full")
PLATFORMS = ("android", "ios")
# Frozen vocabulary (research §7.5). Unknown names are a header error;
# the executor checks these against the resolved session before mutating.
KNOWN_CAPABILITIES = SEMANTIC_CAPABILITIES
SIDE_EFFECTS = (
    "none", "app-data", "device-state", "network", "external-system",
    "credentials", "media", "clipboard",
)
SETUP_FIELDS = (
    "profile", "fixtures", "mocks", "permissions", "location",
    "orientation", "appearance", "locale", "network", "reset",
)

# --- Typed model -------------------------------------------------------------


@dataclass
class FlowSelector:
    fields: dict = field(default_factory=dict)  # selector.py keys -> str|bool
    match: str = DEFAULT_MATCH
    index: int | None = None
    line: int = 0
    col: int = 0
    # flow-facing field names in source order, for canonical output
    source_fields: dict = field(default_factory=dict)
    # engine relation key -> {"fields", "mode", "case_sensitive"} anchor spec
    relations: dict = field(default_factory=dict)
    # flow-facing relational name -> nested FlowSelector, for canonical output
    source_relations: dict = field(default_factory=dict)


@dataclass
class WhenClause:
    platform: str | None = None
    visible: FlowSelector | None = None
    not_visible: FlowSelector | None = None
    env_equals: dict = field(default_factory=dict)
    line: int = 0
    col: int = 0


@dataclass
class Step:
    command: str
    args: dict = field(default_factory=dict)
    line: int = 0
    col: int = 0

    @property
    def spec(self) -> CommandSpec:
        return REGISTRY[self.command]

    @property
    def label(self) -> str | None:
        return self.args.get("label")

    @property
    def selector(self) -> FlowSelector | None:
        value = self.args.get("selector")
        return value if isinstance(value, FlowSelector) else None


@dataclass
class Evidence:
    mode: str = "on-failure"
    before_mutation: bool = False
    after_assertion: bool = False
    collect: list = field(default_factory=lambda: ["screenshot", "hierarchy"])
    bodies: str = "preview"
    # field names the author actually wrote, so canonical output does not
    # materialize defaults the file never mentioned
    explicit: list = field(default_factory=list)


@dataclass
class Flow:
    path: str
    name: str = ""
    app_id: str | None = None
    flow_id: str | None = None
    description: str | None = None
    tags: list = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    requires_platforms: list = field(default_factory=list)
    requires_capabilities: list = field(default_factory=list)
    side_effects: list = field(default_factory=list)
    setup: dict = field(default_factory=dict)
    evidence: Evidence | None = None
    on_flow_start: list = field(default_factory=list)
    on_flow_complete: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    # set by the loader when the source file was a Maestro document that was
    # converted on the fly (never part of the flow's own header)
    converted_from: str | None = None


# --- Coercion helpers --------------------------------------------------------


def _fail(code: str, message: str, path: str, line: int, col: int,
          hint: str | None = None, **extra) -> None:
    raise errors.AutonomError(
        code, f"{path}:{line}:{col}: {message}",
        hint=hint, file=path, line=line, column=col, **extra,
    )


def _coerce(scalar, kind: str, code: str, path: str, what: str):
    if not isinstance(scalar, Scalar):
        _fail(code, f"{what} must be a single value",
              path, getattr(scalar, "line", 0), getattr(scalar, "col", 0))
    if kind == "str":
        return scalar.text
    if kind == "bool":
        if scalar.style == "plain" and scalar.text in ("true", "false"):
            return scalar.text == "true"
        quoted = "" if scalar.style == "plain" else " (quoted strings are not booleans)"
        _fail(code, f"{what} must be true or false{quoted}, got {scalar.text!r}",
              path, scalar.line, scalar.col)
    if kind == "int":
        if scalar.style == "plain" and re.fullmatch(r"-?[0-9]+", scalar.text):
            return int(scalar.text)
        _fail(code, f"{what} must be an integer, got {scalar.text!r}",
              path, scalar.line, scalar.col)
    if kind == "float":
        if scalar.style == "plain" and re.fullmatch(r"-?[0-9]+(\.[0-9]+)?",
                                                    scalar.text):
            return float(scalar.text)
        _fail(code, f"{what} must be a number, got {scalar.text!r}",
              path, scalar.line, scalar.col)
    raise AssertionError(f"unhandled kind {kind}")


def _string_list(node, code: str, path: str, what: str) -> list:
    if isinstance(node, Scalar):
        _fail(code, f"{what} must be a list", path, node.line, node.col)
    if isinstance(node, Mapping):
        _fail(code, f"{what} must be a list, not a mapping", path, node.line, node.col)
    out = []
    for item in node.items:
        if not isinstance(item, Scalar):
            _fail(code, f"{what} items must be strings", path, node.line, node.col)
        out.append(item.text)
    return out


def _string_map(node, code: str, path: str, what: str) -> dict:
    if not isinstance(node, Mapping):
        _fail(code, f"{what} must be a mapping",
              path, getattr(node, "line", 0), getattr(node, "col", 0))
    out = {}
    for key, value in node.pairs:
        if not isinstance(value, Scalar):
            _fail(code, f"{what}.{key.text} must be a single value",
                  path, key.line, key.col)
        out[key.text] = value.text
    return out


def _literal(node, code: str, path: str, what: str):
    """Convert the YAML subset AST into a JSON-compatible setup value."""
    if isinstance(node, Scalar):
        if node.style == "plain" and node.text in ("true", "false"):
            return node.text == "true"
        if node.style == "plain" and re.fullmatch(r"-?[0-9]+", node.text):
            return int(node.text)
        if node.style == "plain" and re.fullmatch(r"-?[0-9]+\.[0-9]+", node.text):
            return float(node.text)
        return node.text
    if isinstance(node, Sequence):
        return [_literal(item, code, path, what) for item in node.items]
    if isinstance(node, Mapping):
        result = {}
        for key, value in node.pairs:
            if key.text in result:
                _fail(code, f"duplicate {what} field {key.text!r}",
                      path, key.line, key.col)
            result[key.text] = _literal(value, code, path,
                                        f"{what}.{key.text}")
        return result
    _fail(code, f"{what} has an unsupported value", path,
          getattr(node, "line", 0), getattr(node, "col", 0))


def _build_setup(node, path: str) -> dict:
    code = errors.FLOW_HEADER_INVALID
    if not isinstance(node, Mapping):
        _fail(code, "setup must be a mapping", path,
              getattr(node, "line", 0), getattr(node, "col", 0))
    result = {}
    for key, value in node.pairs:
        if key.text not in SETUP_FIELDS:
            _fail(code, f"unknown setup field {key.text!r}",
                  path, key.line, key.col,
                  hint="Setup fields: " + ", ".join(SETUP_FIELDS) + ".")
        result[key.text] = _literal(value, code, path, f"setup.{key.text}")
    return result


# --- Selector ----------------------------------------------------------------


def build_selector(node, path: str, *, _anchor: bool = False) -> FlowSelector:
    code = errors.FLOW_SELECTOR_INVALID
    if isinstance(node, Scalar):
        # shorthand: bare text with exact match
        return FlowSelector(fields={"text": node.text}, match=DEFAULT_MATCH,
                            line=node.line, col=node.col,
                            source_fields={"text": node.text})
    if not isinstance(node, Mapping):
        _fail(code, "selector must be a mapping or a text shorthand",
              path, getattr(node, "line", 0), getattr(node, "col", 0))
    selector = FlowSelector(line=node.line, col=node.col)
    for key, value in node.pairs:
        name = key.text
        if name in SELECTOR_STRING_FIELDS:
            text = _coerce(value, "str", code, path, f"selector.{name}")
            selector.fields[SELECTOR_STRING_FIELDS[name]] = text
            selector.source_fields[name] = text
        elif name in SELECTOR_BOOL_FIELDS:
            flag = _coerce(value, "bool", code, path, f"selector.{name}")
            selector.fields[name] = flag
            selector.source_fields[name] = flag
        elif name == "match":
            mode = _coerce(value, "str", code, path, "selector.match")
            if mode not in MATCH_MODES:
                _fail(code, f"unknown match mode {mode!r}", path, value.line, value.col,
                      hint="Match modes: " + ", ".join(sorted(MATCH_MODES)) + ".")
            selector.match = mode
        elif name == "index":
            if _anchor:
                _fail(code, "a relational anchor cannot carry index",
                      path, key.line, key.col,
                      hint="Anchors must identify their element by fields alone.")
            selector.index = _coerce(value, "int", code, path, "selector.index")
        elif name in SELECTOR_RELATIONAL_FIELDS:
            if _anchor:
                _fail(code, "relational anchors cannot nest further relations",
                      path, key.line, key.col)
            anchor = build_selector(value, path, _anchor=True)
            mode, case_sensitive = MATCH_MODES[anchor.match]
            selector.relations[SELECTOR_RELATIONAL_FIELDS[name]] = {
                "fields": anchor.fields,
                "mode": mode,
                "case_sensitive": case_sensitive,
            }
            selector.source_relations[name] = anchor
        elif name in SELECTOR_DEFERRED_FIELDS:
            _fail(code, f"selector field {name!r} is not supported in Flow v1",
                  path, key.line, key.col, hint=SELECTOR_DEFERRED_FIELDS[name])
        else:
            _fail(code, f"unknown selector field {name!r}", path, key.line, key.col,
                  hint="Fields: " + ", ".join(list(SELECTOR_STRING_FIELDS)
                                              + list(SELECTOR_BOOL_FIELDS)
                                              + list(SELECTOR_RELATIONAL_FIELDS)
                                              + ["match", "index"]) + ".")
    has_strings = bool(set(selector.fields) & set(SELECTOR_STRING_FIELDS.values()))
    if not has_strings and not selector.relations:
        what = "anchor" if _anchor else "selector"
        _fail(code, f"{what} needs at least one of id, text, description, role"
                    + ("" if _anchor else " (or a relational constraint)"),
              path, node.line, node.col,
              hint="State fields and index alone would match too broadly.")
    if _anchor and not has_strings:
        _fail(code, "anchor needs at least one of id, text, description, role",
              path, node.line, node.col)
    return selector


# --- When clause -------------------------------------------------------------


def build_when(node, path: str) -> WhenClause:
    code = errors.FLOW_COMMAND_INVALID
    if not isinstance(node, Mapping):
        _fail(code, "when must be a mapping",
              path, getattr(node, "line", 0), getattr(node, "col", 0))
    when = WhenClause(line=node.line, col=node.col)
    for key, value in node.pairs:
        name = key.text
        if name == "platform":
            platform = _coerce(value, "str", code, path, "when.platform")
            if platform not in PLATFORMS:
                _fail(code, f"when.platform must be one of {'/'.join(PLATFORMS)}",
                      path, value.line, value.col)
            when.platform = platform
        elif name == "visible":
            when.visible = build_selector(value, path)
        elif name == "notVisible":
            when.not_visible = build_selector(value, path)
        elif name == "envEquals":
            when.env_equals = _string_map(value, code, path, "when.envEquals")
        else:
            _fail(code, f"unknown when condition {name!r}", path, key.line, key.col,
                  hint="Conditions: platform, visible, notVisible, envEquals "
                       "(AND semantics).")
    return when


# --- Steps -------------------------------------------------------------------


def _unknown_command(name: str, path: str, line: int, col: int) -> None:
    if name in DEFERRED_COMMANDS:
        _fail(errors.FLOW_UNKNOWN_COMMAND,
              f"command {name!r} is not part of Flow v1", path, line, col,
              hint=DEFERRED_COMMANDS[name])
    _fail(errors.FLOW_UNKNOWN_COMMAND, f"unknown command {name!r}", path, line, col,
          hint="Commands: " + ", ".join(sorted(REGISTRY)) + ".")


def _apply_shorthand(spec: CommandSpec, scalar: Scalar, path: str) -> dict:
    if spec.shorthand is None:
        _fail(errors.FLOW_COMMAND_INVALID,
              f"{spec.name} takes a mapping of arguments, not a single value",
              path, scalar.line, scalar.col)
    if spec.shorthand == "selector.text":
        return {"selector": FlowSelector(fields={"text": scalar.text},
                                         line=scalar.line, col=scalar.col,
                                         source_fields={"text": scalar.text})}
    return {spec.shorthand: scalar.text}


def build_step(item, path: str) -> Step:
    code = errors.FLOW_COMMAND_INVALID
    if isinstance(item, Scalar):
        name = item.text
        if name not in REGISTRY:
            _unknown_command(name, path, item.line, item.col)
        spec = REGISTRY[name]
        if not spec.bare:
            required = [a.name for a in spec.args if a.required]
            _fail(code, f"{name} needs arguments ({', '.join(required)})",
                  path, item.line, item.col)
        return Step(name, {}, item.line, item.col)
    if isinstance(item, Sequence):
        _fail(code, "a command cannot be a list", path, item.line, item.col)
    if len(item.pairs) != 1:
        _fail(code, "one command per '-' item",
              path, item.line, item.col,
              hint="Split extra keys into their own '- command:' items — or, if "
                   "they are arguments, indent them under the command key.")
    key, value = item.pairs[0]
    name = key.text
    if name not in REGISTRY:
        _unknown_command(name, path, key.line, key.col)
    spec = REGISTRY[name]

    if isinstance(value, Scalar):
        args = _apply_shorthand(spec, value, path)
        return _finish_step(spec, args, key, path)
    if isinstance(value, Sequence):
        _fail(code, f"{name} arguments must be a mapping", path, value.line, value.col)

    args: dict = {}
    by_name = {a.name: a for a in spec.args}
    for arg_key, arg_value in value.pairs:
        arg_name = arg_key.text
        arg = by_name.get(arg_name)
        if arg is None:
            _fail(code, f"unknown argument {arg_name!r} for {name}",
                  path, arg_key.line, arg_key.col,
                  hint="Arguments: " + ", ".join(a.name for a in spec.args) + ".")
        if arg.kind == "selector":
            args[arg_name] = build_selector(arg_value, path)
        elif arg.kind == "env":
            args[arg_name] = _string_map(arg_value, code, path, f"{name}.env")
        elif arg.kind == "when":
            args[arg_name] = build_when(arg_value, path)
        elif arg.kind == "commands":
            if not isinstance(arg_value, Sequence):
                _fail(code, f"{name}.commands must be a sequence of commands",
                      path, arg_key.line, arg_key.col)
            args[arg_name] = [build_step(item, path) for item in arg_value.items]
        elif arg.kind == "strlist":
            args[arg_name] = _string_list(arg_value, code, path,
                                          f"{name}.{arg_name}")
        else:
            coerced = _coerce(arg_value, arg.kind, code, path, f"{name}.{arg_name}")
            if arg.choices and coerced not in arg.choices:
                _fail(code, f"{name}.{arg_name} must be one of "
                            f"{', '.join(arg.choices)}",
                      path, arg_value.line, arg_value.col)
            args[arg_name] = coerced
    return _finish_step(spec, args, key, path)


def _finish_step(spec: CommandSpec, args: dict, key: Scalar, path: str) -> Step:
    code = errors.FLOW_COMMAND_INVALID
    for arg in spec.args:
        if arg.required and arg.name not in args:
            _fail(code, f"{spec.name} requires {arg.name!r}", path, key.line, key.col)
        if arg.choices and arg.name in args and args[arg.name] not in arg.choices:
            # backstop for the shorthand path, which bypasses per-arg parsing
            _fail(code, f"{spec.name}.{arg.name} must be one of {', '.join(arg.choices)}",
                  path, key.line, key.col)
    if spec.name == "waitUntil":
        present = [n for n in ("visible", "notVisible") if n in args]
        if len(present) != 1:
            _fail(code, "waitUntil takes exactly one of visible or notVisible",
                  path, key.line, key.col)
    if args.get("optional"):
        if spec.assertion:
            _fail(errors.FLOW_OPTIONAL_ASSERTION_FORBIDDEN,
                  "an assertion cannot be optional", path, key.line, key.col,
                  hint="If the state genuinely may not occur, the flow should "
                       "branch into separate flows instead.")
        if not spec.optional_allowed:
            _fail(code, f"optional is not supported on {spec.name} in v1",
                  path, key.line, key.col)
        if not args.get("reason"):
            _fail(errors.FLOW_OPTIONAL_ASSERTION_FORBIDDEN,
                  "optional steps must state a reason", path, key.line, key.col,
                  hint="reason: documents why skipping this step cannot hide "
                       "a real failure.")
    if "when" in args and spec.name != "runFlow":
        _fail(code, "when is only supported on runFlow in v1",
              path, key.line, key.col)
    if spec.name == "runFlow":
        present = [n for n in ("file", "commands") if n in args]
        if len(present) != 1:
            _fail(code, "runFlow takes exactly one of file or commands",
                  path, key.line, key.col,
                  hint="Reference a subflow file, or inline the commands.")
        if "commands" in args and not args["commands"]:
            _fail(code, "runFlow.commands is empty", path, key.line, key.col)
    if spec.name == "tapOn":
        taps = args.get("repeat")
        if taps is not None and not 2 <= taps <= 10:
            _fail(code, "tapOn.repeat must be between 2 and 10",
                  path, key.line, key.col,
                  hint="A single tap needs no repeat; more than 10 declared "
                       "taps is a loop in disguise — use repeat.")
        if "delayMs" in args and taps is None:
            _fail(code, "tapOn.delayMs needs repeat", path, key.line, key.col)
        if args.get("delayMs", 0) < 0:
            _fail(code, "tapOn.delayMs cannot be negative",
                  path, key.line, key.col)
    if spec.name == "repeat":
        times = args["times"]
        if not 1 <= times <= 25:
            _fail(errors.FLOW_REPEAT_INVALID,
                  "repeat.times must be between 1 and 25",
                  path, key.line, key.col,
                  hint="repeat is bounded, declared iteration — an unbounded "
                       "loop is not part of Flow v1.")
        clause = args.get("while")
        if clause is not None and (clause.platform or clause.env_equals):
            _fail(errors.FLOW_REPEAT_INVALID,
                  "repeat.while supports visible/notVisible only",
                  path, key.line, key.col,
                  hint="Platform and env conditions do not change between "
                       "iterations; put them on a runFlow when: instead.")
        for sub in args["commands"]:
            if sub.command in ("repeat", "retry", "group", "runFlow"):
                _fail(errors.FLOW_REPEAT_INVALID,
                      f"repeat cannot contain {sub.command} — repeated blocks "
                      "stay small and atomic",
                      path, sub.line, sub.col)
        if not args["commands"]:
            _fail(errors.FLOW_REPEAT_INVALID, "repeat.commands is empty",
                  path, key.line, key.col)
    if spec.name in ("copyTextFrom", "setClipboard"):
        into = args.get("into")
        if into is not None and not _VAR_NAME_RE.match(into):
            _fail(errors.FLOW_VAR_CONFLICT,
                  f"variable name {into!r} must match [A-Za-z_][A-Za-z0-9_]*",
                  path, key.line, key.col)
    if spec.name == "retry":
        attempts = args.setdefault("maxAttempts", 2)
        if not 1 <= attempts <= 3:
            _fail(code, "retry.maxAttempts must be between 1 and 3",
                  path, key.line, key.col,
                  hint="Retrying a large block hides defects; keep it tight.")
        for sub in args["commands"]:
            if sub.command in ("retry", "group", "runFlow", "repeat"):
                _fail(code, f"retry cannot contain {sub.command} — retried "
                            "blocks stay small and atomic",
                      path, sub.line, sub.col)
            if REGISTRY[sub.command].mutating and not args.get("allowMutations"):
                _fail(code, f"retry contains the mutating command "
                            f"{sub.command}; repeating mutations needs an "
                            "explicit allowMutations: true",
                      path, sub.line, sub.col,
                      hint="A repeated tap or input can act twice on the app. "
                           "Say allowMutations: true only when that is safe.")
        if not args["commands"]:
            _fail(code, "retry.commands is empty", path, key.line, key.col)
    if spec.name == "group":
        for sub in args["commands"]:
            if sub.command == "group":
                _fail(code, "groups do not nest — use runFlow for structure",
                      path, sub.line, sub.col)
        if not args["commands"]:
            _fail(code, "group.commands is empty", path, key.line, key.col)
    return Step(spec.name, args, key.line, key.col)


# --- Header ------------------------------------------------------------------


def _build_evidence(node, path: str) -> Evidence:
    code = errors.FLOW_HEADER_INVALID
    if not isinstance(node, Mapping):
        _fail(code, "evidence must be a mapping",
              path, getattr(node, "line", 0), getattr(node, "col", 0))
    evidence = Evidence()
    for key, value in node.pairs:
        name = key.text
        evidence.explicit.append(name)
        if name == "mode":
            mode = _coerce(value, "str", code, path, "evidence.mode")
            if mode not in EVIDENCE_MODES:
                _fail(code, f"evidence.mode {mode!r} must be one of "
                            f"{', '.join(EVIDENCE_MODES)}",
                      path, value.line, value.col)
            evidence.mode = mode
        elif name == "beforeMutation":
            evidence.before_mutation = _coerce(value, "bool", code, path,
                                               "evidence.beforeMutation")
        elif name == "afterAssertion":
            evidence.after_assertion = _coerce(value, "bool", code, path,
                                               "evidence.afterAssertion")
        elif name == "collect":
            kinds = _string_list(value, code, path, "evidence.collect")
            for kind in kinds:
                if kind not in EVIDENCE_KINDS:
                    _fail(code, f"unknown evidence kind {kind!r}",
                          path, value.line, value.col,
                          hint="Kinds: " + ", ".join(EVIDENCE_KINDS) + ".")
            evidence.collect = kinds
        elif name == "bodies":
            bodies = _coerce(value, "str", code, path, "evidence.bodies")
            if bodies not in EVIDENCE_BODIES:
                _fail(code, f"evidence.bodies {bodies!r} must be one of "
                            f"{', '.join(EVIDENCE_BODIES)}",
                      path, value.line, value.col)
            evidence.bodies = bodies
        else:
            _fail(code, f"unknown evidence field {name!r}", path, key.line, key.col)
    return evidence


def _build_requires(node, path: str, flow: Flow) -> None:
    code = errors.FLOW_HEADER_INVALID
    if not isinstance(node, Mapping):
        _fail(code, "requires must be a mapping",
              path, getattr(node, "line", 0), getattr(node, "col", 0))
    for key, value in node.pairs:
        if key.text == "platform":
            platforms = _string_list(value, code, path, "requires.platform")
            for platform in platforms:
                if platform not in PLATFORMS:
                    _fail(code, f"unknown platform {platform!r}",
                          path, value.line, value.col,
                          hint="Platforms: android, ios.")
            flow.requires_platforms = platforms
        elif key.text == "capabilities":
            names = _string_list(
                value, code, path, "requires.capabilities")
            for capability in names:
                if capability not in KNOWN_CAPABILITIES:
                    _fail(code, f"unknown capability {capability!r}",
                          path, value.line, value.col,
                          hint="Capabilities: " + ", ".join(KNOWN_CAPABILITIES)
                          + ".")
            flow.requires_capabilities = names
        else:
            _fail(code, f"unknown requires field {key.text!r}",
                  path, key.line, key.col)


def _build_hook(node, path: str) -> list:
    if not isinstance(node, Sequence):
        _fail(errors.FLOW_HEADER_INVALID, "hooks must be a sequence of commands",
              path, getattr(node, "line", 0), getattr(node, "col", 0))
    return [build_step(item, path) for item in node.items]


def build_flow(document: FlowDocument) -> Flow:
    path = document.path
    code = errors.FLOW_HEADER_INVALID
    header = document.header
    flow = Flow(path=path)

    schema_node = header.get("schema")
    if schema_node is None:
        _fail(errors.FLOW_SCHEMA_UNSUPPORTED,
              f"missing 'schema: {FLOW_SCHEMA_ID}' header field",
              path, header.line or 1, header.col or 1)
    if not isinstance(schema_node, Scalar) or schema_node.text != FLOW_SCHEMA_ID:
        line = getattr(schema_node, "line", 1)
        col = getattr(schema_node, "col", 1)
        got = getattr(schema_node, "text", "<non-scalar>")
        _fail(errors.FLOW_SCHEMA_UNSUPPORTED,
              f"unsupported flow schema {got!r} (this build supports {FLOW_SCHEMA_ID})",
              path, line, col)

    for key, value in header.pairs:
        name = key.text
        if name == "schema":
            continue
        if name not in HEADER_FIELDS:
            _fail(code, f"unknown header field {name!r}", path, key.line, key.col,
                  hint="Header fields: " + ", ".join(HEADER_FIELDS) + ".")
        if name == "appId":
            flow.app_id = _coerce(value, "str", code, path, "appId")
        elif name == "name":
            flow.name = _coerce(value, "str", code, path, "name")
        elif name == "id":
            flow.flow_id = _coerce(value, "str", code, path, "id")
        elif name == "description":
            flow.description = _coerce(value, "str", code, path, "description")
        elif name == "tags":
            flow.tags = _string_list(value, code, path, "tags")
        elif name == "properties":
            flow.properties = _string_map(value, code, path, "properties")
        elif name == "env":
            env = _string_map(value, code, path, "env")
            for var in env:
                if not var.replace("_", "a").isalnum() or var[0].isdigit():
                    _fail(code, f"env name {var!r} must match [A-Za-z_][A-Za-z0-9_]*",
                          path, value.line, value.col)
            flow.env = env
        elif name == "requires":
            _build_requires(value, path, flow)
        elif name == "sideEffects":
            effects = _string_list(value, code, path, "sideEffects")
            unknown = [item for item in effects if item not in SIDE_EFFECTS]
            if unknown:
                _fail(code, f"unknown side effect {unknown[0]!r}",
                      path, value.line, value.col,
                      hint="Side effects: " + ", ".join(SIDE_EFFECTS) + ".")
            if "none" in effects and len(effects) > 1:
                _fail(code, "sideEffects 'none' cannot be combined with other values",
                      path, value.line, value.col)
            flow.side_effects = effects
        elif name == "setup":
            flow.setup = _build_setup(value, path)
        elif name == "evidence":
            flow.evidence = _build_evidence(value, path)
        elif name == "onFlowStart":
            flow.on_flow_start = _build_hook(value, path)
        elif name == "onFlowComplete":
            flow.on_flow_complete = _build_hook(value, path)

    if not flow.name:
        _fail(code, "missing required header field 'name'",
              path, header.line or 1, header.col or 1)

    if not document.commands.items:
        _fail(errors.FLOW_COMMAND_INVALID, "flow has no commands",
              path, document.separator_line, 1)
    flow.steps = [build_step(item, path) for item in document.commands.items]
    return flow
