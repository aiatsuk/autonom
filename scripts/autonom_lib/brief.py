"""A reply in a few lines of text.

In an agent-profile session the act and wait verbs (`ui tap|swipe|pinch|type|
fill|key`, `ui do`, `ui wait`, `network requests wait`) print this instead of
the JSON line: what was done, and what changed on the screen. `--json` on the
verb prints the JSON line with every key; `--screen` adds the whole outline
to the text. The text is for reading, not for parsing: a script passes
`--json`.

Line order, each only when there is something to say:

1. ``! not on the fast path: ...`` when a read or an input of the call did
   not go the fast way (`fallback_reason`, `fallback_hint`);
2. the status line: the verb, ``ok`` and what it acted on;
3. ``until met in N ms: ...`` (an unmet wait is a warning line instead);
4. ``screen: <app> <screen>, N nodes`` with what is over it (keyboard, dialog);
5. ``changes: +A -R ~C`` and the changed outline lines; ``changes: none``;
   or, with nothing to compare with, the whole outline;
6. ``shot: <path>`` for the screenshot the call took;
7. one ``! <code>: <error> <hint>`` line per warning.

Every function takes the reply as `emit` prints it and returns the text; none
reads a device or changes the reply.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import wait

FAST_PATH = "! not on the fast path: "


def _sentence(*parts: Any) -> str:
    """The parts that are there, each ending in one full stop."""
    words = []
    for part in parts:
        text = " ".join(str(part or "").split())
        if text:
            words.append(text if text[-1] in ".!?" else text + ".")
    return " ".join(words)


def fast_path_line(data: Mapping[str, Any]) -> str | None:
    """The first line when the call did not go the fast way, with how to get
    it back."""
    reason = data.get("fallback_reason")
    if not reason:
        return None
    return FAST_PATH + _sentence(reason, data.get("fallback_hint"))


# A warning whose `error` only repeats the status line: its hint is printed.
_SAID_BY_STATUS = ("wait_not_met",)


def _warning_lines(data: Mapping[str, Any]) -> list[str]:
    lines = []
    error = data.get("observe_error")
    if isinstance(error, Mapping):
        lines.append(f"! observe failed ({error.get('code')}): "
                     + _sentence(error.get("message"), error.get("hint")))
    for item in data.get("warnings") or ():
        if not isinstance(item, Mapping):
            lines.append(f"! {item}")
        elif item.get("code") in _SAID_BY_STATUS:
            lines.append("! " + _sentence(item.get("hint")))
        else:
            lines.append(f"! {item.get('code')}: "
                         + _sentence(item.get("error"), item.get("hint")))
    return lines


def _point(value: Any) -> str:
    return ",".join(str(part) for part in value) if isinstance(value, (list, tuple)) else str(value)


def _acted_on(data: Mapping[str, Any]) -> str:
    """What the input was, from the keys each action verb reports."""
    words: list[str] = []
    if data.get("gesture"):
        words.append(f"scale {data.get('scale')} at {_point(data.get('at'))}")
    elif "from" in data and "to" in data:
        words.append(f"{_point(data['from'])} -> {_point(data['to'])}")
    elif data.get("x") is not None and data.get("y") is not None:
        words.append(f"at {data['x']},{data['y']}")
        if data.get("ref"):
            words.append(f"({data['ref']})")
        if data.get("duration_ms"):
            words.append(f"held {data['duration_ms']} ms")
    if "keycode" in data:
        words.append(str(data["keycode"]))
    for key in ("typed", "filled"):
        if key in data:
            value = str(data[key])
            words.append(value if data.get("sensitive") else f'"{value}"')
    if "filled" in data and data.get("ref"):
        words.append(f"({data['ref']})")
    if data.get("complete") is False:
        words.append("(not all of it landed)")
    if data.get("text_matches") is False:
        words.append("(the field shows something else)")
    return " ".join(words)


def _until_line(until: Any) -> str | None:
    if not isinstance(until, Mapping) or not until.get("met"):
        return None
    named = []
    for item in until.get("conditions") or ():
        label = wait.label_of(item)
        request = item.get("request")
        if isinstance(request, Mapping):
            label += f" ({_request(request)})"
        named.append(label)
    return f"until met in {until.get('elapsed_ms')} ms: " + "; ".join(named)


def _request(request: Mapping[str, Any]) -> str:
    words = [str(request.get("method") or "?"),
             f"{request.get('host') or ''}{request.get('path') or ''}"]
    if request.get("completed") is False:
        words.append("did not complete")
    elif request.get("status") is not None:
        words.append(str(request["status"]))
    if request.get("mocked"):
        words.append("mocked")
    if request.get("duration_ms") is not None:
        words.append(f"{request['duration_ms']} ms")
    if request.get("id"):
        words.append(f"id {request['id']}")
    return " ".join(words)


def _screen_lines(data: Mapping[str, Any], whole: bool) -> list[str]:
    screen = data.get("screen")
    if not isinstance(screen, Mapping):
        return []
    foreground = data.get("foreground") or {}
    where = " ".join(str(foreground[key]) for key in ("app", "screen") if foreground.get(key))
    head = f"screen: {where or 'unknown app'}, {screen.get('count')} nodes"
    if screen.get("truncated"):
        head += " (more not listed)"
    signals = screen.get("signals") or {}
    keyboard = signals.get("keyboard")
    if isinstance(keyboard, Mapping) and keyboard.get("visible"):
        head += ", keyboard up"
    dialog = signals.get("dialog")
    if isinstance(dialog, Mapping):
        head += ", " + " ".join(word for word in (
            str(dialog.get("kind") or ""), "dialog",
            f'"{dialog["title"]}"' if dialog.get("title") else "") if word)
    lines = [head]
    changes = data.get("changes")
    outline = str(screen.get("outline") or "")
    if isinstance(changes, Mapping):
        counts = (changes.get("added") or 0, changes.get("removed") or 0,
                  changes.get("changed") or 0)
        if not any(counts):
            lines.append("changes: none")
        else:
            lines.append("changes: +%d -%d ~%d" % counts)
            shown = list(changes.get("lines") or ())
            lines.extend(shown)
            if sum(counts) > len(shown):
                lines.append(f"... {sum(counts) - len(shown)} more (--screen for the whole "
                             "screen)")
        if whole and outline:
            lines.extend(["outline:", outline])
    elif outline:
        # nothing to compare with: the screen itself is the news
        lines.extend(["changes: unknown (" + str(data.get("changes_baseline_reason")
                                                 or "no earlier screen") + "); outline:",
                      outline])
    return lines


def _shot_line(data: Mapping[str, Any]) -> str | None:
    shot = data.get("shot")
    if not isinstance(shot, Mapping) or not shot.get("path"):
        return None
    return f"shot: {shot['path']}"


def _joined(first: str | None, lines: list[str | None]) -> str:
    return "\n".join(line for line in [first, *lines] if line)


def action(verb: str, data: Mapping[str, Any], *, screen: bool = False) -> str:
    """The reply of an action verb (`ui tap|swipe|pinch|type|fill|key`)."""
    status = " ".join(word for word in (verb, "ok", _acted_on(data)) if word)
    return _joined(fast_path_line(data),
                   [status, _until_line(data.get("until")), *_screen_lines(data, screen),
                    _shot_line(data), *_warning_lines(data)])


def steps(data: Mapping[str, Any], *, screen: bool = False) -> str:
    """The reply of `ui do`: one line per step, the failure, the final screen."""
    listed = data.get("steps") or ()
    status = f"ui do {data.get('status')}: {len(listed)} step(s)"
    lines: list[str | None] = [status]
    for item in listed:
        words = [f"  {item.get('index')}. {item.get('command')}", str(item.get("status"))]
        if item.get("error_code"):
            words.append(f"({item['error_code']})")
        elif item.get("skip_reason"):
            words.append(f"({item['skip_reason']})")
        lines.append(" ".join(words))
    failure = data.get("failure")
    if isinstance(failure, Mapping):
        lines.append(f"failed at step {failure.get('index')} ({failure.get('command')}): "
                     + _sentence(f"{failure.get('code')}: {failure.get('message')}"))
    lines.extend(_screen_lines(data, screen))
    lines.extend(_warning_lines(data))
    return _joined(fast_path_line(data), lines)


def waited(data: Mapping[str, Any]) -> str:
    """The reply of `ui wait` (settled, a node, a node gone) and of `network
    requests wait`."""
    if "settled" in data and "wait" not in data:
        if data.get("settled"):
            status = f"settled in {data.get('elapsed_ms')} ms"
        else:
            status = f"NOT settled after {data.get('elapsed_ms')} ms"
    else:
        label = wait.label_of(data)
        if data.get("met"):
            status = f"wait met in {data.get('elapsed_ms')} ms: {label}"
            if isinstance(data.get("request"), Mapping):
                status += f" ({_request(data['request'])})"
        else:
            status = f"wait NOT met after {data.get('elapsed_ms')} ms: {label}"
        if data.get("count") is not None:
            status += f" ({data['count']} on screen)"
    return _joined(fast_path_line(data), [status, *_warning_lines(data)])


# --- the replies the MCP server words (`autonom mcp serve`) -------------------------------


def tree(data: Mapping[str, Any]) -> str:
    """`ui tree --format outline`: one head line, then the outline."""
    head = f"screen: {data.get('count')} nodes"
    if data.get("truncated"):
        head += " (more not listed)"
    size = data.get("screen")
    if isinstance(size, (list, tuple)) and len(size) == 2:
        head += f", {size[0]}x{size[1]}"
    signals = data.get("signals") or {}
    keyboard = signals.get("keyboard")
    if isinstance(keyboard, Mapping) and keyboard.get("visible"):
        head += ", keyboard up"
    dialog = signals.get("dialog")
    if isinstance(dialog, Mapping):
        head += ", " + " ".join(word for word in (
            str(dialog.get("kind") or ""), "dialog",
            f'"{dialog["title"]}"' if dialog.get("title") else "") if word)
    return _joined(fast_path_line(data), [head, str(data.get("outline") or "") or None,
                                          *_warning_lines(data)])


def devices(data: Mapping[str, Any]) -> str:
    """`devices`: one line per device, the running ones first."""
    listed = sorted(data.get("devices") or (), key=lambda item: not item.get("running"))
    lines: list[str | None] = []
    for item in listed:
        words = [str(item.get("platform")), str(item.get("target_id")),
                 "running" if item.get("running") else str(item.get("state") or "not running"),
                 f'"{item.get("avd") or item.get("name")}"']
        if item.get("runtime"):
            words.append(str(item["runtime"]))
        lines.append(" ".join(words))
    if not lines:
        lines.append("no devices")
    idle = [str(name) for name in data.get("avds") or ()
            if not any(item.get("avd") == name for item in listed)]
    if idle:
        lines.append("avds not running: " + ", ".join(idle))
    return _joined(None, [*lines, *_warning_lines(data)])


def session(data: Mapping[str, Any]) -> str:
    """`session start` and `session stop` (which reports its `teardown`)."""
    record = data.get("session")
    if "teardown" in data or not isinstance(record, Mapping):
        named = f" {record.get('session_id')}" if isinstance(record, Mapping) else ""
        return _joined(f"session{named} stopped", _warning_lines(data))
    words = [f"session {record.get('session_id')} on {record.get('platform')} "
             f"{record.get('target_id')}"]
    if record.get("app_id"):
        words.append(f"app {record['app_id']}")
    if data.get("launched"):
        words.append("launched")
    if data.get("installed"):
        words.append("installed")
    return _joined(", ".join(words), _warning_lines(data))


def shot(data: Mapping[str, Any]) -> str:
    """`screenshot`: where the file is and how to read its pixels."""
    line = f"shot: {data.get('path')}"
    if data.get("width") and data.get("height"):
        line += f" ({data['width']}x{data['height']})"
    return _joined(line, [str(data["coordinates"]) if data.get("coordinates") else None,
                          *_warning_lines(data)])


def failure(envelope: Mapping[str, Any]) -> str:
    """An error envelope (`ok: false`, `error_code`, `error`, `hint`)."""
    line = f"error {envelope.get('error_code')}: " + _sentence(envelope.get("error"))
    hint = envelope.get("hint")
    if hint and not str(hint).startswith("usage:"):
        line += "\n" + _sentence(hint)
    return _joined(fast_path_line(envelope), [line])
