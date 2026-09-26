"""A structured hand-off when a flow fails on the device.

Flows replay without a model: when the app moves a button or renames a label,
the run fails loudly and somebody has to fix the YAML. The bare `failure`
block — code, line, message — says what broke but not what to do next. The
`repair` block attached to a `flow run` summary is the JSON-shaped version of
the "to repair" script that screenshot-automation tools built on the same
replay-without-a-model idea print on failure: reconstruct the state the step
assumed, inspect what is on screen now, edit, re-verify.

Nothing repairs itself. The corrected flow is a reviewed edit; the brief only
names the commands that make the edit an informed one — and, when the failing
step's hierarchy was captured, ranks what *was* on screen as `candidates`
(ready-to-paste selectors, never applied).
"""
from __future__ import annotations

import difflib
import json
import shlex
from pathlib import Path
from typing import Any

from .. import errors
from .. import selector as selector_engine

TEST_FAILURE = "test_failure"

# Flow selector field -> the `ui find` flag that queries the same thing.
# visibleText matches across text and description (`selector.LABEL_SOURCES`);
# `ui find` has no single flag for that union, so a visibleText selector is
# queried once per label source (see `selector_flag_sets`).
_FIELD_FLAGS = (
    ("id", "--resource-id"),
    ("text", "--text"),
    ("description", "--desc"),
    ("role", "--role"),
)
_BOOL_FLAGS = (("enabled", "--enabled"),)
_VISIBLE_TEXT_FLAGS = ("--text", "--desc")

# Candidate ranking (FLOW-002): difflib ratio against the failed selector's
# strings, a small bonus when the node's role agrees, a floor below which a
# node is noise, and a cap so the brief stays readable.
_CANDIDATE_LIMIT = 5
_CANDIDATE_THRESHOLD = 0.5
_ROLE_BONUS = 0.1
# flow field -> compact-node keys it is compared against
_CANDIDATE_SOURCES = {
    "text": ("text", "desc"),
    "visibleText": ("text", "desc", "hint"),
    "description": ("desc", "text"),
    "id": ("resource_id",),
}
# compact-node key -> the flow field a candidate selector uses for it
_NODE_FIELD = {"text": "text", "desc": "description", "hint": "visibleText",
               "resource_id": "id"}
_FLOW_TO_ENGINE = {"id": "resource_id", "text": "text",
                   "visibleText": "visible_text", "description": "desc",
                   "role": "role"}
_FLOW_BOOLS = ("enabled", "checked", "selected", "focused")
_ENGINE_MODES = {"exact": ("exact", True), "caseInsensitiveExact": ("exact", False),
                 "contains": ("contains", True), "regex": ("regex", True)}
_NODE_SUMMARY = ("ref", "role", "text", "desc", "resource_id", "bounds")

_SELECTOR_MISS = (
    "The element the step targets was not on screen within timeoutMs. "
    "Reconstruct the state with --until-step, dump the tree, find the label "
    "or identifier it carries now (candidates ranks what was on screen), and "
    "update the selector — prefer id, then description on iOS / text on "
    "Android. Raise timeoutMs only when the tree proves the element arrives "
    "late."
)
_ADVICE = {
    # Flows reach this only through a relational anchor that is off-screen
    # on a tap; a plain miss polls until flow_assertion_timeout.
    errors.NO_MATCHING_NODE: (
        "The element the step targets (or its relational anchor) was not on "
        "screen when the step ran. Dump the tree, find the label or identifier "
        "it carries now, and update the selector — prefer id, then description "
        "on iOS / text on Android."
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
        "justified duplicate — candidates lists every match with the fields "
        "that tell them apart."
    ),
    # Reached by taps; an assertion treats a missing occurrence as absent.
    errors.SELECTOR_INDEX_OUT_OF_RANGE: (
        "Fewer nodes matched than the tap's index expects. Re-count the "
        "matches with --all and fix the index, or drop it for a unique selector."
    ),
    errors.COORDINATE_SPACE_MISMATCH: (
        "The step's coordinates fall outside the target's screen. Recompute them "
        "from the tree's bounds (points on iOS, never pixels) or, better, replace "
        "the coordinates with a selector."
    ),
    errors.FLOW_NO_FOCUSED_FIELD: (
        "inputText found no focused field within timeoutMs, so the text would "
        "have gone nowhere. Replay to the step before, find in the tree the "
        "field that should hold focus, and tapOn it (then waitUntil it is "
        "visible) before typing; use requireFocus: false only for a UI that "
        "never reports focus."
    ),
    errors.FLOW_COPY_EMPTY: (
        "copyTextFrom matched a node that carries neither text nor description. "
        "Point the selector at the node that shows the value — the tree names it."
    ),
}
_DEFAULT_ADVICE = (
    "Read the failing step's screenshot and hierarchy from the run's events, "
    "then fix the flow file; re-running an unchanged flow proves nothing."
)


def _string_fields(selector: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(selector, dict):
        return {}
    return {name: value for name, value in selector.items()
            if name in _FLOW_TO_ENGINE and isinstance(value, str) and value}


def _advice(code: str, selector: dict[str, Any] | None) -> str:
    # What flows actually produce for "the element is not there" is a
    # polling timeout on a selector, not no_matching_node.
    if code == errors.FLOW_ASSERTION_TIMEOUT and _string_fields(selector):
        return _SELECTOR_MISS
    return _ADVICE.get(code, _DEFAULT_ADVICE)


def _mode_flags(selector: dict[str, Any]) -> list[str]:
    # A regex stays a regex: widening `Sett.*` to a literal `contains`
    # search would look for the characters ".*" and find nothing. Flow
    # regex matching is case-sensitive, so the query is too.
    if selector.get("match") == "regex":
        return ["--mode", "regex", "--case-sensitive", "--all"]
    return ["--mode", "contains", "--all"]


def selector_flag_sets(selector: dict[str, Any] | None) -> list[list[str]]:
    """One `ui find` flag list per query needed to cover the selector.

    Every Flow selector string field maps onto its `ui find` flag.
    ``visibleText`` matches text *or* description, which `ui find` cannot
    express in one query, so it yields one query per label source.
    """
    if not isinstance(selector, dict):
        return []
    base: list[str] = []
    for field, flag in _FIELD_FLAGS:
        value = selector.get(field)
        if value is None:
            continue
        base.extend([flag, shlex.quote(str(value))])
    has_string = bool(base)
    for field, flag in _BOOL_FLAGS:
        value = selector.get(field)
        if isinstance(value, bool):
            base.extend([flag, "true" if value else "false"])
    visible = selector.get("visibleText")
    sets: list[list[str]] = []
    if visible is not None:
        taken = set(base[0::2])
        for flag in _VISIBLE_TEXT_FLAGS:
            if flag not in taken:
                sets.append(base + [flag, shlex.quote(str(visible))])
    elif has_string:
        sets.append(base)
    return [flags + _mode_flags(selector) for flags in sets]


def selector_flags(selector: dict[str, Any] | None) -> list[str]:
    """`ui find` flags that query the same fields the flow selector named.

    The match mode is deliberately widened to `contains` and `--all` is added:
    the point of the query is to see what is on screen *near* the old
    selector, not to reproduce the exact miss. A regex selector keeps
    `--mode regex`. For visibleText this is the first of `selector_flag_sets`.
    """
    sets = selector_flag_sets(selector)
    return sets[0] if sets else []


# -- replay prefix ------------------------------------------------------------


def replay_prefix(steps: list[dict[str, Any]] | None,
                  index: int) -> tuple[int | None, str | None]:
    """(``--until-step`` value, reason when there is none) for a failure.

    ``steps`` is the run timeline in completion order. A composite runFlow
    outcome completes *after* its children, so every ancestor of the failed
    step sits after it in the list, and group/retry/repeat spans are not in
    it at all; the prefix end is therefore the last step completed before
    the failed one. ``index - 1`` was wrong whenever the failed step was the
    first child of a block: that number is the block itself, which a prefix
    replay never stops at before its children ran.
    """
    if not steps:
        return None, ("no run timeline was available, so the step before the "
                      "failure cannot be named")
    position = next((i for i, item in enumerate(steps)
                     if item.get("index") == index), None)
    if position is None:
        return None, f"step {index} is not in the run timeline"
    for item in reversed(steps[:position]):
        candidate = item.get("index")
        if not isinstance(candidate, int) or candidate >= index:
            continue
        if item.get("hook") == "onFlowComplete":
            continue
        if item.get("status", "passed") not in ("passed", "skipped"):
            continue  # a recovered retry attempt is not a state to stop in
        return candidate, None
    return None, ("the failed step is the first step that ran, so there is no "
                  "prefix to replay — the flow's start is the state it assumed")


# -- run context --------------------------------------------------------------


def _manifest(run_dir: Path | None) -> dict[str, Any]:
    if run_dir is None:
        return {}
    try:
        data = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _hierarchy(run_dir: Path | None, index: int) -> list[dict[str, Any]] | None:
    """The tree captured at the failing step, when evidence was collected."""
    if run_dir is None:
        return None
    path = run_dir / f"failure-step-{index}-hierarchy.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    nodes = data.get("nodes") if isinstance(data, dict) else data
    if not isinstance(nodes, list):
        return None
    return [node for node in nodes if isinstance(node, dict)]


def _run_flags(secret_names, env_names) -> str:
    """``--secret NAME`` / ``--env NAME=<value>`` placeholders, never values."""
    parts = [f"--secret {shlex.quote(name)}" for name in sorted(secret_names or ())]
    parts.extend(f"--env {shlex.quote(name)}=<value>"
                 for name in sorted(env_names or ()))
    return (" " + " ".join(parts)) if parts else ""


# -- candidates ---------------------------------------------------------------


def _ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    return {key: node.get(key) for key in _NODE_SUMMARY
            if node.get(key) not in (None, "")}


def _find_command(selector: dict[str, Any]) -> str:
    """The exact `ui find` query that confirms a candidate selector."""
    flags: list[str] = []
    for field, flag in _FIELD_FLAGS:
        if field in selector:
            flags.extend([flag, shlex.quote(str(selector[field]))])
    if "visibleText" in selector:
        flags.extend(["--text", shlex.quote(str(selector["visibleText"]))])
    for field, flag in _BOOL_FLAGS:
        if isinstance(selector.get(field), bool):
            flags.extend([flag, "true" if selector[field] else "false"])
    mode, case_sensitive = _ENGINE_MODES.get(selector.get("match") or "exact",
                                             ("exact", True))
    flags.extend(["--mode", mode])
    if case_sensitive:
        flags.append("--case-sensitive")
    if "index" in selector:
        flags.extend(["--index", str(selector["index"])])
    return "autonom ui find " + " ".join(flags)


def _engine_query(selector: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
    keys: dict[str, Any] = {engine: selector[flow]
                            for flow, engine in _FLOW_TO_ENGINE.items()
                            if isinstance(selector.get(flow), str)}
    for name in _FLOW_BOOLS:
        if isinstance(selector.get(name), bool):
            keys[name] = selector[name]
    mode, case_sensitive = _ENGINE_MODES.get(selector.get("match") or "exact",
                                             ("exact", True))
    return keys, mode, case_sensitive


def _matching(nodes: list[dict[str, Any]], selector: dict[str, Any]) -> list[dict[str, Any]]:
    keys, mode, case_sensitive = _engine_query(selector)
    try:
        return selector_engine.filter_nodes(nodes, keys, mode=mode,
                                            case_sensitive=case_sensitive)
    except (errors.AutonomError, ValueError):
        return []


def _ambiguous_candidates(selector: dict[str, Any],
                          nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every node the selector matched, with the fields that tell them apart."""
    matches = _matching(nodes, selector)
    base = {name: value for name, value in selector.items()
            if name in _FLOW_TO_ENGINE or name in _FLOW_BOOLS or name == "match"}
    out: list[dict[str, Any]] = []
    for position, node in enumerate(matches):
        distinguishing = {
            key: node[key] for key in ("resource_id", "text", "desc", "role")
            if node.get(key)
            and sum(1 for other in matches if other.get(key) == node[key]) == 1}
        candidate = dict(base)
        if "resource_id" in distinguishing and "id" not in base:
            candidate["id"] = distinguishing["resource_id"]
        elif "role" in distinguishing and "role" not in base:
            candidate["role"] = distinguishing["role"]
        else:
            candidate["index"] = position
        out.append({
            "selector": candidate,
            "command": _find_command(candidate),
            "score": 1.0,
            "match_position": position,
            "distinguishing": distinguishing,
            "node": _node_summary(node),
        })
    return out


def rank_candidates(selector: dict[str, Any] | None,
                    nodes: list[dict[str, Any]] | None,
                    *, error_code: str | None = None) -> list[dict[str, Any]]:
    """Up to five on-screen nodes most like the failed selector (FLOW-002).

    Scored with ``difflib.SequenceMatcher`` (case-folded) against the
    selector's text, visibleText, description, and id, plus a bonus when the
    role agrees. Each entry is a ready-to-paste Flow selector (exact match)
    and the exact ``ui find`` query that confirms it; a label shared by
    several nodes gets the ``index`` that picks this one. For an ambiguous
    selector every match is listed instead, with its distinguishing fields.
    """
    if not nodes or not isinstance(selector, dict):
        return []
    if error_code == errors.AMBIGUOUS_SELECTOR:
        return _ambiguous_candidates(selector, nodes)
    wanted = _string_fields(selector)
    wanted.pop("role", None)
    if not wanted:
        return []
    role = selector.get("role")
    scored: list[tuple[float, float, int, dict[str, Any]]] = []
    for order, node in enumerate(nodes):
        best: tuple[float, str] | None = None
        for field, expected in wanted.items():
            for key in _CANDIDATE_SOURCES[field]:
                actual = node.get(key)
                if not isinstance(actual, str) or not actual:
                    continue
                ratio = _ratio(expected, actual)
                if best is None or ratio > best[0]:
                    best = (ratio, key)
        if best is None or best[0] < _CANDIDATE_THRESHOLD:
            continue
        ratio, key = best
        candidate: dict[str, Any] = {_NODE_FIELD[key]: node[key]}
        bonus = 0.0
        if role and node.get("role") == role:
            candidate["role"] = role
            bonus = _ROLE_BONUS
        same = _matching(nodes, candidate)
        if len(same) > 1:
            # the suggestion must not be the next ambiguous_selector
            refs = [item.get("ref") for item in same]
            candidate["index"] = (refs.index(node.get("ref"))
                                  if node.get("ref") in refs else 0)
        score = round(min(ratio + bonus, 1.0), 3)
        scored.append((score, ratio, -order, {
            "selector": candidate,
            "command": _find_command(candidate),
            "score": score,
            "matched_field": _NODE_FIELD[key],
            "node": _node_summary(node),
        }))
    scored.sort(key=lambda item: item[:3], reverse=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for *_rank, entry in scored:
        identity = json.dumps(entry["selector"], sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(entry)
        if len(out) == _CANDIDATE_LIMIT:
            break
    return out


# -- brief --------------------------------------------------------------------


def repair_brief(flow_path: str, failure: dict[str, Any] | None,
                 steps: list[dict[str, Any]] | None = None, *,
                 events_path: str | None = None,
                 secret_names: list[str] | None = None,
                 env_names: list[str] | None = None,
                 hierarchy: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The hand-off for a test failure; None when there is nothing to repair.

    Definition and infrastructure failures already abort with their own
    envelope and hint, so only a *test* failure at a known step gets a brief.

    ``flow_path`` is the root flow — what `flow run` replays. The step itself
    may live in a runFlow child: ``failure["flow"]`` names that file, and the
    brief reports it as ``flow`` (the file to edit, whose ``line`` it is)
    next to ``root_flow``. ``secret_names``/``env_names``/``hierarchy``
    default to what the run wrote next to ``events_path`` (its manifest and
    the failing step's hierarchy).
    """
    if not failure or failure.get("step_index") is None:
        return None
    if failure.get("failure_class") not in (None, TEST_FAILURE):
        return None
    index = int(failure["step_index"])
    step = next((item for item in (steps or []) if item.get("index") == index), None)
    selector = (step or {}).get("selector")
    run_dir = Path(events_path).parent if events_path else None
    manifest = _manifest(run_dir)
    if secret_names is None:
        secret_names = manifest.get("secret_names") or []
    if env_names is None:
        env_names = manifest.get("env_override_names") or []
    if hierarchy is None:
        hierarchy = _hierarchy(run_dir, index)
    root_quoted = shlex.quote(str(flow_path))
    run_flags = _run_flags(secret_names, env_names)

    commands: list[str] = []
    until, until_reason = replay_prefix(steps, index)
    if until is not None:
        commands.append(
            f"autonom flow run {root_quoted} --until-step {until}{run_flags}")
    commands.append("autonom ui tree")
    for flags in selector_flag_sets(selector):
        commands.append("autonom ui find " + " ".join(flags))
    commands.append(
        "autonom screenshot --label " + shlex.quote(
            f"repair {failure.get('command') or 'step'} line {failure.get('line') or '?'}"))
    commands.append(f"autonom flow check {root_quoted}")
    commands.append(f"autonom flow run {root_quoted}{run_flags}")

    brief: dict[str, Any] = {
        "step_index": index,
        "command": failure.get("command"),
        "line": failure.get("line"),
        "flow": failure.get("flow") or flow_path,
        "root_flow": flow_path,
        "selector": selector,
        "until_step": until,
        "commands": commands,
        "advice": _advice(failure.get("error_code") or "", selector),
        "note": "The corrected flow is a reviewed edit, never an automatic rewrite.",
    }
    if until_reason:
        brief["until_step_reason"] = until_reason
    if hierarchy is not None:
        brief["candidates"] = rank_candidates(
            selector, hierarchy, error_code=failure.get("error_code"))
    if events_path:
        brief["evidence"] = events_path
    return brief
