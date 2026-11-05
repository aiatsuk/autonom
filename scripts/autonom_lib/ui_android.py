"""Android UI: parse XML dumps, read the screen and actuate it.

The parser and the uiautomator/adb actuation were extracted verbatim from the
0.4.0 `ui.py` so Android behavior is unchanged by the platform refactor
(INV-01). The compact node schema produced here is the contract iOS must
match.

Two backends serve the reads and the inputs (AU-01..AU-05, see "Backend"
below): Autonom's on-device helper (`android_helper`, built on first use),
whose `read` returns the same uiautomator XML, and today's `uiautomator dump`
with `adb shell input`. `--ui-backend`, `AUTONOM_ANDROID_UI_BACKEND` or the
session's stored choice picks one (`auto` by default: the helper when it can
serve the call, else uiautomator and adb with a `fallback_reason`).
"""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack, contextmanager
import re
import subprocess
import threading
import time
from typing import Any, Callable, Iterator, Mapping

from . import adb as adb_mod
from . import errors
from .paths import ensure_ui_scripts_on_path

ROLE_BY_CLASS = {
    "Button": "button",
    "ImageButton": "button",
    "TextView": "text",
    "EditText": "textfield",
    "CheckBox": "checkbox",
    "Switch": "switch",
    "RadioButton": "radio",
    "ImageView": "image",
    "ScrollView": "scroll",
    "RecyclerView": "list",
    "ListView": "list",
    "ProgressBar": "progress",
}


def _load_ui_common():
    ensure_ui_scripts_on_path()
    from ui_common import parse_nodes  # type: ignore

    return parse_nodes


def role_for_class(class_name: str) -> str:
    short = class_name.rsplit(".", 1)[-1] if class_name else ""
    return ROLE_BY_CLASS.get(short, short.lower() or "node")


# Android classes that take typed text. Compose and Flutter text fields both
# report android.widget.EditText; the rest are its framework subclasses
# (AppCompatEditText and TextInputEditText end in "EditText" too).
EDITABLE_CLASSES = ("EditText", "AutoCompleteTextView", "MultiAutoCompleteTextView",
                    "ExtractEditText", "SearchAutoComplete")


def is_editable_class(class_name: str | None) -> bool:
    """Is this an EditText or one of its subclasses, by its short name?"""
    short = str(class_name or "").rsplit(".", 1)[-1]
    return bool(short) and short.endswith(EDITABLE_CLASSES)


def _editable(node: Any) -> bool:
    """Takes typed text: an EditText (or a subclass), or a node with a hint.

    The class is what decides on a stock dump; a `hint` attribute (a field
    showing its placeholder) counts too, and so does an `editable` attribute
    where a dump carries one (``is_meaningful`` reads the same attribute).
    """
    return (node.bool_attr("editable") or is_editable_class(node.class_name)
            or bool((node.attributes.get("hint") or "").strip()))


def compact_node(node: Any, ref: str) -> dict[str, Any]:
    bounds = None
    if node.bounds is not None:
        b = node.bounds
        bounds = [b.left, b.top, b.right, b.bottom]
    return {
        "ref": ref,
        "role": role_for_class(node.class_name),
        "text": node.text or None,
        "desc": node.description or None,
        "resource_id": node.resource_id or None,
        "class": node.class_name or None,
        "package": node.package or None,
        "bounds": bounds,
        "clickable": node.bool_attr("clickable"),
        # Additive: a Flutter control can be long-press-only or checkable
        # without being clickable; `--interactable` has to see both.
        "long_clickable": node.bool_attr("long-clickable"),
        "enabled": node.bool_attr("enabled"),
        "focusable": node.bool_attr("focusable"),
        "focused": node.bool_attr("focused"),
        "scrollable": node.bool_attr("scrollable"),
        "selected": node.bool_attr("selected"),
        "checkable": node.bool_attr("checkable"),
        "checked": node.bool_attr("checked"),
        # Additive: whether typed text lands here, and whether the field
        # hides it (UI Automator's `password`) — the outline prints both.
        "editable": _editable(node),
        "password": node.bool_attr("password"),
        "depth": node.depth,
    }


def is_meaningful(node: Any) -> bool:
    """Unchanged from 0.4.0 — operates on the raw UiNode.

    It reads the raw `checkable` and `editable` attributes (the compact
    `editable` is derived differently, see ``_editable``), so the filter must
    run before compaction or its result would silently change.
    """
    return bool(
        node.text
        or node.description
        or node.resource_id
        or node.bool_attr("clickable")
        or node.bool_attr("scrollable")
        or node.bool_attr("checkable")
        or node.bool_attr("editable")
        or (node.class_name or "").endswith(("EditText", "Button", "Switch", "CheckBox"))
    )


def parse_all(xml_text: str) -> list[dict[str, Any]]:
    """Every node as a compact dict, unfiltered — the search corpus."""
    parse_nodes = _load_ui_common()
    return [compact_node(node, f"n{node.index}") for node in parse_nodes(xml_text)]


def parse_tree(
    xml_text: str,
    *,
    meaningful_only: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = 200,
) -> list[dict[str, Any]]:
    parse_nodes = _load_ui_common()
    result: list[dict[str, Any]] = []
    for node in parse_nodes(xml_text):
        if max_depth is not None and node.depth > max_depth:
            continue
        if meaningful_only and not is_meaningful(node):
            continue
        result.append(compact_node(node, f"n{node.index}"))
        if max_nodes is not None and len(result) >= max_nodes:
            break
    return result


# --- actuation ---------------------------------------------------------------


# What `uiautomator dump` prints (exit 0, no XML) when the screen never went
# idle: it waits for a quiet moment to read the tree, and an animation, a
# video, a ticking timer or a spinner never gives it one. Seen 2026-10-21 on
# an emulator's Settings > About emulated device.
NOT_IDLE_TEXT = "could not get idle state"
# A bad read (output without `</hierarchy>`: a document cut off while an
# activity switches in, or the idle-state refusal above) is read again the
# way it always was: a pause of DUMP_RETRY_PAUSE_S, then a second read, which
# always happens, however long the first read took. A third read (DUMP_READS
# at most) is made only when it can start within DUMP_RETRY_WINDOW_S of the
# end of the first bad read; one dump takes about 2 s on an emulator, so a
# slow device gets the two reads it always had.
DUMP_READS = 3
DUMP_RETRY_PAUSE_S = 0.5
DUMP_RETRY_WINDOW_S = 2.0


def is_not_idle_output(text: str) -> bool:
    """Is this uiautomator's never-idle refusal rather than a (cut) document?

    Only output without a closing `</hierarchy>` counts, so a complete
    dump whose node text happens to say the same words is still a tree.
    """
    return "</hierarchy>" not in text and NOT_IDLE_TEXT in text.lower()


def not_idle_error() -> errors.AutonomError:
    return errors.AutonomError(
        errors.UI_NOT_IDLE,
        "UIAutomator could not read this screen: it never went idle, because "
        "something on it keeps changing (an animation, a video, a timer or a "
        "spinner)",
        "Run 'autonom screenshot' to see this screen; it reads the pixels, not the "
        "tree. 'autonom ui wait --settled' does not help, since this screen does not "
        "stop changing. Read the tree again once the app shows a still screen.",
    )


def dump_hierarchy(adb: str, serial: str, *, reads: int = DUMP_READS,
                   pause_s: float = DUMP_RETRY_PAUSE_S,
                   window_s: float = DUMP_RETRY_WINDOW_S,
                   sleep: Callable[[float], None] | None = None,
                   clock: Callable[[], float] | None = None) -> str:
    """One UIAutomator dump, complete or refused by name.

    A read without `</hierarchy>` (cut off mid-transition, or uiautomator's
    "could not get idle state") is read again after ``pause_s``: the second
    read always happens, as it always did. One more read (``reads`` in all)
    happens only when it can start within ``window_s`` of the end of the
    first bad read. When the last read is bad too, it fails as
    ``ui_not_idle`` if that read said "could not get idle state", else as
    ``backend_failed`` (incomplete hierarchy), as before.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    reads = max(reads, 2)  # never fewer than the read and re-read it always made
    first_bad_end: float | None = None
    last = ""
    for attempt in range(1, reads + 1):
        try:
            completed = adb_mod.run_adb(
                adb,
                ["exec-out", "uiautomator", "dump", "/dev/tty"],
                serial=serial,
                timeout=20,
                check=True,
            )
        except adb_mod.AdbError as exc:
            # A failing exit status is raised at once, as before; when it
            # carries the idle-state words (exit 0 is what was seen) it is
            # named for what it is.
            if is_not_idle_output(exc.message):
                raise not_idle_error() from exc
            raise
        assert isinstance(completed.stdout, str)
        last = completed.stdout
        if "</hierarchy>" in last:
            return last
        if first_bad_end is None:
            first_bad_end = clock()
        if attempt >= reads:
            break
        if attempt >= 2 and clock() + pause_s - first_bad_end > window_s:
            break  # a further read could not start inside the window
        sleep(pause_s)
    if is_not_idle_output(last):
        raise not_idle_error()
    raise errors.AutonomError(
        errors.BACKEND_FAILED,
        "UIAutomator returned an incomplete hierarchy (the screen was probably "
        "mid-transition)",
        "Wait for the screen to settle and retry; 'ui find' polls, 'ui tree' does not.",
    )


# The opening tags of a dump: `<hierarchy rotation="N">`, then its root
# `<node ...>` (pre-order, so the first node is the root). An attribute value
# never holds a raw `>`: the dump's XML writer escapes it.
_HIERARCHY_OPEN = re.compile(r"<hierarchy\b[^>]*>")
_NODE_OPEN = re.compile(r"<node\b[^>]*>")
_BOUNDS_ATTR = re.compile(
    r'\sbounds="\[(-?\d{1,7}),(-?\d{1,7})\]\[(-?\d{1,7}),(-?\d{1,7})\]"')
_ROTATION_ATTR = re.compile(r'\srotation="([0-3])"')
# No display is this many pixels on a side; a root past it is no screen.
DUMP_SCREEN_MAX_EDGE = 100000


def dump_screen(xml: str) -> tuple[int, int, int | None] | None:
    """The screen a `uiautomator dump` lies on, from the dump itself (AU-04):
    ``(width, height, rotation)``, no device call.

    uiautomator clips every node's bounds to the display as it is now (its
    current rotation, which `<hierarchy rotation>` names), and the root
    node of a full-screen window covers all of it, so the root's right and
    bottom edges are the screen input coordinates see. A root that covers
    less (a dialog or a sheet as the active window) gives less than the
    display: every point inside that size is still on screen, and the
    guard decides a point it refuses again on one lookup. None when the
    dump has no root with bounds, or bounds no display has (none above
    ``DUMP_SCREEN_MAX_EDGE``)."""
    if not isinstance(xml, str):
        return None
    hierarchy = _HIERARCHY_OPEN.search(xml)
    if hierarchy is None:
        return None
    root = _NODE_OPEN.search(xml, hierarchy.end())
    bounds = _BOUNDS_ATTR.search(root.group(0)) if root else None
    if bounds is None:
        return None
    right, bottom = int(bounds.group(3)), int(bounds.group(4))
    if not (0 < right <= DUMP_SCREEN_MAX_EDGE and 0 < bottom <= DUMP_SCREEN_MAX_EDGE):
        return None
    turned = _ROTATION_ATTR.search(hierarchy.group(0))
    return right, bottom, int(turned.group(1)) if turned else None


def natural_size(adb: str, serial: str) -> tuple[int, int] | None:
    """`wm size` (the override when one is set): the display in its natural
    orientation, whatever the current rotation is."""
    completed = adb_mod.run_adb(adb, ["shell", "wm", "size"], serial=serial, timeout=10, check=False)
    text = completed.stdout if isinstance(completed.stdout, str) else ""
    for line in reversed(text.splitlines()):
        if "size:" in line and "x" in line:
            try:
                width, height = line.split(":")[-1].strip().split("x")
                return int(width), int(height)
            except ValueError:
                continue
    return None


_SURFACE_ORIENTATION = re.compile(r"SurfaceOrientation:\s*([0-3])\b")
_CURRENT_ROTATION = re.compile(r"\bmCurrentRotation=(?:ROTATION_)?(\d+)\b")
_ROTATION_BY_DEGREES = {"0": 0, "90": 1, "180": 2, "270": 3}


def _shell_text(adb: str, serial: str, args: list[str]) -> str:
    """The command's output, or ``""`` when it fails, times out or cannot be
    started: a rotation source that does not answer names no rotation, so
    ``screen_size`` keeps the natural size."""
    try:
        completed = adb_mod.run_adb(adb, ["shell", *args], serial=serial, timeout=10,
                                    check=False)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if completed.returncode:
        return ""
    return completed.stdout if isinstance(completed.stdout, str) else ""


def parse_rotation(input_dump: str = "", window_dump: str = "",
                   user_rotation: str = "") -> int | None:
    """The display rotation (0..3, quarter turns) from the first source that
    names one: `dumpsys input` SurfaceOrientation, `dumpsys window`
    mCurrentRotation (`ROTATION_90` or `1`), `settings get system
    user_rotation`. None when none does."""
    match = _SURFACE_ORIENTATION.search(input_dump or "")
    if match:
        return int(match.group(1))
    match = _CURRENT_ROTATION.search(window_dump or "")
    if match:
        value = match.group(1)
        if "ROTATION_" in match.group(0):
            if value in _ROTATION_BY_DEGREES:
                return _ROTATION_BY_DEGREES[value]
        elif value in ("0", "1", "2", "3"):
            return int(value)
    value = (user_rotation or "").strip()
    if value in ("0", "1", "2", "3"):
        return int(value)
    return None


def rotation(adb: str, serial: str) -> int | None:
    """The current display rotation, each source asked only when the one
    before it named none."""
    found = parse_rotation(_shell_text(adb, serial, ["dumpsys", "input"]))
    if found is not None:
        return found
    found = parse_rotation(window_dump=_shell_text(adb, serial, ["dumpsys", "window", "displays"]))
    if found is not None:
        return found
    return parse_rotation(user_rotation=_shell_text(
        adb, serial, ["settings", "get", "system", "user_rotation"]))


def rotated_size(size: tuple[int, int], turns: int | None) -> tuple[int, int]:
    """A quarter turn (rotation 1 or 3) swaps width and height."""
    width, height = size
    return (height, width) if turns in (1, 3) else (width, height)


def screen_size(adb: str, serial: str) -> tuple[int, int] | None:
    """The screen as input coordinates see it now: the natural `wm size`,
    swapped while the display is turned to landscape. An unknown rotation
    keeps the natural size."""
    size = natural_size(adb, serial)
    if size is None:
        return None
    return rotated_size(size, rotation(adb, serial))


def tap(adb: str, serial: str, x: int, y: int) -> None:
    adb_mod.run_adb(adb, ["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10, check=True)


def long_press(adb: str, serial: str, x: int, y: int, duration_ms: int) -> None:
    # A zero-distance swipe with a duration IS Android's long press.
    adb_mod.run_adb(
        adb,
        ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms)],
        serial=serial,
        timeout=20,
        check=True,
    )


def swipe(adb: str, serial: str, x1: int, y1: int, x2: int, y2: int, duration: float) -> None:
    milliseconds = str(int(duration * 1000))
    adb_mod.run_adb(
        adb,
        ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), milliseconds],
        serial=serial,
        timeout=20,
        check=True,
    )


def device_shell_quote(value: str) -> str:
    """One literal word for the device shell.

    `adb shell a b c` does not exec argv on the device: adbd joins the words
    with spaces and hands the string to the device's `sh -c`. Any `;`, `&`,
    `|`, `$`, backtick, parentheses, redirections or quotes inside typed text
    would therefore split or expand into device shell syntax. Single quotes
    disable every expansion; a literal `'` is closed, escaped and reopened.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def adb_text_parts(text: str) -> list[str]:
    """The `input text` arguments (before shell quoting) that type ``text``
    exactly, in order.

    `input text` turns `%s` into a space and has no escape for a literal
    `%s`, so spaces travel as `%s` and the text is cut between the `%` and
    the `s` of every literal `%s`: no argument then holds a `%s` that is not
    a space (a `%` at the end of one argument and an `s` at the start of the
    next arrive as they are). Text without a literal `%s` is one argument,
    as before; empty text is one empty argument."""
    parts: list[str] = []
    start = 0
    for index in range(len(text) - 1):
        if text[index] == "%" and text[index + 1] == "s":
            parts.append(text[start:index + 1])
            start = index + 1
    parts.append(text[start:])
    return [part.replace(" ", "%s") for part in parts]


def adb_untypable(text: str) -> list[int]:
    """Positions of the characters of ``text`` that `adb shell input text`
    cannot send: it injects key events of the virtual keyboard map, which
    reliably covers printable ASCII only (space to `~`); accents, other
    scripts, emoji and control characters are dropped or mangled on many
    images."""
    return [index for index, char in enumerate(text) if not " " <= char <= "~"]


def type_text(adb: str, serial: str, text: str) -> None:
    # Each part (adb_text_parts) is single-quoted so the device shell passes
    # it through literally (see device_shell_quote). The caller has checked
    # the text with ``adb_untypable``: `input text` injects only characters
    # the device's virtual keyboard map knows.
    for part in adb_text_parts(text):
        adb_mod.run_adb(adb, ["shell", "input", "text", device_shell_quote(part)],
                        serial=serial, timeout=10, check=True)


def press_key(adb: str, serial: str, keycode: str) -> None:
    adb_mod.run_adb(adb, ["shell", "input", "keyevent", keycode], serial=serial, timeout=10, check=True)


# `input keyevent` takes several keys in one call; a long field is cleared in
# calls of at most this many keys.
ADB_KEYS_PER_CALL = 100


def press_keys(adb: str, serial: str, keycodes: list[str]) -> None:
    """``keycodes`` in order through `adb shell input keyevent`, several per
    call."""
    for start in range(0, len(keycodes), ADB_KEYS_PER_CALL):
        adb_mod.run_adb(adb, ["shell", "input", "keyevent",
                              *keycodes[start:start + ADB_KEYS_PER_CALL]],
                        serial=serial, timeout=20, check=True)


# --- Backend: the on-device helper, or uiautomator and adb (AU-01..AU-05) -------
#
# `preference` names what was asked for: `--ui-backend` (this invocation),
# else AUTONOM_ANDROID_UI_BACKEND, else the session's stored choice
# (`session start --ui-backend`), else `auto`.
#
# - `uiautomator`: today's `uiautomator dump` and `adb shell input`; no helper
#   is built, pushed or contacted.
# - `helper`: every read, tap, long press, swipe, key, typing and fill goes to
#   the helper; one that cannot serve the call is `android_helper_unavailable`
#   before anything is sent.
#
# Typing (AT-01, AT-02): through the helper any Unicode text (`type`: key
# events where the keyboard map has them, set-text insertion at the cursor
# for the rest; `set_text` for `ui fill`). The uiautomator/adb way takes
# printable ASCII only (`adb shell input text`): other text is
# `text_not_typable` before anything is sent, whoever asked (`ui type`,
# `ui fill`, a Flow `inputText`, Mobile Canvas text).
# - `auto`: the helper when it can serve the call; when it cannot (no JDK or
#   SDK, a failed build, a device where it cannot run, a read it failed),
#   the rest of this invocation uses uiautomator and adb, and the reply says
#   why (`fallback_reason`, `fallback_hint`). An input written to the helper
#   and not acknowledged is `ui_action_uncertain` and is never sent again,
#   by the helper or by adb (AH-06).
#
# Before any uiautomator dump on a device whose helper is alive (the explicit
# `uiautomator` backend, a fallback, the sparse-tree recovery's reads), the
# helper is stopped first (AH-07, INV-3): only one UiAutomation holder.
#
# Every helper call holds the device's use lock (`android_helper.use`), and so
# do that stop and the dump after it, together. When another Autonom process
# keeps it past the wait (`HelperBusy`), it is talking to the helper: nothing
# is sent, nothing is stopped and no uiautomator dump is made beside it; the
# call fails with `backend_failed` (`helper_busy`), or
# `android_helper_unavailable` under `--ui-backend helper`. Auto does not fall
# back on that. The same holds for a helper that stays silent while another
# Autonom state directory forwards to it (`helper_shared_forward`): that
# directory's lock is not this one's, so the silence may be its call.
#
# Geometry (AU-04): a helper read carries the display (`width`, `height`,
# `rotation`, input coordinates as they are now); it is recorded for the
# target in this invocation and in the session that owns the target
# (`android_geometry`), as is the `screen` `ui tree` reports. A uiautomator
# dump carries it too: its root node covers the display in its current
# rotation (`dump_screen`), recorded for this invocation only. The
# coordinate guard takes the size from there before it looks anything up,
# so a selector tap after its own read asks the device no size on either
# backend.
#
# `android_helper` (and the build module behind it) is imported only when a
# call needs it, never at start-up.

UI_BACKENDS = ("auto", "helper", "uiautomator")
HELPER = "helper"
UIAUTOMATOR = "uiautomator"
ADB = "adb"
# Keys a CLI reply and a Flow step event carry to say what served them.
PROVENANCE_KEYS = ("ui_backend", "input_backend", "fallback_reason", "fallback_hint")
SESSION_GEOMETRY_KEY = "android_geometry"

# The helper's own limits (native/android/helper/README.md): a read waits
# until 500 ms passed without accessibility events, at most 2000 ms; an input
# settles after its first event (at most 300 ms) the same way, at most
# 2000 ms after the input.
READ_QUIET_MS = 500
READ_MAX_MS = 2000
SETTLE_FIRST_EVENT_MS = 300
SETTLE_MAX_MS = 2000
# What an observed input's budget keeps for the read and the transport
# beyond the settle: the default observe budget, 3000 ms, settles for 2000.
SETTLE_READ_RESERVE_MS = 1000
# The longest wait or gesture one helper request takes (Main.MAX_WAIT_MS:
# first_event_ms, quiet_ms, max_ms and duration_ms above it are bad_request).
# A longer read budget is spent over several reads; a longer gesture goes
# through adb in auto mode.
HELPER_MAX_WAIT_MS = 60000
# Key numbers the helper's `key` op takes on every device it runs on: from 1
# to KeyEvent.getMaxKeyCode() of API 26, its minimum (283,
# KEYCODE_SYSTEM_NAVIGATION_RIGHT). 0 (KEYCODE_UNKNOWN) is refused by the
# helper; a higher number may be unknown to an older device, so auto sends it
# with adb, and `--ui-backend helper` lets the device's helper decide.
HELPER_MIN_KEYCODE = 1
HELPER_SAFE_MAX_KEYCODE = 283
# `adb shell input swipe` reads a negative duration as this many ms.
ADB_NEGATIVE_SWIPE_MS = 300

HINT_HELPER_READ = ("The helper answered but could not read this screen; Android calls in this "
                    "command use uiautomator. 'autonom ui helper status' shows the helper; "
                    "'autonom ui helper stop' ends it.")

_FLAG: str | None = None
_STATE: dict[str, dict[str, Any]] = {}
_GEOMETRY: dict[str, dict[str, Any]] = {}
_LOCAL = threading.local()


def set_preference(value: str | None) -> None:
    """``--ui-backend`` of this invocation. An iOS value (idb, xcuitest) is
    no Android choice and is ignored here: the CLI refuses it for an Android
    target. Starts the invocation afresh: what an earlier one in this
    process decided, measured or recorded is forgotten."""
    global _FLAG
    word = str(value or "").strip().lower()
    _FLAG = word if word in UI_BACKENDS else None
    reset()


def reset() -> None:
    """Forget every backend decision, provenance and geometry of this
    process, and the helper forwards it confirmed (each invocation reads
    `adb forward --list` afresh, once per device)."""
    from sys import modules

    _STATE.clear()
    _GEOMETRY.clear()
    helper = modules.get(f"{__package__}.android_helper")
    if helper is not None:  # not imported only for this
        helper.reset_forward_cache()
    for name in ("read", "limits", "observe", "input_end", "dump", "screen"):
        setattr(_LOCAL, name, None)


def _helper_module():
    from . import android_helper  # lazily: not on the start-up path of quick verbs

    return android_helper


def _owning_session(target: Any) -> dict[str, Any] | None:
    """The session this invocation is bound to, when it owns ``target``."""
    from . import session

    try:
        record = session.load_current()
    except Exception:  # noqa: BLE001 - an unreadable record names no session
        return None
    if (not record or record.get("target_id") != target.target_id
            or (record.get("platform") or "android") != "android"):
        return None
    return record


def preference(target: Any) -> str:
    """auto, helper or uiautomator for ``target`` (see above); a value of
    AUTONOM_ANDROID_UI_BACKEND outside these is ``invalid_value``."""
    record = _owning_session(target)
    return _helper_module().preference(
        _FLAG, session_value=(record or {}).get("ui_backend_preference"))


def _state(target: Any) -> dict[str, Any]:
    return _STATE.setdefault(target.target_id, {})


def _note(target: Any, kind: str, backend: str, *, ui_backend: str | None = None,
          fallback: tuple[str, str | None] | None = None) -> None:
    """Record one read (``kind`` "read") or input ("input") of ``target``:
    the backend that served it, for an input the UI backend it counts under
    (None when nothing says: an adb input auto chose for that input alone),
    and why the helper did not serve it when auto would have used it
    (``fallback``; by default the invocation's fallback, once auto fell back
    and the call took the uiautomator/adb way). Each record carries its
    number, so a caller can ask what served the calls after a mark."""
    state = _state(target)
    use = int(state.get("uses") or 0) + 1
    state["uses"] = use
    state["last_" + kind] = {"use": use, "backend": backend, "ui_backend": ui_backend}
    if fallback is None and backend != HELPER and state.get("fallen_back"):
        fallback = (state.get("fallback_reason") or "", state.get("fallback_hint"))
    if fallback and fallback[0]:
        state["last_fallback"] = {"use": use, "reason": fallback[0], "hint": fallback[1]}


def uses(target_id: str) -> int:
    """How many reads and inputs of ``target_id`` this invocation made: a
    mark a caller takes before a step of its own, to ask ``provenance``
    about that step alone afterwards."""
    return int((_STATE.get(target_id) or {}).get("uses") or 0)


def provenance(target_id: str, since: int = 0) -> dict[str, Any]:
    """What served the reads and inputs of ``target_id`` after mark ``since``
    (a ``uses`` value; 0 for the whole invocation): ``ui_backend`` (what the
    latest read came from; without a read, the UI backend of the latest
    input when one is known), ``input_backend`` (what delivered the latest
    input; absent when nothing was sent) and, when auto did not use the
    helper for one of them, ``fallback_reason`` and ``fallback_hint``. Empty
    when nothing was read or sent."""
    state = _STATE.get(target_id) or {}
    # The opt-out rule, ODA-001 in spec/owner-decisions-after-approval.md:
    # under the explicit ``uiautomator`` choice -- ``--ui-backend
    # uiautomator``, AUTONOM_ANDROID_UI_BACKEND=uiautomator (the test harness
    # default), or a session started with it -- a reply is exactly today's,
    # with no ui_backend, input_backend or fallback_reason (and no settled or
    # settle_ms: those come only from a helper read). The keys appear
    # whenever the helper or the automatic choice is in play: a helper read
    # or input, and auto falling back to uiautomator and adb (then with
    # fallback_reason and fallback_hint).
    if state.get("preference") == UIAUTOMATOR:
        return {}

    def fresh(name: str) -> dict[str, Any] | None:
        item = state.get(name)
        return item if isinstance(item, dict) and int(item.get("use") or 0) > since else None

    read_, sent, fallback = fresh("last_read"), fresh("last_input"), fresh("last_fallback")
    result: dict[str, Any] = {}
    ui_backend = read_["backend"] if read_ else (sent or {}).get("ui_backend")
    if ui_backend:
        result["ui_backend"] = ui_backend
    if sent:
        result["input_backend"] = sent["backend"]
    if fallback:
        result["fallback_reason"] = fallback["reason"]
        if fallback.get("hint"):
            result["fallback_hint"] = fallback["hint"]
    return result


def fall_back(target: Any, reason: str, hint: str | None) -> None:
    """Auto mode: the rest of this invocation reads with uiautomator and sends
    with adb, and each of those calls says why."""
    state = _state(target)
    state["fallback_reason"] = str(reason)
    state["fallback_hint"] = str(hint) if hint else None
    state["fallen_back"] = True


def helper_serves_reads(target: Any) -> bool:
    """Would a read of ``target`` go to the helper now (it may still turn
    out unusable)? No device call."""
    chosen = preference(target)
    return chosen == HELPER or (chosen == "auto" and not _state(target).get("fallen_back"))


def _timeout(op: str, args: Mapping[str, Any], wait_ms: int | None) -> float:
    """The host timeout of ``op``: the client's own, longer by what a settle
    or read was allowed beyond the helper's default 2000 ms."""
    base = _helper_module().timeout_for(op, args)
    extra = max(0, int(wait_ms or 0) - SETTLE_MAX_MS) / 1000.0
    return base + extra


def _helper_call(target: Any, op: str, *, wait_ms: int | None = None,
                 **args: Any) -> dict[str, Any] | None:
    """The helper's reply to ``op`` (``ok`` true or false), or None when this
    call goes the uiautomator/adb way: the preference is uiautomator, or auto
    found the helper unusable (now, or earlier in this invocation). With
    ``--ui-backend helper`` an unusable helper raises
    ``android_helper_unavailable``; nothing was sent to it then. A helper
    another Autonom process is using (`HelperBusy`) fails the call in every
    mode (``backend_failed`` with ``helper_busy`` in auto). An input written
    and not acknowledged raises ``ui_action_uncertain``."""
    chosen = preference(target)
    state = _state(target)
    state["preference"] = chosen
    if chosen == UIAUTOMATOR:
        return None
    if chosen == "auto" and state.get("fallen_back"):
        return None
    helper = _helper_module()
    try:
        return helper.request(target, op, force=chosen == HELPER,
                              timeout=_timeout(op, args, wait_ms), **args)
    except helper.HelperBusy as exc:
        # Another Autonom process is talking to the helper: nothing was
        # sent, and auto must neither stop it nor dump beside it (that would
        # cut that call), so this call fails instead of falling back.
        if chosen == HELPER:
            raise
        raise helper_busy_error(exc, HELPER) from None
    except helper.HelperUnavailable as exc:
        if chosen == HELPER:
            raise
        fall_back(target, exc.reason, exc.hint)
        return None


def _reply_error(reply: Mapping[str, Any]) -> tuple[str, str]:
    error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    code = str(error.get("code") or "internal")
    message = " ".join(str(error.get("message") or "").split())[:300]
    return code, message


# --- reads ----------------------------------------------------------------------


def helper_wait_ms(value: Any) -> int:
    """``value`` as a wait one helper request takes: 0 to
    ``HELPER_MAX_WAIT_MS`` (the helper refuses more with bad_request)."""
    return min(max(0, int(value)), HELPER_MAX_WAIT_MS)


@contextmanager
def read_limits(*, quiet_ms: int, max_ms: int) -> Iterator[None]:
    """Helper reads inside this block wait for ``quiet_ms`` without events,
    at most ``max_ms`` (``ui wait --settled``, observe's own read), each
    within what one helper request takes (0 to ``HELPER_MAX_WAIT_MS``)."""
    previous = getattr(_LOCAL, "limits", None)
    _LOCAL.limits = {"quiet_ms": helper_wait_ms(quiet_ms), "max_ms": helper_wait_ms(max_ms)}
    try:
        yield
    finally:
        _LOCAL.limits = previous


def last_helper_read(target: Any) -> dict[str, Any] | None:
    """``settled``, ``settle_ms``, ``busy``, ``display`` and ``windows`` of
    this thread's latest read of ``target`` when the helper served it; None
    when that read came from uiautomator (or there was none)."""
    held = getattr(_LOCAL, "read", None)
    if not held or held[0] != target.target_id:
        return None
    return held[1]


def last_dump(target: Any) -> tuple[float, float] | None:
    """When (``time.monotonic()``) this thread's latest ``uiautomator dump``
    of ``target`` began and ended: the dump alone, not the helper attempt
    and the stop before it. None when there was none."""
    held = getattr(_LOCAL, "dump", None)
    if not held or held[0] != target.target_id:
        return None
    return held[1], held[2]


def _display_size(display: Any) -> tuple[int, int, int | None] | None:
    if not isinstance(display, dict):
        return None
    width, height = display.get("width"), display.get("height")
    if (isinstance(width, bool) or isinstance(height, bool)
            or not isinstance(width, int) or not isinstance(height, int)
            or width <= 0 or height <= 0):
        return None
    rotation = display.get("rotation")
    return width, height, rotation if rotation in (0, 1, 2, 3) else None


def _keep_helper_read(target: Any, reply: Mapping[str, Any]) -> None:
    info = {"settled": reply.get("settled") is True, "settle_ms": reply.get("settle_ms"),
            "display": reply.get("display"), "windows": reply.get("windows")}
    if reply.get("busy"):
        info["busy"] = reply.get("busy")
    _LOCAL.read = (target.target_id, info)
    _LOCAL.screen = (target.target_id, reply.get("xml"), reply.get("windows"), HELPER,
                     reply.get("display"))
    found = _display_size(reply.get("display"))
    if found:
        record_geometry(target, found[:2], found[2])
    _note(target, "read", HELPER)


def read(target: Any) -> str:
    """One read of the screen as uiautomator XML: through the helper (it
    waits for quiet first, at most 2000 ms, and never answers ``ui_not_idle``)
    or through ``uiautomator dump``."""
    limits = getattr(_LOCAL, "limits", None) or {}
    quiet_ms = limits.get("quiet_ms", READ_QUIET_MS)
    max_ms = limits.get("max_ms", READ_MAX_MS)
    _LOCAL.read = None
    _LOCAL.screen = None
    reply = _helper_call(target, "read", wait_ms=max_ms, quiet_ms=quiet_ms, max_ms=max_ms)
    if reply is not None:
        xml = reply.get("xml")
        if reply.get("ok") is True and isinstance(xml, str) and "</hierarchy>" in xml:
            _keep_helper_read(target, reply)
            return xml
        if reply.get("ok") is True:
            code, message = "bad_reply", "the read carried no complete hierarchy"
        else:
            code, message = _reply_error(reply)
        if preference(target) == HELPER:
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                f"the Android helper could not read the screen ({code}: {message})",
                "Retry; 'autonom ui helper status' shows the helper, and --ui-backend "
                "uiautomator reads with uiautomator instead.",
                helper_error=code, backend=HELPER)
        fall_back(target, f"the helper could not read the screen ({code}: {message})",
                  HINT_HELPER_READ)
    return uiautomator_read(target)


def stop_helper_before_dump(target: Any) -> dict[str, Any] | None:
    """INV-3: a helper that holds UiAutomation on this device is stopped
    before uiautomator reads (quit, a kill of its verified pid, its own
    forward removed). No helper record (a file check), or a record without
    a running helper: nothing, and no device call.

    The stop waits for the device's use lock like any helper call, so it
    never cuts another Autonom process's request; holding the lock, a helper
    that does not answer is stuck and is killed. When another process keeps
    the lock past the wait, nothing is stopped and no dump is made beside
    that call: the read fails with ``backend_failed`` (``helper_busy``)."""
    helper = _helper_module()
    serial = target.target_id
    if not helper.has_record(serial):
        return None
    record = helper.load_record(serial)
    if not isinstance(record.get("port"), int) and not isinstance(record.get("pid"), int):
        return None
    try:
        return helper.stop(target)
    except helper.HelperBusy as exc:
        raise helper_busy_error(exc, UIAUTOMATOR) from None
    except errors.AutonomError as exc:  # the dump then says what it finds
        return {"stopped": False, "error": exc.message}


def helper_busy_error(exc: Any, backend: str) -> errors.AutonomError:
    """``backend_failed`` for a call that found another Autonom process
    talking to the helper (`android_helper.HelperBusy`): nothing was sent,
    stopped or dumped."""
    extra: dict[str, Any] = {"backend": backend, "helper_busy": True, "reason": exc.reason}
    if getattr(exc, "owner_pid", None):
        extra["helper_owner_pid"] = exc.owner_pid
    if getattr(exc, "shared_forward", None):
        extra["helper_shared_forward"] = exc.shared_forward
    if getattr(exc, "foreign_pids", None) is not None:
        extra["helper_foreign_pids"] = list(exc.foreign_pids)
    action = ("uiautomator cannot read the screen now" if backend == UIAUTOMATOR
              else "the Android helper cannot serve this call now")
    return errors.AutonomError(errors.BACKEND_FAILED, f"{action}: {exc.reason}", exc.hint,
                               **extra)


def uiautomator_read(target: Any) -> str:
    """``uiautomator dump``, after a live helper was stopped. The screen the
    dump's root covers (``dump_screen``) is recorded as ``target``'s
    geometry for this invocation, so the coordinate guard of the command
    that made the read asks the device nothing (AU-04).

    On a device this state directory ever started a helper on (its record),
    the stop and the dump run under one hold of the use lock, so no other
    Autonom process starts or uses the helper while uiautomator reads; with
    no record, no lock (no file, no device call)."""
    with ExitStack() as held:
        helper = _helper_module()
        if helper.has_record(target.target_id):
            try:
                held.enter_context(helper.use(target.target_id))
            except helper.HelperBusy as exc:
                raise helper_busy_error(exc, UIAUTOMATOR) from None
            except helper.HelperUnavailable:
                pass  # no lock file can be made: the stop says so, the dump what it finds
        stop_helper_before_dump(target)
        began = time.monotonic()
        xml = dump_hierarchy(target.tool, target.target_id)
        ended = time.monotonic()
    _LOCAL.read = None
    _LOCAL.screen = (target.target_id, xml, None, UIAUTOMATOR, None)
    _LOCAL.dump = (target.target_id, began, ended)
    try:
        found = dump_screen(xml)
    except (TypeError, ValueError):  # the size is a bonus: never fail a good read
        found = None
    if found:
        record_geometry(target, found[:2], found[2], in_session=False)
    _note(target, "read", UIAUTOMATOR)
    return xml


# --- signals (AS-01, AS-02) ------------------------------------------------------------
#
# What a read shows besides its nodes, for reporting only: nothing is clicked,
# dismissed or sent because of it (C-5, NG-5).
#
# ``keyboard``: from the helper's windows list (an ``input_method`` window
# with an area: ``visible`` true, its ``package`` and ``bounds``; none:
# ``visible`` false). A read uiautomator served has no windows list, and
# asking the device for one (``dumpsys input_method``) would add a call to
# every read (DEC-014): ``keyboard`` is null there, meaning unknown.
#
# ``dialog``, the first that applies, else null:
# - ``permission``: a node of the permission controller
#   (``PERMISSION_PACKAGES``), a node with one of its allow/deny button ids
#   (``...:id/permission_allow...``, ``...:id/permission_deny...``), or, in the
#   helper's windows, a window of that package;
# - ``alert``: a node ``android:id/alertTitle`` or ``android:id/button1..3``
#   (the framework's AlertDialog, and the AppCompat/Material ones built on
#   the same button ids);
# - ``window``: only with the helper's windows, never in split screen (a
#   ``split_screen_divider`` window listed), one of:
#   - another application window over the app's (the topmost application
#     window overlaps a lower one), as a popup or a non-modal dialog makes;
#   - a modal dialog, seen from the active window leaving the screen partly
#     uncovered: the active window (when none is active, the topmost
#     application window) is an application window, and all the listed
#     windows together, clipped to the read's display, leave at least
#     ``DIALOG_UNCOVERED_MIN_PERCENT`` percent of the display's area
#     uncovered. A modal window (a Compose dialog, an AlertDialog or a
#     dialog-themed activity) accounts for its whole task area, so Android
#     leaves the app windows beneath it out of the accessibility windows
#     list: only the system bars and the dialog's own window are listed (the
#     Settings "Force stop?" dialog of 2026-10-25 leaves 71% uncovered). A
#     full-screen app (reported full-screen or clipped between the bars it
#     lists), the launcher, an app with the keyboard shown or with a
#     picture-in-picture window over it leave nothing uncovered; the limit
#     keeps a strip no window is listed for (a display cutout in landscape,
#     at most about 6% on a phone; system bars not listed beside a clipped
#     app window, about 8%) from counting as a dialog. A read without a
#     display size, or with more than ``SIGNAL_WINDOWS_MAX`` windows, is not
#     judged this way.
# Each names its ``package``, its ``title`` (null when unknown) and its
# ``buttons`` (``label``, ``resource_id``; at most ``SIGNAL_BUTTONS_MAX``);
# for ``window``, the window's package and title and the buttons inside its
# bounds, of its package.
#
# The explicit uiautomator opt-out (``--ui-backend uiautomator``, the
# environment variable, a session started with it, the test harness default)
# keeps today's reply, without ``signals`` (DEC-018); an auto fall-back read
# carries them with ``keyboard`` null.

PERMISSION_PACKAGES = ("com.android.permissioncontroller",
                       "com.google.android.permissioncontroller")
_PERMISSION_BUTTON_ID = re.compile(r":id/permission_(?:allow|deny)\w*$")
_PERMISSION_TITLE_ID = re.compile(r":id/permission_message$")
ALERT_TITLE_ID = "android:id/alertTitle"
ALERT_BUTTON_IDS = ("android:id/button1", "android:id/button2", "android:id/button3")
_ANY_ALERT_TITLE_ID = re.compile(r":id/alertTitle$")
SIGNAL_BUTTONS_MAX = 8
SIGNAL_TEXT_MAX = 120
# A modal dialog's window leaves at least this share of the display uncovered.
DIALOG_UNCOVERED_MIN_PERCENT = 10
# More windows than a device lists (a handful): the uncovered area is not
# computed, which keeps its grid of window edges small.
SIGNAL_WINDOWS_MAX = 32
WINDOW_APPLICATION = "application"
WINDOW_INPUT_METHOD = "input_method"
_TYPE_INPUT_METHOD = 2  # AccessibilityWindowInfo.TYPE_INPUT_METHOD
_WINDOW_SPLIT_DIVIDER = "split_screen_divider"


def _signal_text(value: Any) -> str | None:
    """A label as a signal reports it: one line, at most SIGNAL_TEXT_MAX
    characters; None when empty."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:SIGNAL_TEXT_MAX] or None


def _rect(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return None
    return [int(item) for item in value]


def _area(rect: list[int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _overlap(first: list[int], second: list[int]) -> bool:
    return (min(first[2], second[2]) > max(first[0], second[0])
            and min(first[3], second[3]) > max(first[1], second[1]))


def _windows_list(windows: Any) -> list[dict[str, Any]] | None:
    """The helper's windows list (its dict entries), or None when the read
    carried none (uiautomator, a reply without one)."""
    if not isinstance(windows, list):
        return None
    return [window for window in windows if isinstance(window, dict)]


def keyboard_signal(windows: Any) -> dict[str, Any] | None:
    """``signals.keyboard`` of a read whose windows list is ``windows``
    (None: unknown)."""
    listed = _windows_list(windows)
    if listed is None:
        return None
    for window in listed:
        kind = window.get("type")
        if kind != WINDOW_INPUT_METHOD and not (
                kind is None and window.get("type_id") == _TYPE_INPUT_METHOD):
            continue
        bounds = _rect(window.get("bounds"))
        if bounds is not None and _area(bounds) > 0:
            return {"visible": True, "package": _signal_text(window.get("package")),
                    "bounds": bounds}
    return {"visible": False, "package": None, "bounds": None}


def _own_label(node: Mapping[str, Any]) -> str | None:
    return _signal_text(node.get("text")) or _signal_text(node.get("desc"))


def _subtree_end(nodes: list[dict[str, Any]], index: int) -> int:
    """The index just past the last descendant of ``nodes[index]`` (nodes in
    document order with their ``depth``)."""
    depth = int(nodes[index].get("depth") or 0)
    end = index + 1
    while end < len(nodes) and int(nodes[end].get("depth") or 0) > depth:
        end += 1
    return end


def _label(nodes: list[dict[str, Any]], index: int) -> str | None:
    """A button's label: its own text or description, else the first one
    inside it (a Compose or layout button keeps its text in a child)."""
    own = _own_label(nodes[index])
    if own:
        return own
    for inner in nodes[index + 1:_subtree_end(nodes, index)]:
        found = _own_label(inner)
        if found:
            return found
    return None


def _button(nodes: list[dict[str, Any]], index: int) -> dict[str, Any]:
    return {"label": _label(nodes, index),
            "resource_id": _signal_text(nodes[index].get("resource_id"))}


def _looks_like_button(node: Mapping[str, Any]) -> bool:
    if node.get("role") == "button":
        return True
    return bool(node.get("clickable")) and not node.get("editable") \
        and not node.get("scrollable")


def _buttons(nodes: list[dict[str, Any]], keep: Callable[[Mapping[str, Any]], bool],
             ) -> list[dict[str, Any]]:
    """The buttons among the nodes ``keep`` accepts: a button-class node, or a
    clickable one that is neither a text field nor a list. Only buttons with
    a label or a resource id; a clickable container whose subtree holds
    another such button is left out for that one, while an inner button with
    neither (a Compose button node beside its text, as in the Settings "Force
    stop?" dialog) leaves the container reported with the label inside it.
    At most SIGNAL_BUTTONS_MAX."""
    named: list[tuple[int, dict[str, Any]]] = []
    for index, node in enumerate(nodes):
        if keep(node) and _looks_like_button(node):
            button = _button(nodes, index)
            if button["label"] or button["resource_id"]:
                named.append((index, button))
    found: list[dict[str, Any]] = []
    for position, (index, button) in enumerate(named):
        end = _subtree_end(nodes, index)
        if any(index < other < end for other, _inner in named[position + 1:]):
            continue  # the button inside it is reported instead
        found.append(button)
        if len(found) >= SIGNAL_BUTTONS_MAX:
            break
    return found


def _resource_package(resource_id: str | None) -> str | None:
    head, sep, _rest = str(resource_id or "").partition(":id/")
    return head if sep and head else None


def _permission_dialog(nodes: list[dict[str, Any]],
                       windows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    by_package = [node for node in nodes if node.get("package") in PERMISSION_PACKAGES]
    by_id = [node for node in nodes
             if _PERMISSION_BUTTON_ID.search(str(node.get("resource_id") or ""))]
    window = next((item for item in windows or ()
                   if item.get("package") in PERMISSION_PACKAGES), None)
    if not by_package and not by_id and window is None:
        return None
    if by_package:
        package = by_package[0].get("package")
    elif window is not None:
        package = window.get("package")
    else:
        package = by_id[0].get("package") or _resource_package(by_id[0].get("resource_id"))
    title = next((_own_label(node) for node in nodes
                  if _PERMISSION_TITLE_ID.search(str(node.get("resource_id") or ""))
                  and _own_label(node)), None)
    if title is None and window is not None:
        title = _signal_text(window.get("title"))

    def keep(node: Mapping[str, Any]) -> bool:
        return (node.get("package") == package
                or bool(_PERMISSION_BUTTON_ID.search(str(node.get("resource_id") or ""))))

    return {"kind": "permission", "package": _signal_text(package), "title": title,
            "buttons": _buttons(nodes, keep)}


def _alert_dialog(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    marks = [node for node in nodes
             if node.get("resource_id") in (ALERT_TITLE_ID, *ALERT_BUTTON_IDS)]
    if not marks:
        return None
    titled = ([node for node in nodes if node.get("resource_id") == ALERT_TITLE_ID]
              + [node for node in nodes
                 if _ANY_ALERT_TITLE_ID.search(str(node.get("resource_id") or ""))])
    title = next((_own_label(node) for node in titled if _own_label(node)), None)
    buttons = [_button(nodes, index) for index, node in enumerate(nodes)
               if node.get("resource_id") in ALERT_BUTTON_IDS][:SIGNAL_BUTTONS_MAX]
    return {"kind": "alert", "package": _signal_text(marks[0].get("package")),
            "title": title, "buttons": buttons}


def _split_screen(windows: list[dict[str, Any]]) -> bool:
    return any(item.get("type") == _WINDOW_SPLIT_DIVIDER for item in windows)


def _app_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The application windows with an area, topmost first: as the device
    lists them, the layer deciding when every one has an integer layer."""
    apps = [item for item in windows if item.get("type") == WINDOW_APPLICATION
            and _rect(item.get("bounds")) is not None and _area(_rect(item["bounds"])) > 0]
    layers = [item.get("layer") for item in apps]
    if all(isinstance(layer, int) and not isinstance(layer, bool) for layer in layers):
        apps = sorted(apps, key=lambda item: -int(item["layer"]))  # stable: a tie keeps the order
    return apps


def _window_signal(nodes: list[dict[str, Any]], window: Mapping[str, Any]) -> dict[str, Any]:
    """``kind: window`` for ``window`` (an application window with an area):
    its package and title, and the buttons whose centre lies inside its
    bounds, of its package."""
    bounds = _rect(window.get("bounds"))
    assert bounds is not None
    package = window.get("package") if isinstance(window.get("package"), str) else None

    def keep(node: Mapping[str, Any]) -> bool:
        found = _rect(node.get("bounds"))
        if found is None or _area(found) <= 0:
            return False
        center_x, center_y = (found[0] + found[2]) // 2, (found[1] + found[3]) // 2
        inside = (bounds[0] <= center_x < bounds[2] and bounds[1] <= center_y < bounds[3])
        return inside and (not package or node.get("package") == package)

    return {"kind": "window", "package": _signal_text(package),
            "title": _signal_text(window.get("title")), "buttons": _buttons(nodes, keep)}


def _window_dialog(nodes: list[dict[str, Any]],
                   windows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Another application window over the app's: the topmost application
    window overlaps a lower one (not in split screen)."""
    if not windows or _split_screen(windows):
        return None
    apps = _app_windows(windows)
    if len(apps) < 2:
        return None
    top, below = apps[0], apps[1:]
    top_bounds = _rect(top.get("bounds"))
    assert top_bounds is not None
    if not any(_overlap(top_bounds, _rect(item.get("bounds")) or [0, 0, 0, 0])
               for item in below):
        return None
    return _window_signal(nodes, top)


def _clipped(rect: list[int], width: int, height: int) -> list[int]:
    return [max(0, rect[0]), max(0, rect[1]), min(width, rect[2]), min(height, rect[3])]


def _union_area(rects: list[list[int]]) -> int:
    """The area the rectangles cover together, each point counted once (a
    grid of their edges: a handful of windows)."""
    xs = sorted({edge for rect in rects for edge in (rect[0], rect[2])})
    ys = sorted({edge for rect in rects for edge in (rect[1], rect[3])})
    total = 0
    for left, right in zip(xs, xs[1:]):
        for top, bottom in zip(ys, ys[1:]):
            if any(rect[0] <= left and right <= rect[2] and rect[1] <= top and bottom <= rect[3]
                   for rect in rects):
                total += (right - left) * (bottom - top)
    return total


def uncovered_area(windows: list[dict[str, Any]], width: int, height: int) -> int | None:
    """The part of a ``width`` x ``height`` display no listed window covers
    (every window with bounds, clipped to the display); None with more than
    SIGNAL_WINDOWS_MAX windows."""
    rects = []
    for item in windows:
        bounds = _rect(item.get("bounds"))
        if bounds is None:
            continue
        clipped = _clipped(bounds, width, height)
        if _area(clipped) > 0:
            rects.append(clipped)
    if len(rects) > SIGNAL_WINDOWS_MAX:
        return None
    return width * height - _union_area(rects)


def _modal_window_dialog(nodes: list[dict[str, Any]], windows: list[dict[str, Any]] | None,
                         display: Any) -> dict[str, Any] | None:
    """A modal dialog: the active window (none active: the topmost
    application window) is an application window, and the listed windows
    leave at least DIALOG_UNCOVERED_MIN_PERCENT of the read's display
    uncovered (see the rules above). None without windows, without a display
    size, in split screen."""
    screen = _display_size(display)
    if not windows or screen is None or _split_screen(windows):
        return None
    active = [item for item in windows if item.get("active") is True]
    if active:
        window = active[0]
        bounds = _rect(window.get("bounds"))
        if window.get("type") != WINDOW_APPLICATION or bounds is None or _area(bounds) <= 0:
            return None
    else:
        apps = _app_windows(windows)
        if not apps:
            return None
        window = apps[0]
    width, height = screen[0], screen[1]
    uncovered = uncovered_area(windows, width, height)
    if uncovered is None or uncovered * 100 < DIALOG_UNCOVERED_MIN_PERCENT * width * height:
        return None
    return _window_signal(nodes, window)


def dialog_signal(nodes: list[dict[str, Any]], windows: Any = None,
                  display: Any = None) -> dict[str, Any] | None:
    """``signals.dialog`` of a read: its nodes (``parse_all``) and, from the
    helper, its windows list and its ``display`` (``{width, height}``; None
    for a uiautomator read: then only the tree decides, and a ``window``
    dialog cannot be seen). The rules in their order: permission, alert,
    another application window over the app's, a modal dialog's window
    leaving the screen partly uncovered (the last needs the display)."""
    listed = _windows_list(windows)
    return (_permission_dialog(nodes, listed) or _alert_dialog(nodes)
            or _window_dialog(nodes, listed) or _modal_window_dialog(nodes, listed, display))


def screen_signals(xml: Any, windows: Any = None, display: Any = None) -> dict[str, Any]:
    """``{"keyboard", "dialog"}`` of one read: its XML and, when the helper
    served it, its windows list and its ``display`` (None: a uiautomator
    read). Reporting only, no device call; a read that cannot be parsed here
    says no dialog."""
    try:
        nodes = parse_all(xml) if isinstance(xml, str) and xml else []
    except Exception:  # noqa: BLE001 - a signal is a bonus: never fail a good read
        nodes = []
    return {"keyboard": keyboard_signal(windows),
            "dialog": dialog_signal(nodes, windows, display)}


def read_signals(target: Any) -> dict[str, Any] | None:
    """``signals`` of this thread's latest read of ``target``: with the
    helper's windows when the helper served it, with ``keyboard`` null when
    an auto fall-back read with uiautomator; None (no key: today's reply,
    DEC-018) under the explicit uiautomator choice, or when there was no
    read. No device call."""
    held = getattr(_LOCAL, "screen", None)
    if not held or held[0] != target.target_id:
        return None
    _target_id, xml, windows, backend, display = held
    if backend != HELPER and _state(target).get("preference") == UIAUTOMATOR:
        return None
    if backend != HELPER:
        return screen_signals(xml)
    return screen_signals(xml, windows, display)


# --- agent screenshots (AS-03) -------------------------------------------------------

AGENT_SHOT_MAX_EDGE = 1000
AGENT_SHOT_QUALITY = 80
# What the helper's `screenshot` takes (Main.java): a long edge from 16 to 100000.
AGENT_SHOT_MIN_EDGE = 16
AGENT_SHOT_MAX_EDGE_LIMIT = 100000
HINT_SHOT_OPT_OUT = ("The uiautomator backend was chosen (--ui-backend uiautomator, "
                     "AUTONOM_ANDROID_UI_BACKEND or the session), which uses no on-device "
                     "helper, so this is the full-size PNG: image pixels are screen pixels.")
HINT_SHOT_FALLBACK = ("This is the full-size PNG: image pixels are screen pixels. 'autonom ui "
                      "helper status' shows the helper; 'autonom doctor' shows whether its "
                      "tools are found.")


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _shot_from_reply(reply: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The JPEG of a helper `screenshot` reply (``data``, ``width``,
    ``height``, ``screen``), or None and why it is unusable."""
    if reply.get("ok") is not True:
        code, message = _reply_error(reply)
        return None, f"{code}: {message}"
    if reply.get("format") != "jpeg":
        return None, f"bad_reply: format {reply.get('format')!r}, not jpeg"
    sizes = [_positive_int(reply.get(key))
             for key in ("width", "height", "screen_width", "screen_height")]
    if None in sizes:
        return None, "bad_reply: the image or screen size is missing"
    encoded = reply.get("data_base64")
    try:
        data = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
    except (binascii.Error, ValueError):
        data = b""
    if not data.startswith(b"\xff\xd8"):
        return None, "bad_reply: the image is not a JPEG"
    width, height, screen_width, screen_height = sizes
    return {"data": data, "width": width, "height": height,
            "screen": (screen_width, screen_height)}, ""


def helper_screenshot(target: Any, max_edge: int = AGENT_SHOT_MAX_EDGE,
                      quality: int = AGENT_SHOT_QUALITY,
                      ) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """An agent screenshot made on the device by the helper (AS-03): a JPEG
    of ``quality`` whose long edge is at most ``max_edge`` (never upscaled).

    ``(shot, None, None)``: ``shot`` has the JPEG ``data``, its ``width`` and
    ``height`` and the ``screen`` size. ``(None, reason, hint)``: take
    today's full-size PNG instead and report why — the uiautomator choice
    (no helper is contacted), auto finding the helper unusable or busy (a
    screencap does not touch UiAutomation, so a busy helper is not cut), or
    a reply without a usable JPEG. Under ``--ui-backend helper`` those
    raise instead (``android_helper_unavailable``, or ``backend_failed``
    for an unusable reply)."""
    chosen = preference(target)
    state = _state(target)
    state["preference"] = chosen
    if chosen == UIAUTOMATOR:
        return None, "the uiautomator backend was chosen: no on-device helper", \
            HINT_SHOT_OPT_OUT
    if chosen == "auto" and state.get("fallen_back"):
        return None, str(state.get("fallback_reason") or "the helper is not used now"), \
            state.get("fallback_hint") or HINT_SHOT_FALLBACK
    helper = _helper_module()
    try:
        reply = helper.request(target, "screenshot", force=chosen == HELPER,
                               max_edge=int(max_edge), quality=int(quality))
    except helper.HelperBusy as exc:
        if chosen == HELPER:
            raise
        return None, exc.reason, HINT_SHOT_FALLBACK
    except helper.HelperUnavailable as exc:
        if chosen == HELPER:
            raise
        fall_back(target, exc.reason, exc.hint)
        return None, exc.reason, HINT_SHOT_FALLBACK
    shot, why = _shot_from_reply(reply)
    if shot is not None:
        return shot, None, None
    if chosen == HELPER:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"the Android helper could not take the screenshot ({why})",
            "Retry; 'autonom ui helper status' shows the helper, and --ui-backend uiautomator "
            "takes today's full-size PNG instead.",
            helper_error=why.split(":", 1)[0], backend=HELPER)
    return None, f"the helper could not take the screenshot ({why})", HINT_SHOT_FALLBACK


# --- geometry (AU-04) ---------------------------------------------------------------


def record_geometry(target: Any, size: tuple[int, int], rotation: int | None = None, *,
                    in_session: bool = True) -> None:
    """Record ``size`` (input coordinates as they are now) as ``target``'s
    geometry: for this invocation, and, unless ``in_session`` is false, in
    the session that owns the target (written only when it changed; never
    into a stopped session). A uiautomator dump's root records for this
    invocation only: a dialog's root covers less than the display, a size
    a later command should not start from."""
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return
    if width <= 0 or height <= 0:
        return
    geometry = {"width": width, "height": height,
                "rotation": rotation if rotation in (0, 1, 2, 3) else None}
    _GEOMETRY[target.target_id] = geometry
    if not in_session:
        return
    record = _owning_session(target)
    if record is None or record.get("stopped_at") or record.get(SESSION_GEOMETRY_KEY) == geometry:
        return
    from . import session

    record[SESSION_GEOMETRY_KEY] = geometry
    try:
        session.save(record)
    except OSError:
        pass  # a geometry that is not kept only costs the next guard a lookup


def recorded_screen(target: Any) -> tuple[tuple[int, int], bool] | None:
    """``(size, measured)``, no device call: the size recorded for
    ``target`` by the latest read of this invocation (a helper read's
    display, a uiautomator dump's root, the ``screen`` `ui tree` looked up;
    ``measured`` True), else by the session that owns it (an earlier
    command's; ``measured`` False). The device may have turned since an
    earlier command, so the coordinate guard trusts a session size only for
    a point that fits it in either orientation."""
    found = _display_size(_GEOMETRY.get(target.target_id))
    if found is not None:
        return (found[0], found[1]), True
    record = _owning_session(target)
    found = _display_size((record or {}).get(SESSION_GEOMETRY_KEY))
    return ((found[0], found[1]), False) if found else None


def lookup_screen_size(target: Any) -> tuple[int, int] | None:
    """The coordinate guard's one lookup of the screen size (AU-04): the
    helper's display info when the helper serves ``target``, else ``wm
    size`` and the rotation sources. Only the guard asks it, right before
    an input that goes to the same backend; every other size lookup
    (``ui.screen_size``: Mobile Canvas, recordings, Flow) is ``screen_size``
    and never starts or waits for the helper."""
    reply = _helper_call(target, "display")
    if reply is not None and reply.get("ok") is True:
        found = _display_size(reply)
        if found:
            return found[0], found[1]
    return screen_size(target.tool, target.target_id)


# --- inputs ---------------------------------------------------------------------------


def observe_inputs(target: Any, timeout_ms: int | None, quiet_ms: int = READ_QUIET_MS) -> None:
    """The next inputs to ``target`` in this command are observed (AL-11):
    through the helper they come back with the settled screen in the same
    round trip (AU-03). ``timeout_ms`` None stops that."""
    if timeout_ms is None:
        _LOCAL.observe = None
        return
    _LOCAL.observe = (target.target_id, settle_args(timeout_ms, quiet_ms))


def settle_args(timeout_ms: int, quiet_ms: int = READ_QUIET_MS) -> dict[str, Any]:
    """The settle and read an observed helper input asks for: first event
    at most 300 ms, then ``quiet_ms`` without events, ending 2000 ms after
    the input for the default 3000 ms observe budget (the budget minus a
    second for the read above it, its whole length below it), and never
    more than one helper request waits (``HELPER_MAX_WAIT_MS``)."""
    budget = max(0, int(timeout_ms))
    max_ms = budget - SETTLE_READ_RESERVE_MS if budget >= 3000 else min(budget, SETTLE_MAX_MS)
    max_ms = helper_wait_ms(max_ms)
    if max_ms <= 0:
        return {"settle": False, "read_after": True}
    return {"settle": {"first_event_ms": min(SETTLE_FIRST_EVENT_MS, max_ms),
                       "quiet_ms": helper_wait_ms(quiet_ms), "max_ms": max_ms},
            "read_after": True}


def input_ended(target: Any) -> float | None:
    """When (``time.monotonic()``) this thread's latest helper input to
    ``target`` ended (its request written, then its gesture), or None."""
    held = getattr(_LOCAL, "input_end", None)
    return held[1] if held and held[0] == target.target_id else None


def observed_settle(target: Any) -> dict[str, Any] | None:
    held = getattr(_LOCAL, "observe", None)
    if not held or held[0] != target.target_id:
        return None
    return dict(held[1])


# The helper's refusals of `type` and `set_text` that have a code of their own
# in the CLI (nothing was typed): (Autonom error code, hint).
_TEXT_REFUSALS: dict[str, tuple[str, str]] = {
    "text_not_typable": (
        errors.TEXT_NOT_TYPABLE,
        "Nothing was typed: the field's current text cannot be read (a password field, or "
        "a field that exposes no text), so inserting characters the keyboard map cannot "
        "produce could overwrite it. Clear the field first ('autonom ui fill' replaces its "
        "whole text), or type only characters the keyboard produces."),
    "fill_target_mismatch": (
        errors.FILL_TARGET_MISMATCH,
        "Nothing was typed: the field that has input focus is not the target, so the tap "
        "did not focus it. Read the screen with 'autonom ui tree' and retry with a selector "
        "that names the field."),
    "no_focus": (
        errors.NO_FOCUSED_FIELD,
        "Nothing was typed: no text field has input focus. Tap the field first (ui tap), "
        "or use 'autonom ui fill' with a selector for it."),
    "not_editable": (
        errors.NO_FOCUSED_FIELD,
        "Nothing was typed: the view that has input focus is not a text field. Tap the "
        "field first (ui tap), or use 'autonom ui fill' with a selector for it."),
}


def _refused(op: str, reply: Mapping[str, Any]) -> errors.AutonomError:
    """An error reply to an input: not performed, or failed part way while
    being injected (``inject_failed``, ``internal``); never re-sent. A
    typing refusal (``_TEXT_REFUSALS``) keeps its own code."""
    code, message = _reply_error(reply)
    if op in ("type", "set_text") and code in _TEXT_REFUSALS:
        mapped, hint = _TEXT_REFUSALS[code]
        return errors.AutonomError(
            mapped, f"the Android helper did not type the text ({code}: {message})", hint,
            helper_error=code, op=op, backend=HELPER)
    partial = code in ("inject_failed", "internal")
    if partial:
        hint = ("It may have been injected in part: read the screen with 'autonom ui tree' "
                "before sending anything again.")
    elif code == "bad_request":
        hint = ("Nothing was performed: the helper refused the request's arguments. "
                "--ui-backend uiautomator sends it with adb input instead.")
    else:
        hint = "Nothing was sent; read the screen with 'autonom ui tree' and retry."
    return errors.AutonomError(
        errors.BACKEND_FAILED, f"the Android helper did not perform the {op} ({code}: {message})",
        hint, helper_error=code, op=op, backend=HELPER)


# The key of the reply ``_helper_input`` gives for an observed input that an
# interrupt (Ctrl-C, a host's SIGINT) cut off after its request was written
# (AL-11): the helper performs it whatever happens to the connection, so the
# input was sent, and observe reports the interrupt beside the action
# instead of failing it. No helper reply carries this key.
INPUT_INTERRUPTED = "_interrupted"


def input_interrupted(reply: Any) -> bool:
    """Is ``reply`` the one of an observed helper input whose settle an
    interrupt cut off (``INPUT_INTERRUPTED``)?"""
    return isinstance(reply, Mapping) and reply.get(INPUT_INTERRUPTED) is True


def _helper_input(target: Any, op: str, args: dict[str, Any],
                  extra: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``op`` through the helper: its reply, or None for the adb route. An
    input that is not observed does not wait for a settle (``settle: false``),
    as `adb shell input` never did.

    An observed input waits in the same request for the settle and the read
    after it (AU-03), up to its observe budget: an interrupt in that wait,
    once the request was written, ends observing, not the action (AL-11).
    The reply is then ``{"ok": true, INPUT_INTERRUPTED: true}``, and observe
    reports ``observe_error: interrupted`` (the input was sent: never send it
    again). An interrupt before the write still ends the command, with
    nothing sent."""
    observed = bool(extra)
    extra = dict(extra) if extra else {"settle": False}
    settle = extra.get("settle")
    wait_ms = settle.get("max_ms") if isinstance(settle, dict) else None
    before = time.monotonic()
    try:
        reply = _helper_call(target, op, wait_ms=wait_ms, **args, **extra)
    except KeyboardInterrupt:
        if not observed or _written_at(op, before) is None:
            raise
        reply = {"ok": True, INPUT_INTERRUPTED: True}
    if reply is None:
        return None
    if reply.get("ok") is not True:
        raise _refused(op, reply)
    # when the input ended: its request was written (after any helper start
    # and hello), then the gesture ran; the helper's settle counts from here
    sent_at = _written_at(op, before) or before
    gesture_ms = args.get("duration_ms") if op in ("long_press", "swipe") else 0
    _LOCAL.input_end = (target.target_id, sent_at + max(0, int(gesture_ms or 0)) / 1000.0)
    _note(target, "input", HELPER, ui_backend=HELPER)
    after = reply.get("read")
    if isinstance(after, dict):
        found = _display_size(after.get("display"))
        if found:
            record_geometry(target, found[:2], found[2])
    return reply


def _written_at(op: str, since: float) -> float | None:
    """When (``time.monotonic()``) this thread wrote ``op`` to the helper,
    if it did at or after ``since``; else None."""
    written = _helper_module().last_write()
    if written and written[0] == op and written[1] >= since:
        return written[1]
    return None


def _adb_input(target: Any, fallback: tuple[str, str | None] | None = None) -> None:
    """An input `adb shell input` delivered. It counts under uiautomator when
    the whole invocation is on uiautomator and adb (the choice, or auto fell
    back: the reason is reported); an input auto sent with adb for its own
    reason (``fallback``) names no UI backend, as nothing was read for it."""
    state = _state(target)
    whole = state.get("preference") == UIAUTOMATOR or bool(state.get("fallen_back"))
    _note(target, "input", ADB, ui_backend=UIAUTOMATOR if whole else None, fallback=fallback)


def _not_for_helper(target: Any, message: str, reason: str, hint: str, auto_hint: str,
                    **extra: Any) -> tuple[str, str | None] | None:
    """An input the helper cannot take as asked (a key it has no number
    for, a gesture longer than one request takes). ``--ui-backend helper``:
    ``invalid_value`` (``message``, ``hint``) before anything is sent. Auto:
    it goes through adb, and the reply says why (``reason`` and
    ``auto_hint``, returned). None when adb is the route anyway
    (uiautomator, or auto fell back)."""
    chosen = preference(target)
    state = _state(target)
    state["preference"] = chosen
    if chosen == HELPER:
        raise errors.AutonomError(errors.INVALID_VALUE, message, hint, **extra)
    if chosen == UIAUTOMATOR or state.get("fallen_back"):
        return None
    return reason, auto_hint


def _gesture_ms(duration_ms: int) -> int | None:
    """``duration_ms`` as the helper's gesture length, the way `adb shell
    input swipe` reads it (a negative length is its default, 300 ms; 0 is
    down and up at once, 1 ms for the helper, which takes 1 to 60000 ms);
    None for a gesture longer than one helper request takes."""
    if duration_ms < 0:
        return ADB_NEGATIVE_SWIPE_MS
    if duration_ms > HELPER_MAX_WAIT_MS:
        return None
    return max(1, duration_ms)


def _long_gesture(target: Any, op: str, duration_ms: int) -> tuple[str, str | None] | None:
    return _not_for_helper(
        target,
        f"the Android helper takes a {op} of at most {HELPER_MAX_WAIT_MS} ms; this one is "
        f"{duration_ms} ms",
        f"the helper takes a {op} of at most {HELPER_MAX_WAIT_MS} ms; this one ({duration_ms} ms) "
        "was sent with adb shell input",
        f"Keep the {op} within {HELPER_MAX_WAIT_MS} ms, or use --ui-backend uiautomator "
        "(adb input) for a longer one.",
        f"A {op} of at most {HELPER_MAX_WAIT_MS} ms goes through the helper.",
        op=op, duration_ms=duration_ms, max_ms=HELPER_MAX_WAIT_MS)


def send_tap(target: Any, x: int, y: int, *, extra: Mapping[str, Any] | None = None
             ) -> tuple[str, dict[str, Any] | None]:
    """A tap: ``("helper", reply)`` or ``("adb", None)``."""
    reply = _helper_input(target, "tap", {"x": int(x), "y": int(y)}, extra)
    if reply is not None:
        return HELPER, reply
    tap(target.tool, target.target_id, x, y)
    _adb_input(target)
    return ADB, None


def send_long_press(target: Any, x: int, y: int, duration_ms: int, *,
                    extra: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any] | None]:
    """A long press: ``("helper", reply)`` or ``("adb", None)``; one longer
    than the helper takes goes through adb in auto mode."""
    helper_ms = _gesture_ms(int(duration_ms))
    fallback = reply = None
    if helper_ms is None:
        fallback = _long_gesture(target, "long_press", int(duration_ms))
    else:
        reply = _helper_input(target, "long_press",
                              {"x": int(x), "y": int(y), "duration_ms": helper_ms}, extra)
    if reply is not None:
        return HELPER, reply
    long_press(target.tool, target.target_id, x, y, duration_ms)
    _adb_input(target, fallback)
    return ADB, None


def send_swipe(target: Any, x1: int, y1: int, x2: int, y2: int, duration: float, *,
               extra: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any] | None]:
    """A swipe of ``duration`` seconds: ``("helper", reply)`` or ``("adb",
    None)``; its length in ms is the one adb would send (whole ms)."""
    milliseconds = int(duration * 1000)
    helper_ms = _gesture_ms(milliseconds)
    fallback = reply = None
    if helper_ms is None:
        fallback = _long_gesture(target, "swipe", milliseconds)
    else:
        reply = _helper_input(target, "swipe", {"x1": int(x1), "y1": int(y1), "x2": int(x2),
                                                "y2": int(y2), "duration_ms": helper_ms}, extra)
    if reply is not None:
        return HELPER, reply
    swipe(target.tool, target.target_id, x1, y1, x2, y2, duration)
    _adb_input(target, fallback)
    return ADB, None


def text_route(target: Any) -> str:
    """Where typing to ``target`` goes now, without a device call: ``HELPER``
    (the choice is helper, or auto has not fallen back; the helper may still
    turn out unusable, and auto then falls back with nothing sent) or
    ``ADB`` (the uiautomator choice, or auto fell back). A bad
    AUTONOM_ANDROID_UI_BACKEND is ``invalid_value`` here, before anything is
    typed."""
    chosen = preference(target)
    state = _state(target)
    state["preference"] = chosen
    if chosen == UIAUTOMATOR or (chosen == "auto" and state.get("fallen_back")):
        return ADB
    return HELPER


HINT_TEXT_ADB = ("adb shell input text types printable ASCII only. The on-device helper types "
                 "any text: use --ui-backend auto or helper (it needs a JDK and the Android SDK "
                 "on this host; 'autonom doctor' shows android_helper), or type ASCII only.")


def check_adb_typable(target: Any, text: str, *, verb: str = "type") -> None:
    """``text_not_typable`` before anything is sent when `adb shell input
    text` cannot type ``text`` (``adb_untypable``). The error names how many
    characters and where the first one is, never the characters: the text
    may be a credential. After an auto fall-back it carries the reason."""
    bad = adb_untypable(text)
    if not bad:
        return
    extra: dict[str, Any] = {"characters": len(bad), "first_position": bad[0],
                             "backend": ADB}
    state = _state(target)
    if state.get("fallen_back") and state.get("fallback_reason"):
        extra["fallback_reason"] = state["fallback_reason"]
    raise errors.AutonomError(
        errors.TEXT_NOT_TYPABLE,
        f"{len(bad)} character(s) of the text (the first at position {bad[0]}) cannot be "
        f"sent with adb shell input text; nothing was typed ({verb})",
        HINT_TEXT_ADB, **extra)


def send_text(target: Any, text: str, *, extra: Mapping[str, Any] | None = None
              ) -> tuple[str, dict[str, Any] | None]:
    """Type ``text`` at the cursor of the focused field: ``("helper", reply)``
    or ``("adb", None)``.

    Through the helper (`type`) any Unicode text: key events where the
    keyboard map has them, set-text insertion at the cursor for the rest
    (the reply carries ``method``, ``text_after`` and ``complete``; a part
    that did not land stops the typing, ``complete: false``). An observed
    call asks for the settle and the screen after it in the same request.
    The helper refuses with ``no_focus``/``not_editable``
    (``no_focused_field``) and ``text_not_typable``; nothing was typed then.
    Empty text sends nothing through the helper (``("helper", None)``).

    The uiautomator/adb way (the explicit choice, or auto fell back, now or
    before) is today's `adb shell input text`, printable ASCII only: other
    text is ``text_not_typable`` before anything is sent; a literal `%s`
    arrives as `%s` (``adb_text_parts``)."""
    if text_route(target) == HELPER:
        if not text:
            return HELPER, None
        reply = _helper_input(target, "type", {"text": text}, extra)
        if reply is not None:
            return HELPER, reply
    check_adb_typable(target, text)
    type_text(target.tool, target.target_id, text)
    _adb_input(target)
    return ADB, None


# The settle of the tap `ui fill` focuses its target with: the helper's own
# defaults (first event at most 300 ms, then 500 ms without events, at most
# 2000 ms), so the field has focus and the keyboard is up before its text is
# set.
FILL_TAP_SETTLE = {"first_event_ms": SETTLE_FIRST_EVENT_MS, "quiet_ms": READ_QUIET_MS,
                   "max_ms": SETTLE_MAX_MS}


def send_focus_tap(target: Any, x: int, y: int) -> dict[str, Any] | None:
    """The tap `ui fill` focuses its target with, through the helper: it
    settles and reads the screen after it in the same request, so the
    caller finds the field where it is now (the keyboard may have moved
    it). None: the adb route, nothing was sent. An interrupt once the tap
    was written ends the fill (the tap was sent; nothing more is)."""
    reply = _helper_input(target, "tap", {"x": int(x), "y": int(y)},
                          {"settle": dict(FILL_TAP_SETTLE), "read_after": True})
    if input_interrupted(reply):
        raise KeyboardInterrupt
    return reply


def _bounds_arg(bounds: Any) -> list[int] | None:
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        values = [int(value) for value in bounds]
    except (TypeError, ValueError):
        return None
    return values if values[2] > values[0] and values[3] > values[1] else None


def send_set_text(target: Any, text: str, bounds: Any, *,
                  extra: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Replace the whole text of the focused field with ``text`` through the
    helper (`set_text`), refused with ``fill_target_mismatch`` when the
    focused field is not at ``bounds`` (the target's, as the screen shows it
    now). The reply carries ``text_after`` (null for a password field or one
    that exposes no text) and ``complete``. None: the adb route, nothing was
    sent."""
    args: dict[str, Any] = {"text": text}
    expected = _bounds_arg(bounds)
    if expected is not None:
        args["expect_bounds"] = expected
    return _helper_input(target, "set_text", args, extra)


def adb_fill(target: Any, text: str, *, point: tuple[int, int] | None,
             current_length: int) -> int:
    """`ui fill` the uiautomator/adb way: a tap at ``point`` to focus the
    field (None: it has focus already), `KEYCODE_MOVE_END`, then
    `KEYCODE_DEL` as many times as the field's current text is long (at
    least once), then `adb shell input text` (nothing for empty text). The
    caller has checked the text (``check_adb_typable``) before any input.
    Returns how many `KEYCODE_DEL` were sent."""
    if point is not None:
        tap(target.tool, target.target_id, int(point[0]), int(point[1]))
        _adb_input(target)
    deletes = max(1, int(current_length or 0))
    press_keys(target.tool, target.target_id, ["KEYCODE_MOVE_END"] + ["KEYCODE_DEL"] * deletes)
    _adb_input(target)
    if text:
        type_text(target.tool, target.target_id, text)
        _adb_input(target)
    return deletes


# android.view.KeyEvent constants the helper's `key` op takes by number (it
# takes no names); `adb shell input keyevent` resolves the same names.
_KEY_NUMBERS: dict[str, int] = {
    "UNKNOWN": 0, "SOFT_LEFT": 1, "SOFT_RIGHT": 2, "HOME": 3, "BACK": 4, "CALL": 5,
    "ENDCALL": 6, "STAR": 17, "POUND": 18, "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21,
    "DPAD_RIGHT": 22, "DPAD_CENTER": 23, "VOLUME_UP": 24, "VOLUME_DOWN": 25, "POWER": 26,
    "CAMERA": 27, "CLEAR": 28, "COMMA": 55, "PERIOD": 56, "ALT_LEFT": 57, "ALT_RIGHT": 58,
    "SHIFT_LEFT": 59, "SHIFT_RIGHT": 60, "TAB": 61, "SPACE": 62, "SYM": 63, "EXPLORER": 64,
    "ENVELOPE": 65, "ENTER": 66, "DEL": 67, "GRAVE": 68, "MINUS": 69, "EQUALS": 70,
    "LEFT_BRACKET": 71, "RIGHT_BRACKET": 72, "BACKSLASH": 73, "SEMICOLON": 74,
    "APOSTROPHE": 75, "SLASH": 76, "AT": 77, "NUM": 78, "HEADSETHOOK": 79, "FOCUS": 80,
    "PLUS": 81, "MENU": 82, "NOTIFICATION": 83, "SEARCH": 84, "MEDIA_PLAY_PAUSE": 85,
    "MEDIA_STOP": 86, "MEDIA_NEXT": 87, "MEDIA_PREVIOUS": 88, "MEDIA_REWIND": 89,
    "MEDIA_FAST_FORWARD": 90, "MUTE": 91, "PAGE_UP": 92, "PAGE_DOWN": 93,
    "PICTSYMBOLS": 94, "SWITCH_CHARSET": 95, "ESCAPE": 111, "FORWARD_DEL": 112,
    "CTRL_LEFT": 113, "CTRL_RIGHT": 114, "CAPS_LOCK": 115, "SCROLL_LOCK": 116,
    "META_LEFT": 117, "META_RIGHT": 118, "FUNCTION": 119, "SYSRQ": 120, "BREAK": 121,
    "MOVE_HOME": 122, "MOVE_END": 123, "INSERT": 124, "FORWARD": 125, "MEDIA_PLAY": 126,
    "MEDIA_PAUSE": 127, "MEDIA_CLOSE": 128, "MEDIA_EJECT": 129, "MEDIA_RECORD": 130,
    "NUM_LOCK": 143, "NUMPAD_DIVIDE": 154, "NUMPAD_MULTIPLY": 155, "NUMPAD_SUBTRACT": 156,
    "NUMPAD_ADD": 157, "NUMPAD_DOT": 158, "NUMPAD_COMMA": 159, "NUMPAD_ENTER": 160,
    "NUMPAD_EQUALS": 161, "NUMPAD_LEFT_PAREN": 162, "NUMPAD_RIGHT_PAREN": 163,
    "VOLUME_MUTE": 164, "INFO": 165, "CHANNEL_UP": 166, "CHANNEL_DOWN": 167,
    "ZOOM_IN": 168, "ZOOM_OUT": 169, "SETTINGS": 176, "APP_SWITCH": 187, "ASSIST": 219,
    "BRIGHTNESS_DOWN": 220, "BRIGHTNESS_UP": 221, "SLEEP": 223, "WAKEUP": 224,
    "VOICE_ASSIST": 231, "CUT": 277, "COPY": 278, "PASTE": 279, "ALL_APPS": 284,
    "REFRESH": 285,
}
_KEY_NUMBERS.update({str(digit): 7 + digit for digit in range(10)})
_KEY_NUMBERS.update({chr(ord("A") + offset): 29 + offset for offset in range(26)})
_KEY_NUMBERS.update({f"F{number}": 130 + number for number in range(1, 13)})
_KEY_NUMBERS.update({f"NUMPAD_{digit}": 144 + digit for digit in range(10)})


def keycode_number(key: str) -> int | None:
    """The KeyEvent number of ``key`` (a decimal number, or a name with or
    without ``KEYCODE_``, as `input keyevent` takes it); None when Autonom
    does not know the name."""
    word = str(key).strip()
    if word.isdigit() and word.isascii():
        return int(word)
    name = word[len("KEYCODE_"):] if word.startswith("KEYCODE_") else word
    return _KEY_NUMBERS.get(name)


# The Android key names and numbers Autonom sends (AT-03, widened by owner
# decision ODA-004): `KEYCODE_[A-Z0-9_]+`, a bare upper-case name such as
# BACK (sent as KEYCODE_BACK), or a decimal key number of 1 to 4 digits.
# Every Android key goes through `android_key` before any call to the device:
# `ui key`, a Flow `pressKey`, `back` or `eraseText` step (`ui do` steps
# included) and a Mobile Canvas bridge key, as all of them call
# `ui.press_key`. Four digits (up to 9999) hold every Android key code with
# room to spare (the highest, KeyEvent.getMaxKeyCode(), is a few hundred),
# while a longer number can only be a mistake: it never reaches `adb shell
# input keyevent` or Python's int(), which refuses more than 4300 digits.
_KEY_FULL_NAME = re.compile(r"KEYCODE_[A-Z0-9_]+")
_KEY_BARE_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
KEY_NUMBER_MAX_DIGITS = 4
_KEY_DECIMAL = re.compile(r"[0-9]{1,%d}" % KEY_NUMBER_MAX_DIGITS)
HINT_KEY_NAME = ("Use an upper-case key name such as BACK, HOME or ENTER (sent as "
                 "KEYCODE_BACK, ...), a KEYCODE_* name, or the key's number from "
                 f"android.view.KeyEvent (at most {KEY_NUMBER_MAX_DIGITS} digits), e.g. 4.")


def android_key(key: Any) -> str:
    """``key`` as Autonom sends it on Android: a ``KEYCODE_*`` name or a
    decimal number of 1 to `KEY_NUMBER_MAX_DIGITS` digits as given, a bare
    upper-case name with ``KEYCODE_`` in front (``BACK`` -> ``KEYCODE_BACK``).
    Anything else (shell text, spaces, lower case, punctuation, an empty
    word, ``KEYCODE_`` alone, a longer number) is ``invalid_value`` (``flag:
    "keycode"``, ``value``); the message does not repeat the word, as a
    Flow key may come from a secret variable."""
    word = key if isinstance(key, str) else ""
    if _KEY_FULL_NAME.fullmatch(word) or _KEY_DECIMAL.fullmatch(word):
        return word
    if _KEY_BARE_NAME.fullmatch(word) and not word.startswith("KEYCODE_"):
        return "KEYCODE_" + word
    raise errors.AutonomError(
        errors.INVALID_VALUE,
        "not an Android key: give an upper-case key name (BACK or KEYCODE_BACK) or a key "
        f"number of at most {KEY_NUMBER_MAX_DIGITS} digits", HINT_KEY_NAME, flag="keycode",
        value=key)


HINT_KEY_NUMBER = ("Pass the key's number (android.view.KeyEvent, from 1), e.g. 'autonom ui "
                   "key 4' for KEYCODE_BACK, or use --ui-backend uiautomator (adb input).")
HINT_KEY_NUMBER_AUTO = ("A key number from 1 (android.view.KeyEvent), e.g. 'autonom ui key 4' "
                        "for KEYCODE_BACK, goes through the helper.")


def send_key(target: Any, key: str, *, extra: Mapping[str, Any] | None = None
             ) -> tuple[str, dict[str, Any] | None]:
    """A key: through the helper by its number. In auto mode a key the
    helper cannot take goes through `adb shell input keyevent` as given,
    and the reply says why (nothing was sent to the helper): a name Autonom
    has no number for, 0 (KEYCODE_UNKNOWN), or a number above the highest
    every device the helper runs on knows (``HELPER_SAFE_MAX_KEYCODE``).
    With ``--ui-backend helper`` the first two are ``invalid_value``, and a
    higher number goes to the helper, whose device decides. No message and
    no ``fallback_reason`` repeats the key's word (a Flow key may come from
    a secret variable, and both are stored with the run); a key number may
    appear."""
    number = keycode_number(key)
    chosen = preference(target)
    fallback = reply = None
    if number is None:
        fallback = _not_for_helper(
            target,
            "the Android helper takes key numbers, and the key is not a key name Autonom knows",
            "the helper takes key numbers, and the key is not a key name Autonom knows; it was "
            "sent with adb shell input keyevent",
            HINT_KEY_NUMBER, HINT_KEY_NUMBER_AUTO, key=str(key))
    elif number < HELPER_MIN_KEYCODE:
        fallback = _not_for_helper(
            target,
            f"the Android helper sends key numbers from {HELPER_MIN_KEYCODE}; the key is "
            f"{number}",
            f"the helper sends key numbers from {HELPER_MIN_KEYCODE}, and the key is {number}; "
            "it was sent with adb shell input keyevent",
            HINT_KEY_NUMBER, HINT_KEY_NUMBER_AUTO, key=str(key))
    elif number > HELPER_SAFE_MAX_KEYCODE and chosen != HELPER:
        fallback = _not_for_helper(
            target, "",  # never raised: not the helper choice
            f"key number {number} is above {HELPER_SAFE_MAX_KEYCODE}, the highest every device "
            "the helper runs on knows; it was sent with adb shell input keyevent",
            "",  # never raised
            "--ui-backend helper sends it through the helper, which refuses a number its "
            "device does not know.", key=str(key))
    else:
        reply = _helper_input(target, "key", {"keycode": number}, extra)
    if reply is not None:
        return HELPER, reply
    press_key(target.tool, target.target_id, key)
    _adb_input(target, fallback)
    return ADB, None


def settle_report(reply: Mapping[str, Any], *, quiet_ms: int,
                  elapsed_ms: int | None = None, snapshots: int = 1) -> dict[str, Any]:
    """A helper settle (an input's settle block, or a read's quiet wait) in
    the shape ``ui.settle`` reports: ``method: "helper"``, one read (more
    when a budget above one request's limit was spent over several)."""
    settled = reply.get("settled") is True
    settle_ms = reply.get("settle_ms") if isinstance(reply.get("settle_ms"), int) else None
    result: dict[str, Any] = {
        "settled": settled, "method": HELPER, "snapshots": max(1, int(snapshots)), "changes": 0,
        "elapsed_ms": elapsed_ms if elapsed_ms is not None else (settle_ms or 0),
        "dump_ms": 0, "stable_ms": max(int(quiet_ms), 0) if settled else 0,
        "settle_ms": settle_ms,
    }
    if "first_event_ms" in reply:
        result["first_event_ms"] = reply.get("first_event_ms")
    if reply.get("busy"):
        result["busy"] = reply.get("busy")
    return result
