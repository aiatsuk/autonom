"""Android UI Automator backend: parse XML dumps and actuate the screen.

Extracted verbatim from the 0.4.0 `ui.py` so Android behavior is unchanged by
the platform refactor (INV-01). The compact node schema produced here is the
contract iOS must match.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any

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
        "depth": node.depth,
    }


def is_meaningful(node: Any) -> bool:
    """Unchanged from 0.4.0 — operates on the raw UiNode.

    It reads `checkable` and `editable`, which are UI Automator attributes that
    the compact schema does not carry, so the filter must run before compaction
    or its result would silently change.
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


def dump_hierarchy(adb: str, serial: str, *, retries: int = 1) -> str:
    """One UIAutomator dump, complete or refused by name.

    Mid-transition (an activity switching in), `uiautomator dump` returns a
    truncated document; that used to surface as a bare ValueError with no
    `error_code`. Retry once after a short settle, then fail as the backend
    failure it is.
    """
    import time as _time

    last = ""
    for attempt in range(retries + 1):
        completed = adb_mod.run_adb(
            adb,
            ["exec-out", "uiautomator", "dump", "/dev/tty"],
            serial=serial,
            timeout=20,
            check=True,
        )
        assert isinstance(completed.stdout, str)
        last = completed.stdout
        if "</hierarchy>" in last:
            return last
        if attempt < retries:
            _time.sleep(0.5)
    raise errors.AutonomError(
        errors.BACKEND_FAILED,
        "UIAutomator returned an incomplete hierarchy (the screen was probably "
        "mid-transition)",
        "Wait for the screen to settle and retry; 'ui find' polls, 'ui tree' does not.",
    )


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


def type_text(adb: str, serial: str, text: str) -> None:
    # `input text` turns `%s` into a space, so spaces travel as `%s`; the whole
    # payload is then single-quoted so the device shell passes it through
    # literally (see device_shell_quote). `input text` itself only injects
    # characters the device's virtual keyboard map knows: ASCII is reliable,
    # accents and other non-ASCII text depend on the image and may be dropped.
    payload = text.replace(" ", "%s")
    adb_mod.run_adb(adb, ["shell", "input", "text", device_shell_quote(payload)],
                    serial=serial, timeout=10, check=True)


def press_key(adb: str, serial: str, keycode: str) -> None:
    adb_mod.run_adb(adb, ["shell", "input", "keyevent", keycode], serial=serial, timeout=10, check=True)
