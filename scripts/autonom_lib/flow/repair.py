"""A structured hand-off when a flow fails on the device.

Flows replay without a model: when the app moves a button or renames a label,
the run fails loudly and somebody has to fix the YAML. The bare `failure`
block — code, line, message — says what broke but not what to do next. The
`repair` block attached to a `flow run` summary is the JSON-shaped version of
the "to repair" script that screenshot-automation tools built on the same
replay-without-a-model idea print on failure: reconstruct the state the step
assumed, inspect what is on screen now, edit, re-verify.

Nothing repairs itself. The corrected flow is a reviewed edit; the brief only
names the commands that make the edit an informed one.
"""
from __future__ import annotations

import shlex
from typing import Any

from .. import errors

TEST_FAILURE = "test_failure"

# Flow selector field -> the `ui find` flag that queries the same thing.
_FIELD_FLAGS = (
    ("id", "--resource-id"),
    ("resource_id", "--resource-id"),
    ("text", "--text"),
    ("description", "--desc"),
    ("desc", "--desc"),
    ("role", "--role"),
)

_ADVICE = {
    errors.NO_MATCHING_NODE: (
        "The element the step targets was not on screen when the step ran. "
        "Dump the tree, find the label or identifier it carries now, and update "
        "the selector — prefer id, then description on iOS / text on Android."
    ),
    errors.FLOW_ASSERTION_TIMEOUT: (
        "The asserted state never held within timeoutMs. Reconstruct the state "
        "with --until-step, dump the tree, and check whether the element renders "
        "under another label or simply later — raise timeoutMs only when the "
        "tree proves it arrives late."
    ),
    errors.AMBIGUOUS_SELECTOR: (
        "More than one node matched, so the mutation refused rather than tap the "
        "wrong one. Tighten the selector with id or role, or add index for a "
        "justified duplicate."
    ),
    errors.SELECTOR_INDEX_OUT_OF_RANGE: (
        "Fewer nodes matched than the selector's index expects. Re-count the "
        "matches with --all and fix the index, or drop it for a unique selector."
    ),
    errors.COORDINATE_SPACE_MISMATCH: (
        "The step's coordinates fall outside the target's screen. Recompute them "
        "from the tree's bounds (points on iOS, never pixels) or, better, replace "
        "the coordinates with a selector."
    ),
}
_DEFAULT_ADVICE = (
    "Read the failing step's screenshot and hierarchy from the run's events, "
    "then fix the flow file; re-running an unchanged flow proves nothing."
)


def selector_flags(selector: dict[str, Any] | None) -> list[str]:
    """`ui find` flags that query the same fields the flow selector named.

    The match mode is deliberately widened to `contains` and `--all` is added:
    the point of the query is to see what is on screen *near* the old
    selector, not to reproduce the exact miss.
    """
    if not isinstance(selector, dict):
        return []
    flags: list[str] = []
    seen: set[str] = set()
    for field, flag in _FIELD_FLAGS:
        value = selector.get(field)
        if value is None or flag in seen:
            continue
        seen.add(flag)
        flags.extend([flag, shlex.quote(str(value))])
    if not flags:
        return []
    return flags + ["--mode", "contains", "--all"]


def repair_brief(flow_path: str, failure: dict[str, Any] | None,
                 steps: list[dict[str, Any]] | None = None, *,
                 events_path: str | None = None) -> dict[str, Any] | None:
    """The hand-off for a test failure; None when there is nothing to repair.

    Definition and infrastructure failures already abort with their own
    envelope and hint, so only a *test* failure at a known step gets a brief.
    """
    if not failure or failure.get("step_index") is None:
        return None
    if failure.get("failure_class") not in (None, TEST_FAILURE):
        return None
    index = int(failure["step_index"])
    step = next((item for item in (steps or []) if item.get("index") == index), None)
    selector = (step or {}).get("selector")
    quoted = shlex.quote(str(flow_path))

    commands: list[str] = []
    if index > 1:
        commands.append(f"autonom flow run {quoted} --until-step {index - 1}")
    commands.append("autonom ui tree")
    flags = selector_flags(selector)
    if flags:
        commands.append("autonom ui find " + " ".join(flags))
    commands.append(
        "autonom screenshot --label " + shlex.quote(
            f"repair {failure.get('command') or 'step'} line {failure.get('line') or '?'}"))
    commands.append(f"autonom flow check {quoted}")
    commands.append(f"autonom flow run {quoted}")

    brief: dict[str, Any] = {
        "step_index": index,
        "command": failure.get("command"),
        "line": failure.get("line"),
        "flow": flow_path,
        "selector": selector,
        "commands": commands,
        "advice": _ADVICE.get(failure.get("error_code") or "", _DEFAULT_ADVICE),
        "note": "The corrected flow is a reviewed edit, never an automatic rewrite.",
    }
    if events_path:
        brief["evidence"] = events_path
    return brief
