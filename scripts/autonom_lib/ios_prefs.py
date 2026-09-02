"""iOS Simulator preference store — keyboard and locale pinning from the host.

Autocorrect, predictive text, and auto-capitalisation rewrite typed strings
mid-flow. A title typed as "Sync conflicts when editing offline" came back as
"Synu cofnelibysmy when emitent offline" on a simulator with a non-English
keyboard — the kind of flake that fails an `assertVisible` on text the flow
itself just typed. Pinning the keyboard language and switching all three off
makes `ui type` and the flow `inputText` command exact.

The values are written straight into the shut-down device's preference
store rather than through `simctl spawn defaults`: cfprefsd reads them at
process start, so a write into a booted device lands in a cache that is
overwritten on the next flush and only takes effect after a reboot anyway.
Editing the plists on disk with `plistlib` also means no `defaults` binary is
needed, so the same code runs on a Linux orchestrator that mounts the
CoreSimulator tree of a remote Mac.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
from pathlib import Path
from typing import Any

from . import errors

DEVICES_DIR_ENV = "AUTONOM_CORESIMULATOR_DEVICES"
GLOBAL_DOMAIN = ".GlobalPreferences"

# A pin replaces values a human may have set on purpose — the first real
# device this ran on carried `AppleLocale = en_US@rg=nlzzzz`, a region
# override the pin flattened to `en_US`. So `pin` records what it replaced
# and `reset` puts it back, instead of deleting keys and hoping the default
# was what the device had before.
_ABSENT = {"__absent__": True}

# The keys the pin owns, per domain. `reset` removes exactly these and nothing
# else, so a device keeps whatever a human configured by hand.
KEYBOARD_PINS: dict[str, dict[str, Any]] = {
    "com.apple.Preferences": {
        "KeyboardAutocorrection": False,
        "KeyboardPrediction": False,
        "KeyboardAutocapitalization": False,
    },
    "com.apple.keyboard.preferences": {
        "KeyboardAutocorrection": False,
        "KeyboardPrediction": False,
    },
}
LOCALE_KEYS = ("AppleLocale", "AppleLanguages")

_LOCALE = re.compile(r"^[a-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")


def devices_dir() -> Path:
    override = os.environ.get(DEVICES_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library/Developer/CoreSimulator/Devices"


def preferences_dir(udid: str) -> Path:
    return devices_dir() / udid / "data/Library/Preferences"


def require_preferences_dir(udid: str) -> Path:
    path = preferences_dir(udid)
    if not path.is_dir():
        raise errors.AutonomError(
            errors.SIMULATOR_DATA_NOT_FOUND,
            f"no preference store for simulator {udid} under {devices_dir()}",
            "Boot the simulator once so CoreSimulator creates its data directory, "
            f"or point {DEVICES_DIR_ENV} at the CoreSimulator Devices directory.",
        )
    return path


def normalize_locale(locale: str) -> str:
    """`en-US` and `en_US` both become `en_US`, the form AppleLocale stores."""
    candidate = (locale or "").strip()
    if not _LOCALE.match(candidate):
        raise errors.AutonomError(
            errors.FLOW_COMMAND_INVALID,
            f"not a locale identifier: {locale!r}",
            "Pass a BCP 47 / POSIX locale such as en-US, de_DE, or pt-BR.",
        )
    return candidate.replace("-", "_")


def keyboard_pins(locale: str | None) -> dict[str, dict[str, Any]]:
    """The full set of values a pin writes, with the locale pair when asked."""
    pins = {domain: dict(values) for domain, values in KEYBOARD_PINS.items()}
    if locale:
        normalized = normalize_locale(locale)
        pins[GLOBAL_DOMAIN] = {
            "AppleLocale": normalized,
            "AppleLanguages": [normalized.split("_", 1)[0]],
        }
    return pins


def owned_keys(pins: dict[str, dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Every key a pin may have written — the locale pair included, so a reset
    without `locale=` still removes a locale pinned earlier."""
    keys = {domain: tuple(values) for domain, values in pins.items()}
    keys.setdefault(GLOBAL_DOMAIN, LOCALE_KEYS)
    return keys


def plist_path(udid: str, domain: str) -> Path:
    return preferences_dir(udid) / f"{domain}.plist"


def state_root() -> Path:
    """The machine-level state root the registries share (`mocks`,
    `processes`): `$AUTONOM_HOME`, else `$XDG_STATE_HOME/autonom`, else
    `~/.local/state/autonom`."""
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        return Path(explicit).expanduser()
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "autonom"


def backup_path(udid: str) -> Path:
    return state_root() / "simulator-prefs" / f"{udid}.json"


def read_backup(udid: str) -> dict[str, dict[str, Any]] | None:
    path = backup_path(udid)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def record_backup(udid: str, pins: dict[str, dict[str, Any]]) -> Path | None:
    """Snapshot the owned keys before the first pin; a second pin on top of
    the first must not overwrite the snapshot with pinned values."""
    if read_backup(udid) is not None:
        return None
    snapshot: dict[str, dict[str, Any]] = {}
    for domain, values in pins.items():
        current = read_domain(udid, domain)
        snapshot[domain] = {
            key: (current[key] if key in current else _ABSENT) for key in values
        }
    path = backup_path(udid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_domain(udid: str, domain: str) -> dict[str, Any]:
    path = plist_path(udid, domain)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except (plistlib.InvalidFileException, ValueError, OSError) as exc:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"unreadable preference file {path}: {exc}",
            "Move the file aside (the simulator recreates it) and retry.",
        ) from exc
    return loaded if isinstance(loaded, dict) else {}


def write_domain(udid: str, domain: str, values: dict[str, Any]) -> Path:
    path = plist_path(udid, domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(values, handle, fmt=plistlib.FMT_BINARY)
    return path


def apply_pins(udid: str, pins: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge the pins into each domain, keeping every unrelated key."""
    require_preferences_dir(udid)
    written: dict[str, dict[str, Any]] = {}
    for domain, values in pins.items():
        current = read_domain(udid, domain)
        current.update(values)
        write_domain(udid, domain, current)
        written[domain] = dict(values)
    return written


def remove_pins(udid: str, pins: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Undo a pin: put back what the pin replaced, delete what it added.

    With a backup from `record_backup`, every owned key returns to its
    recorded value (or is deleted when it was absent). Without one — a pin
    made by hand, or a backup lost with the state root — the owned keys are
    deleted so the system defaults apply, which is the best honest guess.
    Returns `{"restored": {domain: {key: value}}, "removed": {domain: [key]},
    "backup": bool}`.
    """
    require_preferences_dir(udid)
    backup = read_backup(udid)
    restored: dict[str, dict[str, Any]] = {}
    removed: dict[str, list[str]] = {}
    for domain, keys in owned_keys(pins).items():
        current = read_domain(udid, domain)
        previous = (backup or {}).get(domain, {})
        changed = False
        for key in keys:
            if key in previous and previous[key] != _ABSENT:
                if current.get(key) != previous[key]:
                    current[key] = previous[key]
                    changed = True
                restored.setdefault(domain, {})[key] = previous[key]
            elif key in current:
                del current[key]
                removed.setdefault(domain, []).append(key)
                changed = True
        if changed:
            write_domain(udid, domain, current)
    if backup is not None:
        try:
            backup_path(udid).unlink()
        except OSError:
            pass
    return {"restored": restored, "removed": removed, "backup": backup is not None}


def observe(udid: str, pins: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """What the store holds right now for every owned key (None = unset)."""
    require_preferences_dir(udid)
    found: dict[str, dict[str, Any]] = {}
    for domain, keys in owned_keys(pins).items():
        current = read_domain(udid, domain)
        found[domain] = {key: current.get(key) for key in keys}
    return found


def is_pinned(observed: dict[str, dict[str, Any]],
              pins: dict[str, dict[str, Any]]) -> bool:
    return all(
        observed.get(domain, {}).get(key) == value
        for domain, values in pins.items()
        for key, value in values.items()
    )
