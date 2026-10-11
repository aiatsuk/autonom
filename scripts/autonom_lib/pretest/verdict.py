"""The ticket verdict (tp-012), from folded checks and findings.

In this order, the first rule that holds decides:

1. ``fail``         — a check ended ``fail``, or a confirmed blocker or major
                      finding exists;
2. ``blocked``      — a check ended ``blocked``;
3. ``not_covered``  — there are no checks, a check was added but never
                      began, or a check ended ``not_covered``;
4. ``inconclusive`` — a check ended ``inconclusive`` or is still open;
5. ``pass``         — every check ended ``pass``.

Unconfirmed findings (every automatic one until a reviewer confirms it)
never move the verdict. A check whose ``state`` is not ``added``, ``open``
or ``ended``, or an ended check without a known verdict, is refused
(``invalid_value``): it never counts as passed. The lists ``not_covered``, ``blocked`` and ``open``
are always filled, whatever the verdict, so a summary can name every check
that kept the ticket from passing.
"""
from __future__ import annotations

from typing import Any, Iterable

from .. import errors
from .checks import STATES, VERDICTS as CHECK_VERDICTS

VERDICTS = ("fail", "blocked", "not_covered", "inconclusive", "pass")
GATING_SEVERITIES = ("blocker", "major")


def _items(values: Any, what: str) -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, dict)) or not isinstance(values, Iterable):
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"the ticket verdict needs a list of {what}",
            hint=f"Pass the result of '{what}.fold'.")
    items = list(values)
    if not all(isinstance(item, dict) for item in items):
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"every one of the {what} must be an object",
            hint=f"Pass the result of '{what}.fold'.")
    return items


def _check_states(checks: list[dict[str, Any]]) -> None:
    """Refuse a check the fold could not have produced: it must not fall
    through to ``pass``."""
    for check in checks:
        state, verdict = check.get("state"), check.get("verdict")
        if state not in STATES:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"check {check.get('check_id')!r} has an unknown state {state!r}",
                hint="States: " + ", ".join(STATES) + ". Pass the result of 'checks.fold'.")
        if state == "ended" and verdict not in CHECK_VERDICTS:
            raise errors.AutonomError(
                errors.INVALID_VALUE,
                f"check {check.get('check_id')!r} ended without a known verdict "
                f"({verdict!r})",
                hint="Verdicts: " + ", ".join(CHECK_VERDICTS)
                     + ". Pass the result of 'checks.fold'.")


def _ids(checks: list[dict[str, Any]], keep) -> list[str]:
    return [str(check.get("check_id")) for check in checks if keep(check)]


def ticket_verdict(checks: Any, findings: Any) -> dict[str, Any]:
    """``{"verdict", "reasons": [...], "not_covered": [ids], "blocked": [ids],
    "open": [ids]}`` (see the module text for the order)."""
    check_items = _items(checks, "checks")
    finding_items = _items(findings, "findings")
    _check_states(check_items)

    failed = _ids(check_items, lambda c: c.get("state") == "ended"
                  and c.get("verdict") == "fail")
    gating = [str(f.get("finding_id")) for f in finding_items
              if f.get("confirmed") is True and f.get("severity") in GATING_SEVERITIES]
    blocked = _ids(check_items, lambda c: c.get("state") == "ended"
                   and c.get("verdict") == "blocked")
    never_ran = _ids(check_items, lambda c: c.get("state") == "added")
    ended_not_covered = _ids(check_items, lambda c: c.get("state") == "ended"
                             and c.get("verdict") == "not_covered")
    not_covered = [cid for cid in _ids(check_items, lambda c: True)
                   if cid in never_ran or cid in ended_not_covered]
    open_ids = _ids(check_items, lambda c: c.get("state") == "open")
    inconclusive = _ids(check_items, lambda c: c.get("state") == "ended"
                        and c.get("verdict") == "inconclusive")

    reasons: list[str] = []
    if failed or gating:
        verdict = "fail"
        reasons += [f"check {cid} failed" for cid in failed]
        severity = {str(f.get("finding_id")): f.get("severity") for f in finding_items}
        reasons += [f"finding {fid} is a confirmed {severity[fid]}" for fid in gating]
    elif blocked:
        verdict = "blocked"
        reasons += [f"check {cid} is blocked" for cid in blocked]
    elif not check_items or not_covered:
        verdict = "not_covered"
        if not check_items:
            reasons.append("no checks were added")
        reasons += [f"check {cid} never ran" for cid in never_ran]
        reasons += [f"check {cid} ended not covered" for cid in ended_not_covered]
    elif inconclusive or open_ids:
        verdict = "inconclusive"
        reasons += [f"check {cid} is inconclusive" for cid in inconclusive]
        reasons += [f"check {cid} is still open" for cid in open_ids]
    else:
        verdict = "pass"
        reasons.append(f"all {len(check_items)} check(s) passed")
    return {"verdict": verdict, "reasons": reasons, "not_covered": not_covered,
            "blocked": blocked, "open": open_ids}
