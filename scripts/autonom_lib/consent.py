"""The one gate for privileged operations (CAP-ATTACH-001, C-05, INV-04).

Installing a CA, changing a device's proxy, or widening the proxy bind are
state-changing security operations. They require **separate, explicit, informed
human confirmation for the exact action**, obtained fresh each time.

Deliberate design choices, all of them load-bearing:

- there is no bypass parameter and no environment override, so a script, a
  subagent, or a stored preference cannot manufacture consent;
- consent is never cached — a grant earlier in the same session does not carry
  to the next invocation;
- refusal happens *before* any backend command runs, so a refused operation has
  an exact side-effect count of zero;
- every grant is appended to the session's `consent_log`.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any

from . import errors

PHRASE_EN = (
    "As the user, I explicitly agree to the described certificate, key, signing, "
    "or network configuration change"
)
PHRASE_RU = (
    "Я, пользователь, явно согласен на описанное изменение сертификатов, ключей, "
    "подписей или сетевых настроек"
)
ACCEPTED_PHRASES = (PHRASE_EN, PHRASE_RU)


@dataclass(frozen=True)
class Operation:
    """What is about to change, stated before anything is asked."""

    kind: str          # e.g. "device_proxy", "ca_install", "proxy_bind"
    target: str        # platform:target_id, or the host
    effect: str        # one sentence a human can evaluate
    flags: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"{self.kind} on {self.target}: {self.effect}"


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().rstrip(".").split()).casefold()


def phrase_accepted(text: str) -> bool:
    candidate = _normalize(text)
    return any(candidate == _normalize(phrase) for phrase in ACCEPTED_PHRASES)


def require(
    operation: Operation,
    *,
    acknowledged: bool,
    extra_required: bool = True,
    stream: Any = None,
    prompt: Any = None,
) -> dict[str, Any]:
    """Authorize `operation`, or raise.

    `acknowledged` is the caller's flag state (e.g. `--i-understand-mitm` present,
    plus `--install-ca` when the operation writes to a trust store). It is a
    necessary condition, never a sufficient one on an interactive terminal.
    """
    if not acknowledged or not extra_required:
        raise errors.AutonomError(
            errors.CONSENT_REQUIRED,
            f"refused without explicit consent — {operation.describe()}",
            "Re-run with the required flag(s): " + (", ".join(operation.flags) or
                                                    "--i-understand-mitm") +
            ". On an interactive terminal you will also be asked to type the "
            "confirmation phrase.",
            operation=operation.kind,
            target=operation.target,
        )

    if sys.stdin is not None and sys.stdin.isatty():
        out = stream or sys.stderr
        reader = prompt or (lambda: sys.stdin.readline())
        print(f"\nAutonom is about to perform a privileged change:\n"
              f"  {operation.describe()}\n\n"
              f"To proceed, type exactly:\n  {PHRASE_EN}\n"
              f"or:\n  {PHRASE_RU}\n", file=out)
        answer = reader()
        if not phrase_accepted(answer):
            raise errors.AutonomError(
                errors.CONSENT_DECLINED,
                f"consent declined — {operation.describe()}",
                "Nothing was changed. Re-run and type the confirmation phrase exactly.",
                operation=operation.kind,
                target=operation.target,
            )

    return {
        "operation": operation.kind,
        "target": operation.target,
        "effect": operation.effect,
        "flags": list(operation.flags),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def record(session_record: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Append the audit entry. Never stores secret material — only what changed."""
    session_record.setdefault("consent_log", []).append(entry)
    return session_record
