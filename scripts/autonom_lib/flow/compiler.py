"""Session → Flow compiler (research doc §8): a verified journey becomes a
repeatable flow file.

Input is the session's journal plus the per-action detail records the
instrumented handlers write (``autonom_lib/actions.py``). The compiler is
deliberately conservative — §8.4's "never silently" list is the contract:

- an action whose provenance is missing (a coordinate tap, a pre-0.23
  session without detail records) is **skipped with a named warning**, never
  approximated;
- sensitive input (``ui type --sensitive``, or a value typed right after
  focusing a credential-shaped field) becomes ``${SECRET_n}`` — the value is
  not in the artifacts and cannot leak into the flow;
- a selector that was proven unique during the session is reused verbatim;
  an explicit ``--index`` carries over as an explicit ``index`` (it was the
  operator's stated choice, §7.9 item 6);
- the emitted file is parsed and built back before it is written, and the
  quality report explains the risk (§8.5) instead of hiding it.
"""
from __future__ import annotations

import re
from typing import Any

from .. import actions as actions_mod
from .. import errors
from .. import journal as journal_mod
from .canonical import emit_flow
from .parser import parse_document
from .schema import Flow, FlowSelector, Step, build_flow

_CREDENTIAL_HINT = re.compile(
    r"pass(word)?|pwd|pin\b|secret|token|otp|cvv|card", re.IGNORECASE)

_NOISE_VERBS = {
    "ui tree", "ui find", "journal", "shots", "devices", "doctor",
    "processes", "cleanup", "session show", "session stop", "session start",
    "network requests", "network status", "record start", "record stop",
    "flow", "crash", "file", "logs",
}


def _selector_from_detail(detail: dict[str, Any]) -> FlowSelector | None:
    raw = detail.get("selector") or {}
    fields: dict[str, Any] = {}
    source: dict[str, Any] = {}
    mapping = {"resource_id": ("resource_id", "id"), "text": ("text", "text"),
               "desc": ("desc", "description"), "role": ("role", "role")}
    for cli_name, (engine_key, flow_name) in mapping.items():
        value = raw.get(cli_name)
        if value is not None:
            fields[engine_key] = value
            source[flow_name] = value
    for bool_name in ("enabled", "checked", "selected", "focused", "clickable"):
        value = raw.get(bool_name)
        if value is not None and bool_name != "clickable":
            fields[bool_name] = bool(value)
            source[bool_name] = bool(value)
    if not (set(fields) & {"resource_id", "text", "desc", "role"}):
        return None
    mode = raw.get("mode", "contains")
    case_sensitive = bool(raw.get("case_sensitive", False))
    if mode == "exact":
        match = "exact" if case_sensitive else "caseInsensitiveExact"
    elif mode in ("contains", "regex"):
        match = mode  # flow contains/regex are case-sensitive; close enough
    else:
        return None
    selector = FlowSelector(fields=fields, match=match,
                            source_fields=source)
    if raw.get("index") is not None:
        selector.index = int(raw["index"])
    return selector


def _selector_from_node(node: dict[str, Any],
                        nodes: list[dict[str, Any]]) -> FlowSelector | None:
    """§7.9 priority over the recorded tree: id, unique text, unique desc."""
    def unique(key: str, value: str) -> bool:
        return sum(1 for other in nodes if other.get(key) == value) == 1

    resource_id = node.get("resource_id")
    if resource_id and unique("resource_id", resource_id):
        return FlowSelector(fields={"resource_id": resource_id},
                            source_fields={"id": resource_id})
    text = node.get("text")
    if text and unique("text", text):
        return FlowSelector(fields={"text": text}, source_fields={"text": text})
    desc = node.get("desc")
    if desc and unique("desc", desc):
        return FlowSelector(fields={"desc": desc},
                            source_fields={"description": desc})
    return None


def _looks_credential(previous_tap_node: dict[str, Any] | None) -> bool:
    if not previous_tap_node:
        return False
    for key in ("resource_id", "text", "desc"):
        value = previous_tap_node.get(key)
        if value and _CREDENTIAL_HINT.search(str(value)):
            return True
    return False


def compile_session(session: dict[str, Any], *, name: str | None = None,
                    task: str | None = None) -> tuple[Flow, dict[str, Any]]:
    """Journal + details -> (validated Flow, quality report)."""
    entries, _total = journal_mod.read(session, max_entries=10_000)
    details = actions_mod.read_details(session)

    flow = Flow(path="<generated>", name=name or f"Recorded {task or 'session'}",
                app_id=session.get("app_id"))
    if task:
        flow.tags = [task]
    warnings: list[dict[str, Any]] = []
    quality = {"selectors": {"id": 0, "text": 0, "description": 0,
                             "recorded": 0, "index": 0},
               "secrets": 0, "skipped": 0, "steps": 0}
    secret_count = 0
    last_tap_node: dict[str, Any] | None = None

    def warn(code: str, message: str, **extra: Any) -> None:
        warnings.append({"code": code, "error": message, **extra})
        quality["skipped"] += 1

    for entry in entries:
        if entry.get("kind") == "note":
            flow.steps.append(Step("note", {"text": entry.get("text", "")}))
            continue
        if entry.get("kind") != "action" or not entry.get("ok", False):
            continue
        verb = entry.get("verb", "")
        argv = entry.get("argv") or []
        result = entry.get("result") or {}
        detail = details.get(result.get("detail", ""))

        if verb == "session launch":
            flow.steps.append(Step("launchApp", {}))
            flow.app_id = flow.app_id or (argv[2] if len(argv) > 2 else None)
        elif verb == "session clear":
            flow.steps.append(Step("clearState", {}))
        elif verb == "session force-stop":
            flow.steps.append(Step("stopApp", {}))
        elif verb == "open":
            url = argv[-1] if argv else None
            if url:
                flow.steps.append(Step("openLink", {"url": url}))
        elif verb == "ui tap":
            if not detail or detail.get("coordinate"):
                warn("coordinate_tap_not_compilable",
                     "a coordinate tap has no selector to compile; re-record "
                     "it with a semantic selector",
                     seq=entry.get("seq"))
                last_tap_node = None
                continue
            selector = _selector_from_detail(detail)
            if selector is None and detail.get("node"):
                selector = _selector_from_node(detail["node"],
                                               detail.get("nodes") or [])
                if selector is not None:
                    quality["selectors"]["recorded"] += 1
            elif selector is not None:
                quality["selectors"]["recorded"] += 1
            if selector is None:
                warn("selector_not_recoverable",
                     "no stable selector could be derived for this tap",
                     seq=entry.get("seq"))
                last_tap_node = None
                continue
            if "resource_id" in selector.fields:
                quality["selectors"]["id"] += 1
            elif "text" in selector.fields:
                quality["selectors"]["text"] += 1
            elif "desc" in selector.fields:
                quality["selectors"]["description"] += 1
            if selector.index is not None:
                quality["selectors"]["index"] += 1
            args: dict[str, Any] = {"selector": selector}
            if detail.get("duration_ms"):
                flow.steps.append(Step("longPressOn",
                                       {**args,
                                        "durationMs": detail["duration_ms"]}))
            else:
                flow.steps.append(Step("tapOn", args))
            last_tap_node = detail.get("node")
        elif verb == "ui type":
            if not detail:
                warn("typed_text_not_recorded",
                     "this session predates typed-text detail records",
                     seq=entry.get("seq"))
                continue
            sensitive = bool(detail.get("sensitive")) or _looks_credential(
                last_tap_node)
            if sensitive or detail.get("text") is None:
                secret_count += 1
                variable = f"SECRET_{secret_count}"
                flow.steps.append(Step("inputText",
                                       {"value": f"${{{variable}}}",
                                        "sensitive": True}))
                quality["secrets"] += 1
            else:
                flow.steps.append(Step("inputText", {"value": detail["text"]}))
        elif verb == "ui key":
            key = argv[-1] if argv else None
            if key == "KEYCODE_BACK":
                flow.steps.append(Step("back", {}))
            elif key:
                flow.steps.append(Step("pressKey", {"key": key}))
        elif verb == "screenshot":
            label = None
            if "--label" in argv:
                position = argv.index("--label")
                if position + 1 < len(argv):
                    label = argv[position + 1]
            flow.steps.append(Step("takeScreenshot",
                                   {"label": label} if label else {}))
        elif verb == "location set":
            coordinates = argv[-1] if argv else ""
            if "," in coordinates:
                latitude, longitude = coordinates.split(",", 1)
                try:
                    flow.steps.append(Step("setLocation", {
                        "latitude": float(latitude),
                        "longitude": float(longitude)}))
                except ValueError:
                    pass
        elif verb == "permissions":
            if len(argv) >= 3:
                args = {"action": argv[1], "service": argv[2]}
                if len(argv) >= 4 and not argv[3].startswith("-"):
                    args["appId"] = argv[3]
                flow.steps.append(Step("setPermissions", args))
        elif verb == "ui swipe":
            warn("swipe_not_compilable",
                 "point-to-point swipes do not compile; use directional "
                 "swipes in the flow by hand", seq=entry.get("seq"))
        elif any(verb == noise or verb.startswith(noise + " ")
                 or verb.split(" ")[0] == noise for noise in _NOISE_VERBS):
            continue

    # a find that proved something visible right before the end becomes the
    # closing assertion — the cheapest §8.3 "infer assertions" heuristic
    closing = None
    for path, detail in reversed(list(details.items())):
        if detail.get("kind") == "find" and detail.get("count", 0) >= 1:
            closing = _selector_from_detail(detail)
            break
    if closing is not None:
        flow.steps.append(Step("assertVisible", {"selector": closing}))
    else:
        warnings.append({
            "code": "no_final_assertion",
            "error": "the session ends without a verifying 'ui find'; the "
                     "flow has no closing assertion",
            "hint": "End recordings with 'autonom ui find <selector>' on the "
                    "success state.",
        })

    if not flow.steps or all(step.command == "note" for step in flow.steps):
        raise errors.AutonomError(
            errors.FLOW_CHECK_FAILED,
            "the session contains no compilable actions",
            hint="Drive the app with ui tap/type/find and re-record.",
            warnings=warnings,
        )
    quality["steps"] = len(flow.steps)

    env_hint = {f"SECRET_{i}": "" for i in range(1, secret_count + 1)}
    report = {"warnings": warnings, "quality": quality,
              "secrets_required": list(env_hint)}
    return flow, report


def compile_to_text(session: dict[str, Any], *, name: str | None = None,
                    task: str | None = None) -> tuple[str, dict[str, Any]]:
    flow, report = compile_session(session, name=name, task=task)
    text = emit_flow(flow)
    build_flow(parse_document(text, "<generated>"))  # never emit the unparseable
    return text, report
