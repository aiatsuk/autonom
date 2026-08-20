"""``when:`` clause evaluation — platform / visible / notVisible / envEquals.

AND semantics only (research §7.12): every stated condition must hold, or the
step is skipped with a reason naming the first condition that did not. The
snapshot is provided by the caller so this module never touches a device —
and so a clause with no UI conditions costs no accessibility dump at all.
"""
from __future__ import annotations

from typing import Callable, Iterable

from . import selectors as flow_selectors
from .schema import WhenClause


def evaluate(when: WhenClause, platform: str, values: dict,
             snapshot: Callable[[], list], *,
             secret_names: Iterable[str] = (),
             secret_literals: Iterable[str] = ()) -> tuple[bool, str | None]:
    """Return (met, reason-not-met).

    ``secret_names`` / ``secret_literals`` keep ``--secret`` and
    ``sensitive:`` values out of skip reasons (events, manifest, reports).
    A mismatch still names the variable; it never prints either side.
    """
    redact_names = set(secret_names)
    redact_values = {v for v in secret_literals if v}
    if when.platform and when.platform != platform:
        return False, f"platform is {platform}, condition wants {when.platform}"
    for name, expected in when.env_equals.items():
        actual = values.get(name)
        if actual != expected:
            return False, _env_equals_reason(
                name, actual, expected, redact_names, redact_values)


def _env_equals_reason(name: str, actual, expected,
                       redact_names: set, redact_values: set) -> str:
    if (name in redact_names
            or actual in redact_values
            or expected in redact_values):
        return f"envEquals: {name} does not match"
    return f"envEquals: {name} is {actual!r}, condition wants {expected!r}"
    nodes: list | None = None
    for label, selector, want in (("visible", when.visible, True),
                                  ("notVisible", when.not_visible, False)):
        if selector is None:
            continue
        if nodes is None:
            nodes = snapshot()
        matches = flow_selectors.select_all(nodes, selector)
        if bool(matches) != want:
            return False, (f"{label}: {flow_selectors.describe(selector)} "
                           f"{'not found' if want else 'still present'}")
    return True, None
