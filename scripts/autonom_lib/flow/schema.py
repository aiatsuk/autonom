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

from dataclasses import dataclass, field

from . import FLOW_SCHEMA_ID
from .. import errors
from .parser import FlowDocument, Mapping, Scalar, Sequence

# --- Failure classes ---------------------------------------------------------
TEST_FAILURE = "test_failure"
FLOW_DEFINITION = "flow_definition"
INFRASTRUCTURE = "infrastructure"

_FAILURE_CLASS_BY_CODE = {
    # The app under test did not behave as the flow asserts.
    errors.FLOW_ASSERTION_TIMEOUT: TEST_FAILURE,
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
    errors.FLOW_FILE_NOT_FOUND: FLOW_DEFINITION,
    errors.FLOW_PATH_ESCAPES_WORKSPACE: FLOW_DEFINITION,
    errors.FLOW_CYCLE_DETECTED: FLOW_DEFINITION,
    errors.FLOW_CHECK_FAILED: FLOW_DEFINITION,
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
    "description": "desc",
    "role": "role",
}
SELECTOR_BOOL_FIELDS = ("enabled", "checked", "selected")
# Recognized-but-deferred fields are rejected with a pointed hint so demand is
# measurable and nothing is silently ignored.
SELECTOR_DEFERRED_FIELDS = {
    "above": "relational selectors are planned; tighten with id, role, or index",
    "below": "relational selectors are planned; tighten with id, role, or index",
    "leftOf": "relational selectors are planned; tighten with id, role, or index",
    "rightOf": "relational selectors are planned; tighten with id, role, or index",
    "childOf": "relational selectors are planned; tighten with id, role, or index",
    "containsChild": "relational selectors are planned; tighten with id, role, or index",
    "containsDescendants": "relational selectors are planned; tighten with id, role, or index",
    "focused": "the focused state is not in the compact node schema yet",
    "point": "raw coordinates are not a flow selector; use 'autonom ui tap --x --y' for one-off taps",
    "bounds": "bounds are diagnostic output, not a selector",
}
# flow match mode -> (selector.py mode, case_sensitive)
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
_OPTIONAL = (ArgSpec("optional", "bool"), ArgSpec("reason", "str"))

REGISTRY: dict[str, CommandSpec] = {spec.name: spec for spec in [
    # lifecycle
    CommandSpec("launchApp", True, bare=True,
                args=(ArgSpec("clearState", "bool"), _LABEL)),
    CommandSpec("stopApp", True, bare=True, args=(_LABEL,)),
    CommandSpec("clearState", True, bare=True, args=(_LABEL,)),
    CommandSpec("openLink", True, shorthand="url",
                args=(ArgSpec("url", "str", required=True), _LABEL)),
    # UI actions
    CommandSpec("tapOn", True, shorthand="selector.text", optional_allowed=True,
                args=(ArgSpec("selector", "selector", required=True),
                      _LABEL, _TIMEOUT) + _OPTIONAL),
    CommandSpec("inputText", True, shorthand="value",
                args=(ArgSpec("value", "str", required=True),
                      ArgSpec("sensitive", "bool"), _LABEL)),
    CommandSpec("eraseText", True, bare=True,
                args=(ArgSpec("chars", "int"), _LABEL)),
    CommandSpec("pressKey", True, shorthand="key",
                args=(ArgSpec("key", "str", required=True), _LABEL)),
    CommandSpec("back", True, bare=True, args=(_LABEL,)),
    CommandSpec("swipe", True, shorthand="direction",
                args=(ArgSpec("direction", "str", required=True,
                              choices=("up", "down", "left", "right")),
                      ArgSpec("durationMs", "int"), _LABEL)),
    CommandSpec("scrollUntilVisible", True, since="0.20.2",
                args=(ArgSpec("selector", "selector", required=True),
                      ArgSpec("direction", "str",
                              choices=("up", "down", "left", "right")),
                      ArgSpec("maxSwipes", "int"), _LABEL)),
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
                      ArgSpec("longitude", "float", required=True), _LABEL)),
    CommandSpec("setPermissions", True, since="0.20.2",
                args=(ArgSpec("action", "str", required=True,
                              choices=("grant", "revoke", "reset")),
                      ArgSpec("service", "str", required=True),
                      ArgSpec("appId", "str"), _LABEL)),
    CommandSpec("addMedia", True, shorthand="path", since="0.20.2",
                args=(ArgSpec("path", "str", required=True), _LABEL)),
    # composition
    CommandSpec("runFlow", True, shorthand="file", since="0.20.2",
                args=(ArgSpec("file", "str", required=True),
                      ArgSpec("env", "env"), ArgSpec("when", "when"), _LABEL)),
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
    "longPressOn": "long press needs a duration surface the adapters do not expose yet",
    "doubleTapOn": "double tap needs a duration surface the adapters do not expose yet",
    "waitForIdle": "no idle signal exists on either backend; use waitUntil with an explicit timeoutMs",
    "setOrientation": "orientation control has no device_state substrate yet",
    "retry": "explicit retry blocks are deferred; assertions already poll",
    "group": "grouping is deferred; use runFlow with a subflow file",
    "extendedWaitUntil": "use waitUntil with an explicit timeoutMs",
    "runScript": "Flow v1 has no script engine, by design; run scripts outside the flow",
    "evalScript": "Flow v1 has no script engine, by design",
    "repeat": "unbounded loops are not part of Flow v1",
}

# --- Header surface ----------------------------------------------------------
HEADER_FIELDS = (
    "schema", "appId", "name", "id", "description", "tags", "properties",
    "env", "requires", "evidence", "onFlowStart", "onFlowComplete",
)
EVIDENCE_MODES = ("minimal", "on-failure", "always", "custom")
EVIDENCE_KINDS = ("screenshot", "hierarchy", "logs", "crashes", "network")
EVIDENCE_BODIES = ("preview", "full")
PLATFORMS = ("android", "ios")

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
    evidence: Evidence | None = None
    on_flow_start: list = field(default_factory=list)
    on_flow_complete: list = field(default_factory=list)
    steps: list = field(default_factory=list)


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
        if scalar.style == "plain" and scalar.text.lstrip("-").isdigit():
            return int(scalar.text)
        _fail(code, f"{what} must be an integer, got {scalar.text!r}",
              path, scalar.line, scalar.col)
    if kind == "float":
        text = scalar.text
        stripped = text.lstrip("-")
        parts = stripped.split(".")
        if scalar.style == "plain" and 1 <= len(parts) <= 2 and all(
                p.isdigit() for p in parts) and stripped:
            return float(text)
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


# --- Selector ----------------------------------------------------------------


def build_selector(node, path: str) -> FlowSelector:
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
            selector.index = _coerce(value, "int", code, path, "selector.index")
        elif name in SELECTOR_DEFERRED_FIELDS:
            _fail(code, f"selector field {name!r} is not supported in Flow v1",
                  path, key.line, key.col, hint=SELECTOR_DEFERRED_FIELDS[name])
        else:
            _fail(code, f"unknown selector field {name!r}", path, key.line, key.col,
                  hint="Fields: " + ", ".join(list(SELECTOR_STRING_FIELDS)
                                              + list(SELECTOR_BOOL_FIELDS)
                                              + ["match", "index"]) + ".")
    if not (set(selector.fields) & set(SELECTOR_STRING_FIELDS.values())):
        _fail(code, "selector needs at least one of id, text, description, role",
              path, node.line, node.col,
              hint="State fields and index alone would match too broadly.")
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
            flow.requires_capabilities = _string_list(
                value, code, path, "requires.capabilities")
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
