"""Screen fingerprints: stable identity for what is on screen (§10.2).

Two hashes per snapshot:

- **structure** — the screen's identity: roles, stable ids, and hierarchy
  shape. Volatile content (numbers, times, list length) must not move it.
- **state** — the variant within a screen: stable text labels plus
  enabled/selected/checked state. A badge count or clock tick must not
  create a new variant, so volatile-looking text is classified, not copied.

Excluded on purpose: timestamps, counters, random-looking ids, system UI
(status bar) and keyboard nodes, and list-item repetition (a list of 3 and
a list of 30 are the same screen).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

_SYSTEM_PACKAGES = ("com.android.systemui", "com.google.android.inputmethod",
                    "com.android.inputmethod")
_VOLATILE_TEXT = re.compile(
    r"^\s*$"                                   # empty
    r"|^[\d\s.,:%/+-]+$"                       # numbers, times, dates, percents
    r"|\d{1,2}[:.]\d{2}"                       # clock-like anywhere
    r"|^[0-9a-f]{8,}$",                        # hex ids
    re.IGNORECASE)
_RANDOM_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}", re.IGNORECASE)


def _stable_text(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if _VOLATILE_TEXT.search(text):
        return "<volatile>"
    return text[:80]


def _stable_id(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if _RANDOM_ID.search(text):
        return "<generated-id>"
    return text


def _meaningful(node: dict[str, Any]) -> bool:
    package = node.get("package") or ""
    if any(package.startswith(system) for system in _SYSTEM_PACKAGES):
        return False
    return bool(node.get("text") or node.get("desc") or node.get("resource_id")
                or node.get("clickable") or node.get("scrollable"))


def _rows(nodes: Iterable[dict[str, Any]]):
    for node in nodes:
        if not _meaningful(node):
            continue
        structure = (node.get("role") or "",
                     _stable_id(node.get("resource_id")) or "",
                     int(node.get("depth") or 0))
        state = (_stable_text(node.get("text")) or "",
                 _stable_text(node.get("desc")) or "",
                 bool(node.get("enabled", True)),
                 bool(node.get("selected")), bool(node.get("checked")))
        yield structure, state


def fingerprint(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """-> {structure, state, labels} — hashes plus a human-readable handle.

    The structure hash collapses *consecutive same-shaped* rows (a list of 3
    and a list of 30 are the same screen); the state hash collapses only
    exact duplicates, so a distinct same-shaped sibling ("Checkout" next to
    a clock) still counts. Labels scan everything.
    """
    structures = []
    states = []
    labels: list[str] = []
    previous_structure = None
    previous_row = None
    for structure, state in _rows(nodes):
        if structure != previous_structure:
            structures.append("|".join(str(part) for part in structure))
            previous_structure = structure
        row = "|".join(str(part) for part in (*structure, *state))
        if row != previous_row:
            states.append(row)
            previous_row = row
        for candidate in (state[0], state[1], structure[1]):
            if candidate and candidate not in ("<volatile>", "<generated-id>") \
                    and candidate not in labels:
                labels.append(candidate)
    structure_hash = hashlib.sha256(
        "\n".join(structures).encode("utf-8")).hexdigest()[:12]
    state_hash = hashlib.sha256(
        "\n".join(states).encode("utf-8")).hexdigest()[:12]
    return {"structure": f"scr_{structure_hash}",
            "state": f"var_{state_hash}",
            "labels": labels[:3]}
