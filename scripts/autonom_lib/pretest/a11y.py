"""Accessibility checks on the current UI tree (tp-025).

``run`` reads the device's whole tree once (``ui.snapshot``), saves it under
the session's ``trees/`` like ``autonom ui tree`` does (the pre-test and the
open check named in it), and evaluates three rules on the interactive nodes
of the screen:

- ``unlabeled`` — a control with no label: no ``text``, ``desc`` or ``hint``
  of its own and none on any node inside it (Android merges a control's
  children into what a screen reader announces);
- ``small_target`` — a control smaller than 48 x 48 dp on Android or
  44 x 44 pt on iOS, with ``SIZE_TOLERANCE`` (0.5 dp or pt) for rounding
  (a 47.6 dp target is not flagged). Android bounds are pixels: dp = px * 160 / density, with
  the density read from the device (``wm density``, else the
  ``ro.sf.lcd_density`` property). When no density can be read the rule is
  skipped with a ``density_unknown`` warning; it never guesses. iOS bounds
  are already points;
- ``duplicate_label`` — two or more controls on one screen announcing the
  same label (case and spacing ignored); a control nested in another control
  of the same group is not counted twice.

A control is a visible node with an area that is clickable, long-clickable or
checkable, or that looks interactive without saying so: its role or class is a
button, link, switch, checkbox, radio, slider or text field (Android class
names such as ``android.widget.ImageButton``, iOS ``XCUIElementType…`` types
and ``AX…`` roles), or it carries an action trait (``traits`` naming a button,
link, adjustable or search field, or custom ``actions``). On Android, nodes of
another package (the status bar, the keyboard) are left out.

Each problem becomes an automatic finding: ``origin: "auto"``, ``source:
"a11y"``, ``severity: "minor"``, ``confirmed: false``, the node bounds in the
text and in ``data``, and the saved tree as evidence (``tree:<file>``). The
finding goes to ``--check`` (which must exist), else to the open check, else to
no check. A finding is written once per check, screen and problem
(``data.key``), so running the check again on the same screen adds nothing.
Contrast, focus order and screen-reader order are out of scope: they need
more than the tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .. import adb as adb_mod
from .. import errors
from .. import ui as ui_mod
from ..atlas import fingerprint as fingerprint_mod
from . import checks as checks_mod
from . import findings as findings_mod
from . import store

RULES = ("unlabeled", "small_target", "duplicate_label")
MIN_DP = 48
MIN_PT = 44
# Pixel bounds converted to dp land a little under the minimum for a target
# that is really 48 dp (47.6); only a shortfall past this counts.
SIZE_TOLERANCE = 0.5
SEVERITY = "minor"
SOURCE = "a11y"
LABEL_LIMIT = 120
MAX_FINDINGS = 200
CONTROL_ROLES = frozenset({"textfield", "securetextfield", "searchfield", "switch",
                           "checkbox", "radio", "slider", "link", "button"})
# Short Android class names (the part after the last dot) of controls that
# need not say clickable: Compose and Flutter often leave the flag off.
CONTROL_CLASSES = frozenset({
    "button", "imagebutton", "materialbutton", "appcompatbutton", "appcompatimagebutton",
    "floatingactionbutton", "extendedfloatingactionbutton", "togglebutton", "compoundbutton",
    "chip", "switch", "switchcompat", "switchmaterial", "materialswitch", "checkbox",
    "appcompatcheckbox", "materialcheckbox", "checkedtextview", "radiobutton",
    "appcompatradiobutton", "materialradiobutton", "seekbar", "appcompatseekbar", "slider",
    "ratingbar", "edittext", "appcompatedittext", "textinputedittext",
    "autocompletetextview", "multiautocompletetextview", "searchview",
})
# Role words, after an ``XCUIElementType``/``AX`` prefix and case are dropped.
CONTROL_ROLE_WORDS = frozenset({
    "button", "link", "switch", "toggle", "checkbox", "radio", "radiobutton", "slider",
    "textfield", "securetextfield", "searchfield", "textarea", "edittext", "togglebutton",
    "menubutton", "popupbutton", "incrementor", "stepper", "segmentedcontrol",
})
# UIAccessibilityTraits (and their AX spellings) that mean a person can act on the node.
ACTION_TRAITS = frozenset({"button", "link", "adjustable", "searchfield", "keyboardkey",
                           "togglebutton", "allowsdirectinteraction"})
_ROLE_PREFIX = re.compile(r"^(xcuielementtype|uiaccessibilitytrait|ax)", re.IGNORECASE)
LABEL_FIELDS = ("text", "desc", "hint")
SYSTEM_PACKAGES = ("com.android.systemui", "com.google.android.inputmethod",
                   "com.android.inputmethod")
_DENSITY = re.compile(r"(Physical|Override) density:\s*(\d+)")
_SPACE = re.compile(r"\s+")


# --- density -------------------------------------------------------------------------


def parse_density(text: str) -> int | None:
    """``wm density`` output -> dpi; an override wins over the physical value."""
    found: dict[str, int] = {}
    for kind, value in _DENSITY.findall(text or ""):
        found[kind] = int(value)
    value = found.get("Override") or found.get("Physical")
    return value if value and value > 0 else None


def android_density(target: Any) -> int | None:
    """The device's dpi, or None when neither source answers."""
    try:
        completed = adb_mod.run_adb(target.tool, ["shell", "wm", "density"],
                                    serial=target.target_id, timeout=10, check=False)
        value = parse_density(completed.stdout if isinstance(completed.stdout, str) else "")
        if value:
            return value
        completed = adb_mod.run_adb(target.tool, ["shell", "getprop", "ro.sf.lcd_density"],
                                    serial=target.target_id, timeout=10, check=False)
        text = (completed.stdout if isinstance(completed.stdout, str) else "").strip()
        return int(text) if text.isdigit() and int(text) > 0 else None
    except (errors.AutonomError, OSError, ValueError, adb_mod.AdbError):
        return None


# --- rules ---------------------------------------------------------------------------


def _box(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    bounds = node.get("bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(value) for value in bounds)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _own_label(node: dict[str, Any]) -> str:
    for field in LABEL_FIELDS:
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            return _SPACE.sub(" ", value.strip())
    return ""


def is_control(node: dict[str, Any], *, platform: str, app_id: str | None) -> bool:
    if node.get("visible") is False or _box(node) is None:
        return False
    package = node.get("package") or ""
    if platform == "android" and package:
        if any(package.startswith(system) for system in SYSTEM_PACKAGES):
            return False
        if app_id and package != app_id:
            return False
    return bool(node.get("clickable") or node.get("long_clickable") or node.get("checkable")
                or (node.get("role") or "") in CONTROL_ROLES or looks_interactive(node))


def _word(value: Any) -> str:
    """A role, type or trait name as a bare lower-case word: ``XCUIElementTypeButton``,
    ``AXButton`` and ``button`` are all ``button``."""
    text = str(value or "").strip()
    return _ROLE_PREFIX.sub("", text).replace("_", "").replace(" ", "").lower()


def _trait_words(traits: Any) -> set[str]:
    if isinstance(traits, str):
        items: list[Any] = re.split(r"[\s,|]+", traits)
    elif isinstance(traits, (list, tuple, set, frozenset)):
        items = list(traits)
    else:
        return set()
    return {_word(item) for item in items if isinstance(item, str) and item.strip()}


def looks_interactive(node: dict[str, Any]) -> bool:
    """A node that is a control by its role, class or traits, whatever its
    clickable flag says (an iOS button, a link, a Compose button left
    non-clickable, a node with an action trait)."""
    if _word(node.get("role")) in CONTROL_ROLE_WORDS:
        return True
    name = str(node.get("class") or "").strip()
    if name:
        short = name.rsplit(".", 1)[-1].rsplit("$", 1)[-1].lower()
        if short in CONTROL_CLASSES or _word(name) in CONTROL_ROLE_WORDS:
            return True
    if _trait_words(node.get("traits")) & ACTION_TRAITS:
        return True
    actions = node.get("actions")
    return isinstance(actions, (list, tuple)) and any(actions)


def _children(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        parent = node.get("parent")
        if parent:
            out.setdefault(str(parent), []).append(node)
    return out


def _ancestors(node: dict[str, Any], by_ref: dict[str, dict[str, Any]]) -> list[str]:
    out: list[str] = []
    parent = node.get("parent")
    while parent and parent in by_ref and parent not in out:
        out.append(parent)
        parent = by_ref[parent].get("parent")
    return out


def effective_label(node: dict[str, Any], children: dict[str, list[dict[str, Any]]]) -> str:
    """The node's own label, else the first labels inside it (what a screen
    reader announces for a merged control)."""
    own = _own_label(node)
    if own:
        return own
    found: list[str] = []
    stack = list(reversed(children.get(str(node.get("ref")), [])))
    seen: set[int] = set()
    while stack and len(found) < 3:
        child = stack.pop()
        if id(child) in seen:
            continue
        seen.add(id(child))
        label = _own_label(child)
        if label and label not in found:
            found.append(label)
        stack.extend(reversed(children.get(str(child.get("ref")), [])))
    return " ".join(found)


def _clip(text: str) -> str:
    return store.clip(text, LABEL_LIMIT) or ""


def evaluate(nodes: list[dict[str, Any]], platform: str, *, density: float | None = None,
             app_id: str | None = None) -> dict[str, Any]:
    """``{"controls": N, "issues": [...], "warnings": [...]}`` for one tree.

    Each issue: ``{"rule", "refs", "bounds": [[x1, y1, x2, y2], ...], "label"?,
    "size"?: {"width", "height", "unit", "minimum"}}``."""
    controls = [node for node in nodes if is_control(node, platform=platform, app_id=app_id)]
    children = _children(nodes)
    by_ref = {str(node.get("ref")): node for node in nodes if node.get("ref")}
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    labels: dict[int, str] = {id(node): effective_label(node, children) for node in controls}
    for node in controls:
        if not labels[id(node)]:
            issues.append({"rule": "unlabeled", "refs": [node.get("ref")],
                           "bounds": [list(_box(node))], "role": node.get("role"),
                           "resource_id": node.get("resource_id")})
    if platform == "android" and not density:
        warnings.append({
            "code": "density_unknown",
            "error": "the device reported no screen density; tap-target sizes were not checked",
            "hint": "Check 'adb shell wm density' on the device and run 'autonom pretest a11y' "
                    "again."})
    else:
        scale = (density / 160.0) if platform == "android" else 1.0
        unit, minimum = ("dp", MIN_DP) if platform == "android" else ("pt", MIN_PT)
        for node in controls:
            x1, y1, x2, y2 = _box(node)  # type: ignore[misc]
            width, height = (x2 - x1) / scale, (y2 - y1) / scale
            if width + SIZE_TOLERANCE < minimum or height + SIZE_TOLERANCE < minimum:
                issues.append({
                    "rule": "small_target", "refs": [node.get("ref")], "bounds": [[x1, y1, x2, y2]],
                    "label": _clip(labels[id(node)]) or None,
                    "size": {"width": round(width, 1), "height": round(height, 1),
                             "unit": unit, "minimum": minimum}})
    groups: dict[str, list[dict[str, Any]]] = {}
    for node in controls:
        label = labels[id(node)]
        if label:
            groups.setdefault(label.casefold(), []).append(node)
    for members in groups.values():
        refs = {str(node.get("ref")) for node in members if node.get("ref")}
        outer = [node for node in members
                 if not any(ancestor in refs for ancestor in _ancestors(node, by_ref))]
        if len(outer) < 2:
            continue
        issues.append({"rule": "duplicate_label", "refs": [node.get("ref") for node in outer],
                       "bounds": [list(_box(node)) for node in outer],
                       "label": _clip(labels[id(outer[0])])})
    return {"controls": len(controls), "issues": issues, "warnings": warnings}


# --- tree evidence ----------------------------------------------------------------------


def save_tree(session: dict[str, Any], target: Any, nodes: list[dict[str, Any]]) -> Path:
    """The tree as ``trees/NNNN_HHMMSS.json`` (and ``latest.json``), shaped
    like ``ui tree``'s saved history, owner-only, naming the pre-test."""
    directory = Path(session["artifacts_dir"]) / "trees"
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"ok": True, "source": "device", "target": target.target_id,
                               "count": len(nodes), "nodes": nodes, **target.identity(),
                               "purpose": "pretest a11y"}
    attribution = store.attribution(session)
    if attribution:
        payload.update(attribution)
        if attribution.get("check_id"):
            payload["task"] = attribution["check_id"]
    blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    stamp = time.strftime("%H%M%S", time.gmtime())
    seq = len(sorted(directory.glob("[0-9]*.json"))) + 1
    path = directory / f"{seq:04d}_{stamp}.json"
    while path.exists():
        seq += 1
        path = directory / f"{seq:04d}_{stamp}.json"
    for out in (path, directory / "latest.json"):
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(blob)
        os.chmod(out, 0o600)
    return path


# --- findings ---------------------------------------------------------------------------


def _box_text(bounds: list[list[int]]) -> str:
    return ", ".join(f"[{b[0]},{b[1]}][{b[2]},{b[3]}]" for b in bounds)


def _finding(issue: dict[str, Any]) -> dict[str, Any]:
    rule, where = issue["rule"], _box_text(issue["bounds"])
    if rule == "unlabeled":
        role = issue.get("role") or "control"
        return {"title": f"Unlabelled {role} at {where}",
                "expected": "Every control has a label a screen reader can announce.",
                "actual": f"The {role} at {where} has no text, description or hint, "
                          "and nothing inside it has one."}
    if rule == "small_target":
        size = issue["size"]
        name = f" “{issue['label']}”" if issue.get("label") else ""
        return {"title": f"Tap target{name} is {size['width']:g}x{size['height']:g} "
                         f"{size['unit']}, below {size['minimum']} {size['unit']}",
                "expected": f"Every control is at least {size['minimum']} x "
                            f"{size['minimum']} {size['unit']}.",
                "actual": f"The control{name} at {where} measures {size['width']:g} x "
                          f"{size['height']:g} {size['unit']}."}
    count = len(issue["refs"])
    return {"title": f"{count} controls share the label “{issue['label']}”",
            "expected": "Controls on one screen announce distinct labels.",
            "actual": f"{count} controls labelled “{issue['label']}” at {where}."}


def _key(check_id: str | None, screen: str, issue: dict[str, Any]) -> str:
    detail = json.dumps([issue["rule"], issue["bounds"], issue.get("label")],
                        ensure_ascii=False)
    digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()[:16]
    return f"a11y:{check_id or '-'}:{screen}:{issue['rule']}:{digest}"


def target_check(session: dict[str, Any], record: dict[str, Any],
                 check_id: str | None) -> str | None:
    """``--check`` (``unknown_check`` when the pre-test has no such check),
    else the open check, else None."""
    states = checks_mod.fold(session, record)
    if check_id is not None:
        if check_id not in {item["check_id"] for item in states}:
            raise errors.AutonomError(
                errors.UNKNOWN_CHECK, f"no check {check_id!r} in this pre-test",
                hint="List checks with 'autonom pretest check list', or add one with "
                     "'autonom pretest check add \"...\" --kind a11y'.", check_id=check_id)
        return check_id
    opened = next((item for item in states if item["state"] == "open"), None)
    return opened["check_id"] if opened else None


def run(session: dict[str, Any], record: dict[str, Any], target: Any, *,
        check_id: str | None = None, density: float | None = None) -> dict[str, Any]:
    """Evaluate the current screen and write its findings (see the module text)."""
    store.ensure_mutable(record)
    chosen = target_check(session, record, check_id)
    nodes = ui_mod.snapshot(target)
    if target.platform == "android" and density is None:
        density = android_density(target)
    app_id = record.get("app_id")
    result = evaluate(nodes, target.platform, density=density, app_id=app_id)
    try:
        tree = save_tree(session, target, nodes)
    except OSError as exc:
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"the tree could not be saved under trees/ ({exc.strerror or exc})",
            hint="Check the session's artifacts folder is writable, then run "
                 "'autonom pretest a11y' again; nothing was written.") from None
    ref = f"tree:{tree.name}"
    screen = fingerprint_mod.fingerprint(nodes)["structure"]
    written: list[dict[str, Any]] = []
    repeated: list[str] = []
    with store.lock(session, record["pretest_id"]):
        current = store.load(session, record["pretest_id"])
        store.ensure_mutable(current)
        seen = {str((item.get("data") or {}).get("key"))
                for item in findings_mod.fold(session, current)
                if (item.get("data") or {}).get("key")}
        for issue in result["issues"][:MAX_FINDINGS]:
            key = _key(chosen, screen, issue)
            issue["key"] = key
            if key in seen:
                repeated.append(key)
                continue
            text = _finding(issue)
            data = {"key": key, "rule": issue["rule"], "bounds": issue["bounds"],
                    "refs": issue["refs"], "screen_id": screen, "platform": target.platform}
            for name in ("label", "size", "role", "resource_id"):
                if issue.get(name) is not None:
                    data[name] = issue[name]
            finding = findings_mod.add(
                session, current, check_id=chosen, severity=SEVERITY, title=text["title"],
                actual=text["actual"], expected=text["expected"], evidence=[ref],
                origin="auto", source=SOURCE, confirmed=False, data=data)
            seen.add(key)
            written.append(finding)
    out: dict[str, Any] = {
        "check_id": chosen, "platform": target.platform, "screen_id": screen,
        "tree": str(tree.relative_to(Path(session["artifacts_dir"]))), "evidence": ref,
        "density": density if target.platform == "android" else None,
        "controls": result["controls"],
        "counts": {rule: sum(1 for issue in result["issues"] if issue["rule"] == rule)
                   for rule in RULES},
        "issues": result["issues"], "findings": written, "already_reported": len(repeated),
    }
    if len(result["issues"]) > MAX_FINDINGS:
        out["omitted"] = len(result["issues"]) - MAX_FINDINGS
    if result["warnings"]:
        out["warnings"] = result["warnings"]
    return out
