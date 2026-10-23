"""Flow v1 steps given inline instead of in a file: `autonom ui do` (AL-12).

An agent that knows the next steps sends them in one call instead of one CLI
call per tap. The steps come on stdin in one of two forms:

- a JSON array of step objects, one command key each:
  ``[{"tapOn": {"text": "General"}}, {"assertVisible": {"text": "About"}}]``;
  a command without arguments may be its bare name (``"back"``);
- a YAML step list: the ``- command`` lines of a flow file, without its
  header and its ``---`` line.

Both become one in-memory Flow through the same parser and schema a flow file
goes through (``parser.py`` for YAML, ``schema.build_step`` for every step),
so the commands, their arguments, exact selectors, polling assertions and
single-fire mutations are those of `flow run`. One short form is added in
both syntaxes: the selector fields of a command that takes a ``selector``
may be written directly under the command (``{"tapOn": {"text":
"General"}}``, the form of the AL-12 example and of Maestro); they are moved
under ``selector:`` before the step is built, so they mean exactly what
they mean there (``match: exact`` unless the step says otherwise). JSON
values keep their JSON type: a string is a quoted scalar (``"true"`` is
text, never a boolean; ``"500"`` is not an integer), numbers and booleans
are plain scalars, and ``null`` is refused. The synthetic header is
``name: ui do``; the app id is the session's (``bind_app``). A ``runFlow``
with ``file:`` is refused: the steps are self-contained (``runFlow`` with
inline ``commands:`` is kept).

Every problem with the input is refused before any step runs, as
``invalid_value`` naming the step (its 1-based position in the input), the
command and the problem; the Flow error code it came from is kept as
``flow_error_code`` (and the parser's ``reason`` when it gave one); a YAML
problem also carries ``line`` and ``column``. The executor's own checks that
need the target (an Android-only step on iOS, a ``${NAME}`` nothing defines)
are refused the same way by ``run``, still before any step.
"""
from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterator

from .. import errors
from . import parser as parser_mod
from . import schema as schema_mod
from . import validator as validator_mod
from .parser import Mapping, Scalar, Sequence

# What events, manifests and errors name as the flow's file and name.
INLINE_PATH = "<stdin>"
INLINE_NAME = "ui do"
# As much as a pre-test ticket on stdin: far more than any step list.
MAX_BYTES = 1024 * 1024
FORMAT_JSON = "json"
FORMAT_YAML = "yaml"
# Steps that act on the flow's app, which here is the session's.
APP_COMMANDS = ("launchApp", "stopApp", "clearState")
# Input steps: their `value:` is typed into the app, or set as the clipboard
# that pasteText types. When the step is `sensitive: true` or that value names
# a sensitive variable, the final screen read keeps the value out of what it
# reports and saves.
SENSITIVE_VALUE_COMMANDS = ("inputText", "setClipboard")

HINT = ("Send Flow v1 steps on stdin: a JSON array such as "
        '[{"tapOn":{"text":"General"}},{"assertVisible":{"text":"About"}}], '
        "or the '- command' lines of a flow file without its header "
        "(docs/FLOW.md lists the commands).")
_VAR_HINT = ("ui do takes no --env or --secret: write the value in the step, or "
             "define it earlier in the same steps with copyTextFrom or setClipboard "
             "(into: NAME).")
_PREFIX = re.compile(r"^(?:" + re.escape(INLINE_PATH) + r":\d+:\d+: |step \S+ \(line \d+\): )")
# Every key `selector:` takes, and the commands whose target is a `selector`
# argument: those keys written directly under such a command are its selector.
_SELECTOR_KEYS = frozenset([*schema_mod.SELECTOR_STRING_FIELDS, *schema_mod.SELECTOR_BOOL_FIELDS,
                            *schema_mod.SELECTOR_RELATIONAL_FIELDS,
                            *schema_mod.SELECTOR_DEFERRED_FIELDS, "match", "index"])
_SELECTOR_COMMANDS = frozenset(
    name for name, spec in schema_mod.REGISTRY.items()
    if any(arg.name == "selector" and arg.kind == "selector" for arg in spec.args))


@dataclass
class InlineSteps:
    """The flow built from the input and how to name its steps."""
    flow: schema_mod.Flow
    format: str
    # line of each top-level step: a JSON step's nodes all carry its position
    # as their line, a YAML step's are the lines of the input
    starts: list = field(default_factory=list)

    def step_at(self, line: Any) -> int | None:
        """The 1-based input step that holds ``line``, else None."""
        return _step_at(self.starts, line)


class _Problem(Exception):
    """A JSON step that cannot be a Flow v1 step (refused as invalid_value)."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class _Pairs(list):
    """A JSON object as its (key, value) pairs in order, duplicates kept, so a
    repeated key is refused instead of silently keeping the last one."""


def _step_at(starts: list, line: Any) -> int | None:
    if not isinstance(line, int) or isinstance(line, bool):
        return None
    position = bisect.bisect_right(starts, line)
    return position or None


def _invalid(message: str, hint: str | None = None, **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(errors.INVALID_VALUE, f"ui do: {message}", hint or HINT,
                               **extra)


def _refusal(exc: errors.AutonomError, fmt: str, starts: list, *,
             position: int | None = None, command: str | None = None) -> errors.AutonomError:
    """``exc`` (a Flow parse, schema or pre-flight refusal) as the
    invalid_value of ``ui do``, naming the step and the problem."""
    extra = exc.extra or {}
    line, column = extra.get("line"), extra.get("column")
    if position is None:
        position = _step_at(starts, line)
    command = command or extra.get("command")
    problem = _PREFIX.sub("", exc.message, count=1)
    where = f"step {position}" if position else "input"
    if position and command:
        where += f" ({command})"
    payload: dict[str, Any] = {"format": fmt, "flow_error_code": exc.code}
    if position:
        payload["step"] = position
    if command:
        payload["command"] = command
    if fmt == FORMAT_YAML and isinstance(line, int):
        payload["line"] = line
        if isinstance(column, int):
            payload["column"] = column
    if extra.get("reason"):
        payload["reason"] = extra["reason"]
    hint = exc.hint or (_VAR_HINT if exc.code == errors.FLOW_VAR_UNDEFINED else None)
    return errors.AutonomError(errors.INVALID_VALUE, f"ui do {where}: {problem}",
                               hint or HINT, **payload)


# --- reading ------------------------------------------------------------------------


def read(stream: Any) -> bytes:
    """The bytes on ``stream`` (stdin), at most MAX_BYTES. A terminal is
    refused instead of waiting for someone to type the steps."""
    if stream is None:
        raise _invalid("no stdin to read the steps from",
                       "Pipe the steps in: autonom ui do < steps.json")
    try:
        interactive = bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        interactive = False
    if interactive:
        raise _invalid("the steps are read from stdin, and stdin is a terminal",
                       "Pipe the steps in, e.g. echo '[{\"tapOn\":{\"text\":\"General\"}}]' "
                       "| autonom ui do, or autonom ui do < steps.yaml")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        data = buffer.read(MAX_BYTES + 1)
    else:
        text = stream.read(MAX_BYTES + 1)
        data = text.encode("utf-8") if isinstance(text, str) else bytes(text or b"")
    if len(data) > MAX_BYTES:
        raise _invalid("the steps on stdin are larger than 1 MiB",
                       "Send the steps of one path at a time.")
    return data


# --- loading ------------------------------------------------------------------------


def load(data: bytes | str) -> InlineSteps:
    """Build the flow of the steps in ``data`` (JSON array or YAML step list).
    Raises invalid_value naming the step and the problem; nothing runs."""
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(f"stdin is not UTF-8 text (byte 0x{raw[exc.start]:02x} at offset "
                       f"{exc.start})", "Send the steps as UTF-8 text.") from None
    text = parser_mod.strip_bom(text.replace("\r\n", "\n").replace("\r", "\n"))
    if not text.strip():
        raise _invalid("no steps on stdin")
    fmt = FORMAT_JSON if text.lstrip().startswith(("[", "{")) else FORMAT_YAML
    try:
        steps, starts = _json_steps(text) if fmt == FORMAT_JSON else _yaml_steps(text)
        flow = schema_mod.Flow(path=INLINE_PATH, name=INLINE_NAME)
        flow.steps = steps
        # what this run executes, named the way load_flow names a file's bytes
        flow.source_sha256 = hashlib.sha256(raw).hexdigest()
        inline = InlineSteps(flow=flow, format=fmt, starts=starts)
        _check(inline)
    except RecursionError:
        # inline runFlow bodies may nest without a bound in the schema
        raise _invalid("the steps are nested too deeply", format=fmt) from None
    return inline


def _yaml_steps(text: str) -> tuple[list, list]:
    # Trailing spaces carry nothing in this grammar (a plain scalar drops
    # them, a quoted one closes on its own line), but an item line "- "
    # with nothing after the dash would reach the parser as an item with
    # no content; cut, it is the bare "-" the parser refuses by position.
    lines = [line.rstrip(" ") for line in text.split("\n")]
    # every top-level item starts at column 1: the grammar has no multi-line
    # scalars, so this names the step of a line even when parsing fails
    item_lines = [number for number, line in enumerate(lines, 1)
                  if line == "-" or line.startswith("- ")]
    separator = next((number for number, line in enumerate(lines, 1)
                      if line.strip() == "---"), None)
    if separator is not None:
        raise _invalid(
            f"line {separator} is a '---' separator: ui do takes the steps of a flow, "
            "not a flow file with its header",
            "Send only the '- command' lines, or run the file with "
            "'autonom flow run <file>'.", format=FORMAT_YAML, line=separator)
    try:
        # the commands half of a flow file, parsed by the flow parser itself
        sequence = parser_mod._Parser(lines, 1, INLINE_PATH).parse_top_sequence(  # noqa: SLF001
            empty_line=1)
    except errors.AutonomError as exc:
        raise _refusal(exc, FORMAT_YAML, item_lines) from None
    if not sequence.items:
        raise _invalid("no steps on stdin (only comments)")
    starts = [item.line for item in sequence.items]
    steps = []
    for position, item in enumerate(sequence.items, 1):
        try:
            steps.append(schema_mod.build_step(_short_selectors(item), INLINE_PATH))
        except errors.AutonomError as exc:
            raise _refusal(exc, FORMAT_YAML, starts, position=position,
                           command=_command_of(item)) from None
    return steps, starts


def _json_steps(text: str) -> tuple[list, list]:
    def no_constant(name: str) -> Any:
        raise ValueError(f"{name} is not a JSON number")

    try:
        value = json.loads(text, object_pairs_hook=_Pairs, parse_constant=no_constant)
    except json.JSONDecodeError as exc:
        raise _invalid(f"stdin is not valid JSON: {exc.msg} (line {exc.lineno} column "
                       f"{exc.colno})", format=FORMAT_JSON) from None
    except RecursionError:
        raise _invalid("the steps are nested too deeply", format=FORMAT_JSON) from None
    except ValueError as exc:
        raise _invalid(f"stdin is not valid JSON: {exc}", format=FORMAT_JSON) from None
    if isinstance(value, _Pairs):
        raise _invalid("the JSON is an object; ui do takes an array of steps",
                       'Wrap the step in [ ]: [{"tapOn":{"text":"General"}}].',
                       format=FORMAT_JSON)
    if not isinstance(value, list):
        raise _invalid("the JSON is not an array of steps", format=FORMAT_JSON)
    if not value:
        raise _invalid("the JSON array has no steps", format=FORMAT_JSON)
    starts = list(range(1, len(value) + 1))
    steps = []
    for position, raw in enumerate(value, 1):
        command = None
        try:
            if isinstance(raw, str):
                command = raw
                item: Any = Scalar(raw, "plain", position, 1)
            elif isinstance(raw, _Pairs) and len(raw) == 1:
                command = raw[0][0]
                item = _short_selectors(_node(raw, position))
            else:
                raise _Problem(
                    "a step is an object with exactly one command key, or the name of "
                    "a command that takes no arguments, not "
                    f"{_json_kind(raw)}",
                    'For example {"tapOn":{"text":"General"}}, '
                    '{"assertVisible":"About"} or "back".')
            steps.append(schema_mod.build_step(item, INLINE_PATH))
        except _Problem as problem:
            where = f"step {position}" + (f" ({command})" if command else "")
            extra: dict[str, Any] = {"format": FORMAT_JSON, "step": position}
            if command:
                extra["command"] = command
            raise errors.AutonomError(errors.INVALID_VALUE,
                                      f"ui do {where}: {problem.message}",
                                      problem.hint or HINT, **extra) from None
        except errors.AutonomError as exc:
            raise _refusal(exc, FORMAT_JSON, starts, position=position,
                           command=command) from None
    return steps, starts


def _json_kind(value: Any) -> str:
    if isinstance(value, _Pairs):
        return f"an object with {len(value)} keys"
    if isinstance(value, list):
        return "an array"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    return "a number"


def _node(value: Any, line: int) -> Any:
    """A JSON value as the parser's node of the same YAML-subset value; every
    node of step N carries line N, so a refusal names the step."""
    if isinstance(value, _Pairs):
        pairs = []
        seen: set[str] = set()
        for key, item in value:
            if key in seen:
                raise _Problem(f"duplicate key {key!r}")
            seen.add(key)
            pairs.append((Scalar(key, "plain", line, 1), _node(item, line)))
        return Mapping(pairs, line, 1)
    if isinstance(value, list):
        return Sequence([_node(item, line) for item in value], line, 1)
    if isinstance(value, bool):
        return Scalar("true" if value else "false", "plain", line, 1)
    if isinstance(value, int):
        return Scalar(str(value), "plain", line, 1)
    if isinstance(value, float):
        if not math.isfinite(value):  # 1e999 parses as infinity
            raise _Problem("a number is too large (not a finite number)")
        # positional notation: 1e-05 is not a Flow v1 number, 0.00001 is;
        # and a JSON number with a fraction or exponent keeps a fraction
        # (1e16 is 10000000000000000.0), so it is never taken for an integer
        text = format(Decimal(repr(value)), "f")
        if "." not in text:
            text += ".0"
        return Scalar(text, "plain", line, 1)
    if value is None:
        raise _Problem("null is not a Flow v1 value",
                       "Leave the argument out, or give it a value.")
    scalar = Scalar(value, "double", line, 1)
    # the ${NAME} check the parser makes on every scalar it reads
    parser_mod._Parser([], line, INLINE_PATH)._check_interpolation(scalar)  # noqa: SLF001
    return scalar


def _short_selectors(item: Any) -> Any:
    """``item`` (one step's node) with the selector fields written directly
    under a command that takes a ``selector`` moved under ``selector:``, in
    nested command lists (group, retry, repeat, inline runFlow) too. A step
    that already has ``selector:``, and anything that is not a step mapping,
    is left as it is for the schema to judge."""
    if not isinstance(item, Mapping) or len(item.pairs) != 1:
        return item
    key, value = item.pairs[0]
    if not isinstance(value, Mapping):
        return item
    pairs = []
    for arg_key, arg_value in value.pairs:
        if arg_key.text == "commands" and isinstance(arg_value, Sequence):
            arg_value = Sequence([_short_selectors(sub) for sub in arg_value.items],
                                 arg_value.line, arg_value.col)
        pairs.append((arg_key, arg_value))
    if key.text in _SELECTOR_COMMANDS and all(name.text != "selector" for name, _ in pairs):
        fields = [(name, node) for name, node in pairs if name.text in _SELECTOR_KEYS]
        if fields:
            first = fields[0][0]
            selector = (Scalar("selector", "plain", first.line, first.col),
                        Mapping(fields, first.line, first.col))
            pairs = [selector, *((name, node) for name, node in pairs
                                 if name.text not in _SELECTOR_KEYS)]
    return Mapping([(key, Mapping(pairs, value.line, value.col))], item.line, item.col)


def _command_of(item: Any) -> str | None:
    if isinstance(item, Scalar):
        return item.text
    if isinstance(item, Mapping) and item.pairs:
        return item.pairs[0][0].text
    return None


def _walk(steps: list) -> Iterator[schema_mod.Step]:
    """Every step, composite bodies included."""
    for step in steps:
        yield step
        nested = step.args.get("commands")
        if isinstance(nested, list):
            yield from _walk(nested)


def _check(inline: InlineSteps) -> None:
    """The file-level checks `flow check` makes that apply to inline steps."""
    flow = inline.flow
    for position, top in enumerate(flow.steps, 1):
        for step in _walk([top]):
            if step.command == "runFlow" and "file" in step.args:
                raise _invalid(
                    f"step {position} (runFlow): runFlow file: is not supported; the "
                    "steps of ui do are self-contained",
                    "Inline the steps under runFlow commands:, or run the file with "
                    "'autonom flow run <file>'.",
                    format=inline.format, step=position, command="runFlow",
                    flow_error_code=errors.FLOW_COMMAND_INVALID)
    try:
        validator_mod._check_links(flow)  # noqa: SLF001 - openLink needs a scheme
    except errors.AutonomError as exc:
        raise _refusal(exc, inline.format, inline.starts) from None


def bind_app(inline: InlineSteps, app_id: str | None) -> None:
    """Give the steps the session's app id; a step that needs one is refused
    when the session has none."""
    inline.flow.app_id = app_id or None
    if inline.flow.app_id:
        return
    for position, top in enumerate(inline.flow.steps, 1):
        for step in _walk([top]):
            if step.command in APP_COMMANDS or (step.command == "setPermissions"
                                                and not step.args.get("appId")):
                raise _invalid(
                    f"step {position} ({step.command}): {step.command} acts on the "
                    "session's app, and this session has no app id",
                    "Start the session with an app: autonom session start --app-id <id>.",
                    format=inline.format, step=position, command=step.command,
                    flow_error_code=errors.FLOW_COMMAND_INVALID)


def watch(executor: Any, inline: InlineSteps) -> list[str]:
    """Record what the final screen read keeps out of its reply and saved
    tree (``masking``), each value on its own, as the run used it. Call it
    before ``run``; the list it returns fills while the steps run:

    - the value of every ``sensitive: true`` input of the steps
      (``SENSITIVE_VALUE_COMMANDS``) that names no ``${NAME}``, known before
      any step runs (``$${`` is ``${``, as the executor types it);
    - the value an input step types or sets (its ``value:``, exactly as the
      executor filled its ``${NAME}`` at that step: a run variable as it was
      then, a ``runFlow`` ``env:`` overlay included) when the step is
      ``sensitive: true`` or that value names a secret or a sensitive
      variable. Nothing else filled in is recorded: a step that only uses
      such a variable inside another string (an ``openLink`` url, an
      ``assertVisible`` text, a selector, a ``runFlow`` ``env:`` value)
      adds nothing beyond the variable's own value, recorded below, so a
      whole URL or sentence never hides the screen texts it shares a word
      with;
    - after every step, also one that failed, the value of every variable
      the run keeps sensitive (a ``sensitive: true`` copyTextFrom or
      setClipboard ``into:``, a setClipboard whose value names one,
      ``COPIED_TEXT`` included, which is what ``pasteText`` types): a value
      read off the screen and later replaced by another is still masked.

    It wraps ``_resolve`` and ``_dispatch`` of this one executor instance,
    looked up here so a missing one fails before any step; the executor
    itself is not changed."""
    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in values:
            values.append(value)

    for step in _walk(inline.flow.steps):
        value = step.args.get("value")
        if _sensitive_input(step) and isinstance(value, str) and not any(
                found.group(1) for found in _REFERENCE.finditer(value)):
            add(_REFERENCE.sub("${", value))
    resolve = executor._resolve  # noqa: SLF001
    dispatch = executor._dispatch  # noqa: SLF001
    kept = executor._redact_secrets  # noqa: SLF001 - (names, values) kept secret
    # the steps being dispatched, innermost last (a runFlow dispatches its body)
    dispatched: list[Any] = []

    def resolving(text: str) -> tuple[str, bool]:
        filled, secret = resolve(text)
        step = dispatched[-1] if dispatched else None
        # only the value an input step sends, as it sends it
        if (step is not None and step.command in SENSITIVE_VALUE_COMMANDS
                and text == step.args.get("value")
                and (secret or bool(step.args.get("sensitive")))):
            add(filled)
        return filled, secret

    def dispatching(step: Any, flow: Any, attempts: list) -> Any:
        dispatched.append(step)
        try:
            return dispatch(step, flow, attempts)
        finally:
            dispatched.pop()
            for value in sorted(kept()[1]):
                add(value)

    executor._resolve = resolving  # noqa: SLF001
    executor._dispatch = dispatching  # noqa: SLF001
    return values


# a ${NAME} reference, or the $${ that is typed as ${
_REFERENCE = re.compile(r"\$\$\{|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _sensitive_input(step: Any) -> bool:
    return step.command in SENSITIVE_VALUE_COMMANDS and bool(step.args.get("sensitive"))


# --- masking the final screen -----------------------------------------------------


def mask(nodes: list[dict[str, Any]], values: list[str]) -> list[dict[str, Any]]:
    """``nodes`` with every value of ``values`` masked on its own, the way
    `ui type --sensitive` masks its one value (``observe.masked``): a node
    text or description that may show a value (``observe.shows_secret``)
    reads ``<N chars>``, N the length of that value (of the longest one
    when it may show several), so a short value is never lost inside a
    longer one. Any other text of an editable or focused node also reads
    ``<N chars>``: N is the one value's length, as `ui type` gives it, or,
    when there are several and which one went into that field is not
    known, the length of the text itself."""
    from .. import observe as observe_mod
    from .. import ui as ui_mod

    if not values:
        return nodes
    out: list[dict[str, Any]] = []
    for node in nodes:
        field = ui_mod.is_editable(node) or bool(node.get("focused"))
        hidden: dict[str, str] = {}
        for key in ("text", "desc"):
            text = node.get(key)
            if not isinstance(text, str) or not text:
                continue
            shown = [len(value) for value in values if observe_mod.shows_secret(text, value)]
            if shown:
                hidden[key] = f"<{max(shown)} chars>"
            elif field:
                hidden[key] = f"<{len(values[0]) if len(values) == 1 else len(text)} chars>"
        out.append({**node, **hidden} if hidden else node)
    return out


def scrub(error: dict[str, Any], values: list[str]) -> dict[str, Any]:
    """An observe error with every value of ``values`` cut out of its
    message and hint, each as ``<N chars>`` (one pass, longest first, so
    one value is never cut inside another's mask)."""
    if not values:
        return error
    pattern = re.compile("|".join(re.escape(value)
                                  for value in sorted(values, key=len, reverse=True)))
    return {key: pattern.sub(lambda found: f"<{len(found.group(0))} chars>", value)
            if isinstance(value, str) and key != "code" else value
            for key, value in error.items()}


@contextlib.contextmanager
def masking(values: list[str]) -> Iterator[str | None]:
    """Keep every value of ``values`` out of the final screen read.

    ``observe.after_action`` masks one typed value, its ``secret``; a batch
    may hold several, and one string joining them would hide none of the
    short ones. While this is open, observe masks with ``mask`` and
    ``scrub`` (each value on its own) instead of ``masked`` and
    ``_scrubbed``; both are put back when it closes. It yields the
    ``secret`` to pass to ``after_action``: None when there is nothing to
    mask (observe then masks nothing), else the first value."""
    from .. import observe as observe_mod

    if len(values) < 2:
        yield values[0] if values else None
        return
    saved = observe_mod.masked, observe_mod._scrubbed  # noqa: SLF001
    observe_mod.masked = lambda nodes, _secret: mask(nodes, values)
    observe_mod._scrubbed = lambda error, _secret: scrub(error, values)  # noqa: SLF001
    try:
        yield values[0]
    finally:
        observe_mod.masked, observe_mod._scrubbed = saved  # noqa: SLF001


# --- running ------------------------------------------------------------------------


def run(executor: Any, inline: InlineSteps) -> Any:
    """``executor.run`` of the steps. A Flow definition refusal raised before
    the run started (the pre-flight walk against the target, made before
    any step and before the run's events exist) is the input's problem:
    invalid_value naming the step. Every other failure is the executor's,
    raised as it is; when it names a line, ``step`` adds the input step
    that holds it (``line`` is a YAML line, or the JSON step itself)."""
    try:
        return executor.run(inline.flow)
    except errors.AutonomError as exc:
        started = getattr(executor, "_writer_run_id", None) is not None
        if started or schema_mod.failure_class(exc.code) != schema_mod.FLOW_DEFINITION:
            if exc.extra is not None and "step" not in exc.extra:
                position = inline.step_at(exc.extra.get("line"))
                if position is not None:
                    exc.extra["step"] = position
            raise
        raise _refusal(exc, inline.format, inline.starts) from None
    except RecursionError:
        # inline runFlow bodies nest without a bound; the executor walks
        # them recursively (its pre-flight first, before any step)
        raise _invalid("the steps are nested too deeply to run",
                       "Nest runFlow commands: a few levels deep at most.",
                       format=inline.format) from None


def reply(inline: InlineSteps, result: Any) -> dict[str, Any]:
    """``ok``, ``status``, ``run_id``, the step results and ``failure`` of a
    run (the final screen is added by the caller)."""
    steps = []
    for outcome in result.steps:
        entry: dict[str, Any] = {"index": outcome.index,
                                 "step": inline.step_at(outcome.line),
                                 "command": outcome.command, "status": outcome.status,
                                 "duration_ms": outcome.duration_ms}
        if outcome.label:
            entry["label"] = outcome.label
        if outcome.depth:
            entry["depth"] = outcome.depth
        if outcome.error_code:
            entry["error_code"] = outcome.error_code
        if outcome.skip_reason:
            entry["skip_reason"] = outcome.skip_reason
        steps.append(entry)
    failure = None
    if result.failure:
        source = result.failure
        failure = {"step": inline.step_at(source.get("line")),
                   "index": source.get("step_index"),
                   "command": source.get("command"),
                   "code": source.get("error_code"),
                   "message": source.get("error"),
                   "failure_class": source.get("failure_class")}
        for key in ("holder", "generation"):
            if source.get(key) is not None:
                failure[key] = source[key]
    payload: dict[str, Any] = {"ok": result.status == "passed", "status": result.status,
                               "run_id": result.run_id, "steps": steps, "failure": failure,
                               "events": result.events_path}
    if result.warnings:
        payload["warnings"] = list(result.warnings)
    return payload
