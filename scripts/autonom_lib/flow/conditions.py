"""``when:`` clause evaluation — platform / visible / notVisible / envEquals.

AND semantics only (research §7.12): every stated condition must hold, or the
step is skipped with a reason naming the first condition that did not. The
snapshot is provided by the caller so this module never touches a device —
and so a clause with no UI conditions costs no accessibility dump at all.
"""
from __future__ import annotations

from typing import Callable

from . import selectors as flow_selectors
from .schema import WhenClause


def evaluate(when: WhenClause, platform: str, values: dict,
             snapshot: Callable[[], list]) -> tuple[bool, str | None]:
    """Return (met, reason-not-met)."""
    if when.platform and when.platform != platform:
        return False, f"platform is {platform}, condition wants {when.platform}"
    for name, expected in when.env_equals.items():
        actual = values.get(name)
        if actual != expected:
            return False, f"envEquals: {name} is {actual!r}, condition wants {expected!r}"
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
