"""The edge-case kit (tp-016): stable ids an agent plans checks from.

Each case names the risk it probes and the existing Autonom verbs that set it
up on a device. A check refers to a case with ``--case <id>``; ``coverage``
says which cases the pre-test exercised and with which verdicts. A case no
check names is reported ``covered: false`` — skipped, never passed.
"""
from __future__ import annotations

from typing import Any, Iterable

KIT: tuple[dict[str, Any], ...] = (
    {"id": "offline", "title": "No network",
     "risk": "The screen hangs, crashes or shows stale data as if fresh when the device is offline.",
     "verbs": ["simulator network offline", "simulator network online"]},
    {"id": "slow", "title": "Slow network",
     "risk": "Spinners never end, taps fire twice, or timeouts are missing on a slow link.",
     "verbs": ["simulator network shape --value speed=edge --value delay=edge",
               "simulator network shape --value speed=full --value delay=none"]},
    {"id": "server-error", "title": "Server error (5xx)",
     "risk": "A 500 response is swallowed, shown as empty data, or crashes the screen.",
     "verbs": ["network start", "network attach",
               "network mock add --match '<url glob>' --status 500",
               "network mock remove <id>"]},
    {"id": "timeout", "title": "Request never answers",
     "risk": "A request that never completes leaves the screen stuck with no retry.",
     "verbs": ["network start", "network attach",
               "simulator network shape --value speed=gsm --value delay=gprs",
               "network requests list",
               "simulator network shape --value speed=full --value delay=none"]},
    {"id": "background-resume", "title": "Background and resume",
     "risk": "State, timers or input are lost when the app goes to the background and returns.",
     "verbs": ["ui key HOME", "session launch <app-id>"]},
    {"id": "process-death", "title": "Process death and restore",
     "risk": "The app loses the user's place or data after the system kills the process.",
     "verbs": ["session force-stop <app-id>", "session launch <app-id>"]},
    {"id": "rotation", "title": "Rotation",
     "risk": "Layout breaks or input is lost when the screen rotates (Android).",
     "verbs": ["flow run (setOrientation: landscape)", "flow run (setOrientation: portrait)"]},
    {"id": "dark-mode", "title": "Dark mode",
     "risk": "Text or icons become unreadable in the dark color scheme.",
     "verbs": ["simulator appearance dark", "simulator appearance light"]},
    {"id": "large-text", "title": "Large text",
     "risk": "Text is clipped or overlaps at large accessibility font sizes.",
     "verbs": ["simulator text-size 1.3 (Android scale)",
               "simulator text-size accessibility-extra-large (iOS)",
               "simulator text-size 1.0 (Android) or large (iOS)"]},
    {"id": "locale", "title": "Another locale",
     "risk": "Untranslated strings, wrong number or date formats, or overflow in longer languages.",
     "verbs": ["simulator keyboard pin --value locale=<locale>", "simulator keyboard reset"]},
    {"id": "permission-denied", "title": "Permission denied",
     "risk": "The feature crashes or dead-ends when a permission is refused.",
     "verbs": ["permissions revoke <service> <app-id>", "permissions reset <service> <app-id>"]},
    {"id": "empty-input", "title": "Empty input",
     "risk": "Submitting an empty field is accepted, crashes, or shows no message.",
     "verbs": ["ui tap --text '<field>'", "ui tap --text '<submit>'"]},
    {"id": "long-input", "title": "Very long input",
     "risk": "Long text overflows the layout, is cut silently, or breaks the request.",
     "verbs": ["ui tap --text '<field>'", "ui type '<500 characters>'"]},
    {"id": "emoji-input", "title": "Emoji input",
     "risk": "Emoji are dropped, garble the text, or break length counting.",
     "verbs": ["ui tap --text '<field>'", "ui type '<text with emoji>'"]},
    {"id": "rtl-input", "title": "Right-to-left input",
     "risk": "Arabic or Hebrew text is mirrored wrongly or mixes badly with numbers.",
     "verbs": ["ui tap --text '<field>'", "ui type '<right-to-left text>'"]},
    {"id": "double-tap", "title": "Double tap",
     "risk": "A fast second tap submits twice, opens two screens, or charges twice.",
     "verbs": ["flow run (doubleTapOn: <button>)",
               "ui tap --text '<button>' (twice, quickly)"]},
    {"id": "fast-back", "title": "Back while loading",
     "risk": "Going back during a load crashes or leaves a callback updating a dead screen.",
     "verbs": ["ui tap --text '<action>'", "ui key BACK"]},
    {"id": "deep-link", "title": "Deep link",
     "risk": "A link opens the wrong screen, loses its parameters, or fails when the app is cold.",
     "verbs": ["open '<app link>'", "session force-stop <app-id>", "open '<app link>'"]},
)

_BY_ID = {case["id"]: case for case in KIT}


def ids() -> list[str]:
    return [case["id"] for case in KIT]


def get(case_id: str) -> dict[str, Any] | None:
    return _BY_ID.get(case_id)


def coverage(checks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per kit case: whether a check named it, which ones, their verdicts.

    A case counts as covered only when a check naming it actually ran (began);
    a check that was planned and never begun is listed but not coverage.
    """
    checks = list(checks)
    out: list[dict[str, Any]] = []
    for case in KIT:
        named = [check for check in checks if check.get("case") == case["id"]]
        ran = [check for check in named if check.get("state") in ("open", "ended")
               or check.get("began_at")]
        out.append({
            "id": case["id"],
            "title": case["title"],
            "covered": bool(ran),
            "checks": [check.get("check_id") for check in named],
            "verdicts": [check.get("verdict") for check in named],
        })
    return out
