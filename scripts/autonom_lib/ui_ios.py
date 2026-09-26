"""iOS accessibility tree -> the compact node schema (CAP-IOSUI-001).

`idb ui describe-all` returns the *accessibility* hierarchy, not the UIKit or
SwiftUI view tree. Its quality therefore depends on how well the app is
labelled; a Flutter app without `Semantics` can produce almost nothing, which
this module reports as `sparse_accessibility_tree` rather than letting an agent
conclude the screen is empty (RISK-013).

The parser accepts both a nested (`children`) and a flat element list, because
the exact shape varies across idb versions (U-001) and is confirmed by the
TASK-2.0.1 probe rather than assumed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from . import errors, ios_idb, ios_simctl
from .platform import Target

CHILD_KEYS = ("children", "child_elements", "elements")

# AX type / trait -> compact role. Unknown types fall back to a lowercased type.
# Types observed on iOS 26.0 via idb 1.1.7 / idb-companion 1.1.8, plus the
# common XCUIElementType names. Anything unmapped falls back to a lowercased
# type, which stays honest rather than guessing.
ROLE_BY_TYPE = {
    "Button": "button", "StaticText": "text", "Text": "text", "Heading": "heading",
    "TextField": "textfield", "SecureTextField": "textfield", "TextView": "textfield",
    "SearchField": "textfield", "Image": "image", "Cell": "cell", "Switch": "switch",
    "Toggle": "switch", "Slider": "slider", "Link": "link", "Table": "list",
    "CollectionView": "list", "ScrollView": "scroll", "NavigationBar": "navbar",
    "TabBar": "tabbar", "Toolbar": "toolbar", "Alert": "alert", "Sheet": "sheet",
    "ProgressIndicator": "progress", "Group": "group", "Other": "node",
    "Application": "app", "Window": "window",
    # Spelled out (the lowercased fallback gives the same roles) because the
    # toggle state below keys on them.
    "CheckBox": "checkbox", "RadioButton": "radiobutton", "ToggleButton": "togglebutton",
}
# Two-state controls. idb reports their state in AXValue, not in a boolean:
# a Flutter `Checkbox` (and a UIKit/SwiftUI switch) comes back as
# `{"type": "CheckBox", "AXValue": "1"}` / `"0"`, some builds say "on"/"off".
# `checked` used to read only `checked`/`AXChecked`, which idb never sends,
# so it was false for every control and `assertChecked` could not pass on iOS.
TOGGLE_ROLES = {"checkbox", "switch", "togglebutton", "radiobutton"}
TOGGLE_AX_ROLES = {"AXCheckBox", "AXSwitch", "AXToggle", "AXRadioButton"}
TOGGLE_VALUES = {
    "1": True, "true": True, "on": True, "yes": True, "checked": True, "selected": True,
    "0": False, "false": False, "off": False, "no": False, "unchecked": False,
    "not selected": False,
}
CLICKABLE_ROLES = {"button", "link", "cell", "switch", "tabbar", "slider",
                   "checkbox", "radiobutton", "togglebutton"}


def _first(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in source and source[name] not in (None, ""):
            return source[name]
    return None


def _flatten(payload: Any) -> list[dict[str, Any]]:
    """Depth-first element list from either tree shape."""
    elements: list[dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, depth)
            return
        if not isinstance(node, dict):
            return
        children: Iterable[Any] = ()
        for key in CHILD_KEYS:
            if isinstance(node.get(key), list):
                children = node[key]
                break
        record = {key: value for key, value in node.items() if key not in CHILD_KEYS}
        record["_depth"] = depth
        elements.append(record)
        for child in children:
            walk(child, depth + 1)

    walk(payload, 0)
    return elements


def _bounds(element: dict[str, Any]) -> list[int] | None:
    frame = _first(element, "frame", "AXFrame", "rect")
    if isinstance(frame, dict):
        try:
            x = float(_first(frame, "x", "X", "origin_x") or 0)
            y = float(_first(frame, "y", "Y", "origin_y") or 0)
            width = float(_first(frame, "width", "Width", "w") or 0)
            height = float(_first(frame, "height", "Height", "h") or 0)
        except (TypeError, ValueError):
            return None
        # Points, never pixels — do not scale by the display factor (INV-06).
        return [int(x), int(y), int(x + width), int(y + height)]
    return None


def _role(element: dict[str, Any]) -> str:
    raw = _first(element, "type", "AXType", "role", "element_type") or ""
    short = str(raw).replace("XCUIElementType", "")
    return ROLE_BY_TYPE.get(short, short.lower() or "node")


def _truthy(element: dict[str, Any], *names: str, default: bool = False) -> bool:
    value = _first(element, *names)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def is_toggle(element: dict[str, Any], role: str | None = None) -> bool:
    """A checkbox, switch, toggle button, or radio button — by compact role,
    or by the AX role/subrole idb reports next to the type."""
    if (role or _role(element)) in TOGGLE_ROLES:
        return True
    return any(str(element.get(key) or "") in TOGGLE_AX_ROLES for key in ("role", "subrole"))


def toggle_state(element: dict[str, Any]) -> bool | None:
    """The on/off state a toggle's AXValue carries; None when it says neither
    (absent, or a mixed checkbox's "2")."""
    value = _first(element, "AXValue", "value")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return {1: True, 0: False}.get(int(value)) if value in (0, 1) else None
    if value is None:
        return None
    return TOGGLE_VALUES.get(str(value).strip().lower())


def _checked(element: dict[str, Any], role: str) -> bool:
    if _first(element, "checked", "AXChecked") is not None:
        return _truthy(element, "checked", "AXChecked")
    if is_toggle(element, role):
        return bool(toggle_state(element))
    return False


def compact_node(element: dict[str, Any], ref: str) -> dict[str, Any]:
    role = _role(element)
    traits = element.get("traits") or element.get("AXTraits") or []
    trait_text = " ".join(traits) if isinstance(traits, list) else str(traits)
    return {
        "ref": ref,
        "role": role,
        "text": _first(element, "AXValue", "value", "title") or None,
        "desc": _first(element, "AXLabel", "label", "name") or None,
        # The accessibility identifier is iOS's stable selector, the closest
        # thing to Android's resource-id. Plan §2.4 assumed null here; mapping it
        # gives Flutter/SwiftUI apps a durable way to be targeted.
        "resource_id": _first(element, "AXUniqueId", "identifier", "accessibility_identifier") or None,
        "class": str(_first(element, "type", "AXType", "role") or "") or None,
        "package": _first(element, "bundle_id", "bundleID") or None,
        "bounds": _bounds(element),
        "clickable": role in CLICKABLE_ROLES or "Button" in trait_text
        or _truthy(element, "hittable", "AXHittable"),
        "enabled": _truthy(element, "enabled", "AXEnabled", default=True),
        # iOS accessibility has no "focusable" concept; AXFocused is the
        # FOCUSED state and used to be misfiled under focusable — fixed.
        "focusable": False,
        "focused": _truthy(element, "focused", "AXFocused", "has_focus"),
        "scrollable": role in {"scroll", "list"},
        "selected": _truthy(element, "selected", "AXSelected"),
        "checked": _checked(element, role),
        "depth": int(element.get("_depth") or 0),
    }


def is_meaningful(node: dict[str, Any]) -> bool:
    return bool(
        node.get("text")
        or node.get("desc")
        or node.get("resource_id")
        or node.get("clickable")
        or node.get("scrollable")
        or node.get("role") in {"textfield", "slider", "button", *TOGGLE_ROLES}
    )


def _load(payload: str | dict | list) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    text = (payload or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some idb builds emit one JSON object per line.
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise errors.AutonomError(
                    errors.BACKEND_FAILED,
                    f"could not parse idb describe-all output: {exc}",
                    "Capture the raw output and check the idb version with 'autonom doctor'.",
                ) from exc
        return records


def parse_all(payload: str | dict | list) -> list[dict[str, Any]]:
    return [
        compact_node(element, f"n{index}")
        for index, element in enumerate(_flatten(_load(payload)))
    ]


def parse_tree(
    payload: str | dict | list,
    *,
    meaningful_only: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    for node in parse_all(payload):
        if max_depth is not None and node["depth"] > max_depth:
            continue
        if meaningful_only and not is_meaningful(node):
            continue
        nodes.append(node)
        if max_nodes is not None and len(nodes) >= max_nodes:
            break

    warnings: list[dict[str, Any]] = []
    if meaningful_only and len(nodes) < 3:
        warnings.append({
            "code": "sparse_accessibility_tree",
            "error": f"only {len(nodes)} meaningful node(s) found",
            "hint": "The app may not expose accessibility data. Add accessibility labels and "
                    "identifiers (Flutter: Semantics; SwiftUI: .accessibilityLabel / "
                    ".accessibilityIdentifier), re-run with --all, or fall back to a screenshot.",
        })
    if nodes and not any(node.get("resource_id") for node in nodes):
        warnings.append({
            "code": "no_accessibility_identifiers",
            "error": "no element exposes an accessibility identifier",
            "hint": "Select by --text or --desc; --resource-id cannot match on this screen.",
        })
    return nodes, warnings


# --- actuation ---------------------------------------------------------------


def describe_all(target: Target) -> str:
    return ios_idb.describe_all(target)


def screen_size(target: Target) -> tuple[int, int] | None:
    """Screen rectangle in points, used by the tap guard (INV-06).

    Taken from the accessibility tree's own root frame so it is expressed in
    the same coordinate space as the nodes; a mismatch is exactly what the guard
    exists to catch.
    """
    try:
        return screen_size_from(describe_all(target))
    except errors.AutonomError:
        return None


def screen_size_from(payload: str | dict | list) -> tuple[int, int] | None:
    """The root application/window frame, else the widest extent seen."""
    nodes = parse_all(payload)
    for node in nodes:
        if node["depth"] == 0 and node.get("role") in {"app", "window"} and node.get("bounds"):
            bounds = node["bounds"]
            return bounds[2] - bounds[0], bounds[3] - bounds[1]
    widest = 0
    tallest = 0
    for node in nodes:
        bounds = node.get("bounds")
        if bounds:
            widest = max(widest, bounds[2])
            tallest = max(tallest, bounds[3])
    return (widest, tallest) if widest and tallest else None


# --- HID backend: idb or AXe ---------------------------------------------------
#
# idb_companion before 1.6.2 cannot load SimulatorKit on Xcode 27, so every
# HID verb fails while the accessibility tree still works. AXe drives the same
# simulator HID through its own FBSimulatorControl build and works there. The
# tree stays on idb either way: only input is routed.
#
# AUTONOM_IOS_HID (or a ``ios_hid`` alias on the Target) selects the route:
#   idb  - always idb;
#   axe  - always AXe (missing AXe is an error);
#   auto - idb, unless idb HID is known broken and AXe is available. "Known"
#          is the doctor probe (only for a PATH-resolved idb; a pinned client
#          may not pair with the companion on PATH) or, failing that, idb itself
#          answering ios_hid_framework_missing — that failure happens before
#          any event is delivered, so re-sending through AXe repeats nothing.

HID_ENV = "AUTONOM_IOS_HID"
AXE_ENV = "AUTONOM_AXE"
HID_MODES = ("auto", "idb", "axe")
AXE_INSTALL_HINT = "Install AXe: brew install cameroncooke/axe/axe (or set AUTONOM_AXE=/path/to/axe)."

_hid_probe: dict[str, Any] | None = None
_last_backend: str | None = None


def reset_hid_probe() -> None:
    """Forget the per-process HID probe (tests, or after an upgrade)."""
    global _hid_probe, _last_backend
    _hid_probe = None
    _last_backend = None


def last_backend() -> str | None:
    """The backend the most recent HID verb in this process went through."""
    return _last_backend


def hid_mode(target: Target | None = None) -> str:
    aliases = getattr(target, "aliases", None) or {}
    mode = (aliases.get("ios_hid") or os.environ.get(HID_ENV) or "auto").strip().lower()
    if mode not in HID_MODES:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"{HID_ENV}={mode!r} is not a HID backend",
            "Use one of: " + ", ".join(HID_MODES) + ".",
        )
    return mode


def find_axe(target: Target | None = None) -> str | None:
    """AXe client: a Target alias, else AUTONOM_AXE, else PATH. None if absent."""
    aliases = getattr(target, "aliases", None) or {}
    explicit = aliases.get("axe") or os.environ.get(AXE_ENV)
    if explicit:
        return explicit
    return shutil.which("axe")


def _idb_hid_ready() -> bool:
    """Cached per process; only consulted for a PATH-resolved idb."""
    global _hid_probe
    if ios_idb.idb_is_overridden() or ios_idb.companion_endpoint():
        return True
    if _hid_probe is None:
        try:
            _hid_probe = ios_idb.hid_status()
        except Exception:  # noqa: BLE001 - a probe must never block input
            _hid_probe = {"ready": True}
    return bool(_hid_probe.get("ready", True))


def _mark_idb_hid_broken() -> None:
    global _hid_probe
    _hid_probe = {"ready": False, "reason": "idb answered ios_hid_framework_missing"}


def hid_backend(target: Target) -> str:
    """Which backend the next HID verb will use: ``idb`` or ``axe``."""
    mode = hid_mode(target)
    if mode == "idb":
        return "idb"
    axe = find_axe(target)
    if mode == "axe":
        if not axe:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"{HID_ENV}=axe but no axe binary was found",
                AXE_INSTALL_HINT,
            )
        return "axe"
    if axe and not _idb_hid_ready():
        return "axe"
    return "idb"


def run_axe(axe: str, args: list[str], *, udid: str, input_text: str | None = None,
            timeout: float = 30) -> subprocess.CompletedProcess:
    command = [axe, *args, "--udid", udid]
    try:
        completed = subprocess.run(
            command, input=input_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            text=True, check=False, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"axe not found at {axe}", AXE_INSTALL_HINT,
            backend="axe",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"axe {args[0]} timed out after {timeout:.0f}s",
            "Check the simulator is booted with 'autonom devices'.",
        ) from exc
    if completed.returncode != 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            (completed.stderr or completed.stdout or "").strip()[-400:]
            or f"axe {args[0]} failed ({completed.returncode})",
            "Check 'axe list-simulators'; run 'autonom doctor' for the whole environment.",
            backend="axe",
        )
    return completed


def _dispatch(target: Target, via_idb: Callable[[], None], axe_args: list[str],
              *, input_text: str | None = None) -> str:
    global _last_backend
    backend = hid_backend(target)
    if backend == "idb":
        try:
            via_idb()
        except errors.AutonomError as exc:
            axe = find_axe(target)
            if (exc.code != errors.IOS_HID_FRAMEWORK_MISSING or hid_mode(target) != "auto"
                    or not axe):
                raise
            _mark_idb_hid_broken()
            backend = "axe"
        else:
            _last_backend = "idb"
            return "idb"
    run_axe(find_axe(target) or "axe", axe_args, udid=target.target_id,
            input_text=input_text)
    _last_backend = backend
    return backend


def _seconds(value: float) -> str:
    return f"{float(value):g}"


def tap(target: Target, x: int, y: int, *, duration: float | None = None) -> str:
    """Tap (or, with ``duration`` seconds, long-press). Returns the backend."""
    if duration is not None:
        axe_args = ["touch", "-x", str(x), "-y", str(y), "--down", "--up",
                    "--delay", _seconds(duration)]
    else:
        axe_args = ["tap", "-x", str(x), "-y", str(y)]
    return _dispatch(target, lambda: ios_idb.tap(target, x, y, duration=duration), axe_args)


def swipe(target: Target, x1: int, y1: int, x2: int, y2: int, duration: float) -> str:
    return _dispatch(
        target,
        lambda: ios_idb.swipe(target, x1, y1, x2, y2, duration),
        ["swipe", "--start-x", str(x1), "--start-y", str(y1),
         "--end-x", str(x2), "--end-y", str(y2), "--duration", _seconds(duration)],
    )


def type_text(target: Target, text: str) -> str:
    # Through stdin, so text that starts with '-' is not read as a flag and
    # the typed value never shows up in a process listing.
    return _dispatch(target, lambda: ios_idb.text(target, text), ["type", "--stdin"],
                     input_text=text)


def press_key(target: Target, key: str) -> str:
    """Named hardware buttons, or a numeric HID keycode.

    Android `KEYCODE_*` names are rejected with the valid list rather than
    silently doing nothing — and iOS genuinely has no global Back button, which
    the message says so an agent taps the navigation control instead.
    """
    upper = key.upper()
    if upper in ios_idb.BUTTONS:
        return _dispatch(target, lambda: ios_idb.button(target, upper),
                         ["button", upper.lower().replace("_", "-")])
    if key.isdigit():
        return _dispatch(target, lambda: ios_idb.key(target, key), ["key", key])
    hint = "Valid iOS buttons: " + ", ".join(ios_idb.BUTTONS) + "; or a numeric HID keycode."
    if upper.startswith("KEYCODE_"):
        hint += (" iOS has no global Back button — tap the navigation bar's back control "
                 "found via 'ui find --desc Back'.")
    raise errors.AutonomError(
        errors.UNSUPPORTED_KEY_FOR_PLATFORM,
        f"'{key}' is not an iOS key or button",
        hint,
    )


def gesture(target: Target, name: str, **kwargs: Any) -> None:
    ios_idb.gesture(target, name, **kwargs)


def screenshot(target: Target, output: Path) -> Path:
    return ios_simctl.screenshot(target.tool, target.target_id, output)
