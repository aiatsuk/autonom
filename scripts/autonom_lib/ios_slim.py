"""Simulator slimming: which iOS Simulator background services to switch off.

A stock simulator starts well over two hundred launchd services an app under
test never talks to (widgets, Siri, iCloud, telemetry, ...). Disabling them
cuts a simulator's memory about four times. This module is the pure logic:
presets, the settings/profile model, resolution to a set of launchd labels,
the override store on disk, the `launchctl print-disabled` parser, deltas, the
state summary and Autonom's own state file. It prints nothing and runs no
`simctl`; `simulator_slim.py` drives a device with it.

The mechanism and the service catalog follow simslim (MIT, copyright
Interlap); see `ios_slim_catalog.py` and docs/ARCHITECTURE.md ("Simulator
slimming").

The override store is an undocumented CoreSimulator detail: one flat XML
plist per device under `/private/var/tmp`, label -> true (disabled) or false
(explicitly enabled). The runtime keeps entries of its own there, so every
write merges and leaves foreign entries alone, and nothing here is trusted
without a read-back from the booted device.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import errors, ios_prefs
from .contracts import utc_now
from .ios_slim_catalog import (CATALOG_SOURCE, CATEGORIES, FEATURES, FORBIDDEN_LABELS,
                               SERVICE_DESCRIPTIONS)

PROFILE_SCHEMA = "autonom.slim-profile/v1"
STATE_SCHEMA = "autonom.slim-state/v1"
TMP_ROOT_ENV = "AUTONOM_SIMDEVICE_TMP_ROOT"
DEFAULT_TMP_ROOT = "/private/var/tmp"
STORE_DIR_PREFIX = "com.apple.CoreSimulator.SimDevice."
STORE_NAME = "disabled.plist"
DEFAULT_PRESET = "optimized"

# What an app under test most often needs from the system. These stay on in
# `optimized` and `minimal` whatever else is disabled. Deep links with a
# custom scheme need no service at all.
CORE_KEEP: dict[str, str] = {
    "com.apple.assetsd": "photo import (media add)",
    "com.apple.swcd": "universal links",
    "com.apple.apsd": "push notifications",
}

_OPTIMIZED_CATEGORIES = ("widgets", "siri", "telemetry", "apps", "family", "health",
                         "messaging", "connectivity", "mdm", "other")

# Preset id -> the category ids it disables (None: every slimmable label).
PRESETS: dict[str, dict[str, Any]] = {
    "stock": {
        "description": "Nothing disabled: the simulator as Apple ships it.",
        "categories": (),
    },
    "optimized": {
        "description": "Disables services an app under test rarely needs; search, "
                       "iCloud, the stores, contacts/calendar, web and photos stay on.",
        "categories": _OPTIMIZED_CATEGORIES,
    },
    "minimal": {
        "description": "Disables every slimmable service except the core features.",
        "categories": None,
    },
}

PROFILE_FIELDS = ("schema", "preset", "need", "enable", "disable", "keep", "name",
                  "description")
_LIST_FIELDS = ("need", "enable", "disable", "keep")

# Runtimes that keep launchd disable overrides across a reboot: the iOS 18.5
# generation and newer. Older ones hold them in memory only.
_PERSISTENT_SINCE = {"iOS": (18, 5), "tvOS": (18, 5), "watchOS": (11, 5),
                     "xrOS": (2, 5), "visionOS": (2, 5)}
_RUNTIME = re.compile(r"(?:^|\.)([A-Za-z]+)-(\d+)(?:-(\d+))?(?:-\d+)*$")
_UDID = re.compile(r"^[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")


# --- catalog -------------------------------------------------------------------


def _never_disabled() -> frozenset[str]:
    always = {label for category in CATEGORIES for label in category["always_enabled"]}
    return frozenset(always | set(FORBIDDEN_LABELS))


NEVER_DISABLED = _never_disabled()
CATEGORY_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in CATEGORIES}
FEATURE_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in FEATURES}
# Every label a slim operation may ever write, enable or disable.
CATALOG_LABELS: frozenset[str] = frozenset(
    label for category in CATEGORIES for label in category["labels"]) - NEVER_DISABLED


def category_labels(category_id: str) -> frozenset[str]:
    return frozenset(CATEGORY_BY_ID[category_id]["labels"]) & CATALOG_LABELS


def feature_labels(feature_id: str) -> frozenset[str]:
    return frozenset(FEATURE_BY_ID[feature_id]["labels"]) & CATALOG_LABELS


def invalid(message: str, hint: str, **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(errors.SLIM_PROFILE_INVALID, message, hint, **extra)


# --- settings / profile -----------------------------------------------------------


def _as_list(field: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = []
        for part in value:
            if not isinstance(part, str):
                raise invalid(
                    f"{field} must hold names, got {part!r}",
                    f"Pass {field} as a comma list or a JSON list of strings.",
                    field=field, item=repr(part))
            items.append(part.strip())
    else:
        raise invalid(
            f"{field} must be a list of names, got {value!r}",
            f"Pass {field} as a comma list or a JSON list of strings.",
            field=field, item=repr(value))
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def validate_profile(raw: Any, *, require_schema: bool = False) -> dict[str, Any]:
    """The settings as a normalized profile, or `slim_profile_invalid` naming
    the bad item. Every field the resolver reads is in the result."""
    if not isinstance(raw, dict):
        raise invalid("a slim profile must be a JSON object",
                      "See 'autonom simulator slim presets' for the fields.", item="profile")
    unknown = sorted(set(raw) - set(PROFILE_FIELDS))
    if unknown:
        raise invalid(
            f"unknown slim profile field {unknown[0]!r}",
            "Fields: " + ", ".join(PROFILE_FIELDS) + ".",
            field=unknown[0], item=unknown[0])
    schema = raw.get("schema")
    if schema is None and require_schema:
        raise invalid(f"the profile has no schema; expected {PROFILE_SCHEMA!r}",
                      f'Add "schema": "{PROFILE_SCHEMA}" to the profile file.',
                      field="schema", item="schema")
    if schema is not None and schema != PROFILE_SCHEMA:
        raise invalid(f"unsupported slim profile schema {schema!r}",
                      f'Use "schema": "{PROFILE_SCHEMA}".', field="schema", item=str(schema))
    preset = raw.get("preset", DEFAULT_PRESET)
    if not isinstance(preset, str) or preset not in PRESETS:
        raise invalid(f"unknown slim preset {preset!r}",
                      "Presets: " + ", ".join(PRESETS) + ".",
                      field="preset", item=str(preset))
    profile: dict[str, Any] = {"schema": PROFILE_SCHEMA, "preset": preset}
    known = {"need": (FEATURE_BY_ID, "feature"), "enable": (CATEGORY_BY_ID, "category"),
             "disable": (CATEGORY_BY_ID, "category")}
    for field in _LIST_FIELDS:
        items = _as_list(field, raw.get(field))
        for item in items:
            if field == "keep":
                if item not in CATALOG_LABELS:
                    raise invalid(
                        f"keep names {item!r}, which is not a slimmable service label",
                        "keep takes single launchd labels from the catalog; a label "
                        "outside it is never disabled anyway.",
                        field=field, item=item)
                continue
            table, kind = known[field]
            if item not in table:
                raise invalid(
                    f"{field} names an unknown {kind} id {item!r}",
                    f"Valid {kind} ids: " + ", ".join(table) + ".",
                    field=field, item=item)
        profile[field] = items
    for field in ("name", "description"):
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise invalid(f"{field} must be text, got {value!r}",
                          f"Give {field} as a string.", field=field, item=repr(value))
        profile[field] = value
    return profile


def load_profile(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise invalid(f"cannot read slim profile {target}: {exc.strerror or exc}",
                      "Pass profile=<path> to a readable JSON file.",
                      field="profile", item=str(target)) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise invalid(f"slim profile {target} is not valid JSON: {exc}",
                      "Fix the file; see 'autonom simulator slim presets' for the fields.",
                      field="profile", item=str(target)) from exc
    return validate_profile(raw, require_schema=True)


SETTING_KEYS = ("preset", "need", "enable", "disable", "keep")


def has_settings(values: dict[str, Any]) -> bool:
    return any(key in values for key in (*SETTING_KEYS, "profile"))


def profile_from_values(values: dict[str, Any]) -> dict[str, Any]:
    """The profile a control's `--value` pairs describe: `profile=<path>`, or
    `preset`, `need`, `enable`, `disable`, `keep` (never both)."""
    if "profile" in values:
        mixed = sorted(key for key in SETTING_KEYS if key in values)
        if mixed:
            raise invalid(
                f"profile cannot be combined with {mixed[0]}",
                "Pass either profile=<path> or the settings themselves, not both.",
                field=mixed[0], item=mixed[0])
        return load_profile(str(values["profile"]))
    return validate_profile({key: values[key] for key in SETTING_KEYS if key in values})


def resolve(profile: dict[str, Any]) -> frozenset[str]:
    """The labels the profile wants disabled. Keeping wins over disabling, the
    core features stay on outside `stock`, and only catalog labels can ever be
    in the result."""
    preset = PRESETS[profile.get("preset", DEFAULT_PRESET)]
    categories = preset["categories"]
    disabled: set[str] = set(CATALOG_LABELS) if categories is None else set()
    for category_id in categories or ():
        disabled |= category_labels(category_id)
    if categories is not None:
        # A category the preset leaves on stays whole: a label it shares with
        # a disabled category (the wallet daemons are in `store` and `other`)
        # keeps running.
        for category_id in CATEGORY_BY_ID:
            if category_id not in categories:
                disabled -= category_labels(category_id)
    for category_id in profile.get("disable") or ():
        disabled |= category_labels(category_id)
    for category_id in profile.get("enable") or ():
        disabled -= category_labels(category_id)
    for feature_id in profile.get("need") or ():
        disabled -= feature_labels(feature_id)
    disabled -= set(profile.get("keep") or ())
    if profile.get("preset", DEFAULT_PRESET) != "stock":
        disabled -= set(CORE_KEEP)
    disabled -= NEVER_DISABLED
    return frozenset(disabled & CATALOG_LABELS)


def resolve_ids(ids: Iterable[str]) -> tuple[frozenset[str], list[str], list[str]]:
    """Category ids, feature ids or labels -> (catalog labels, the labels that
    were named one by one, labels a named feature covers that are never
    disabled). A name that is both a category and a feature (`siri`, `health`)
    is read as the category. An unknown id is `slim_profile_invalid`."""
    labels: set[str] = set()
    named: list[str] = []
    protected: set[str] = set()
    for item in ids:
        if item in CATEGORY_BY_ID:
            labels |= category_labels(item)
        elif item in FEATURE_BY_ID:
            labels |= feature_labels(item)
            protected |= set(FEATURE_BY_ID[item]["labels"]) - CATALOG_LABELS
        elif item in CATALOG_LABELS:
            labels.add(item)
            named.append(item)
        elif item in NEVER_DISABLED:
            raise invalid(
                f"{item} is never disabled: the simulator needs it",
                "Name a category id, a feature id or a slimmable label "
                "('autonom simulator slim presets').", field="ids", item=item)
        else:
            raise invalid(
                f"ids names {item!r}, which is no category id, feature id or "
                "slimmable service label",
                "See 'autonom simulator slim presets' for the ids.", field="ids", item=item)
    return frozenset(labels), named, sorted(protected)


# --- presets payload ---------------------------------------------------------------


def presets_payload() -> dict[str, Any]:
    presets = []
    for preset_id, preset in PRESETS.items():
        disabled = resolve({"preset": preset_id})
        categories = preset["categories"]
        presets.append({
            "id": preset_id, "description": preset["description"],
            "disabled_labels": len(disabled),
            "disabled_categories": list(CATEGORY_BY_ID if categories is None else categories),
        })
    return {
        "presets": presets,
        "default_preset": DEFAULT_PRESET,
        "core_keep": dict(CORE_KEEP),
        "categories": [{
            "id": item["id"], "name": item["name"], "description": item["description"],
            "downside": item["downside"], "approx_memory_mb": item["approx_memory_mb"],
            "labels": len(category_labels(item["id"])),
        } for item in CATEGORIES],
        "features": [{"id": item["id"], "name": item["name"],
                      "labels": len(item["labels"])} for item in FEATURES],
        "catalog_labels": len(CATALOG_LABELS),
        "catalog_source": CATALOG_SOURCE,
        "profile_schema": PROFILE_SCHEMA,
    }


def describe(label: str) -> str | None:
    return SERVICE_DESCRIPTIONS.get(label)


# --- runtime gate --------------------------------------------------------------------


def persistent_supported(runtime_identifier: str | None) -> bool:
    """Whether the runtime keeps disable overrides across a reboot (iOS 18.5
    and newer). Not proof: an apply still reads the state back after boot."""
    match = _RUNTIME.search(runtime_identifier or "")
    if not match:
        return False
    minimum = _PERSISTENT_SINCE.get(match.group(1))
    if minimum is None:
        return False
    version = (int(match.group(2)), int(match.group(3) or 0))
    return version >= minimum


def require_udid(udid: str) -> str:
    """One exact simulator: `all`, `booted` and anything that is not a UDID
    are refused before a path is built from it."""
    if not _UDID.match(udid or ""):
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"simulator slim needs one exact simulator UDID, got {udid!r}",
            "Pass --udid <UDID> from 'autonom devices'; 'all' and 'booted' are refused.",
            target_id=udid)
    return udid


# --- override store ---------------------------------------------------------------------


def tmp_root() -> Path:
    return Path(os.environ.get(TMP_ROOT_ENV) or DEFAULT_TMP_ROOT).expanduser()


def store_dir(udid: str) -> Path:
    return tmp_root() / f"{STORE_DIR_PREFIX}{require_udid(udid)}"


def store_path(udid: str) -> Path:
    return store_dir(udid) / STORE_NAME


def read_store(udid: str) -> dict[str, Any]:
    """Every entry of the store, verbatim. A device that never booted has no
    store, which is the same as nothing disabled."""
    path = store_path(udid)
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - plistlib raises expat, attribute and value errors
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"unreadable launchd override store {path}: {exc}",
            "Move the file aside (the simulator recreates it at boot) and retry.",
        ) from exc
    if not isinstance(loaded, dict):
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"launchd override store {path} is not a dictionary",
            "Move the file aside (the simulator recreates it at boot) and retry.")
    return loaded


def disabled_in(entries: dict[str, Any]) -> frozenset[str]:
    return frozenset(label for label, value in entries.items() if value is True)


def merged_entries(entries: dict[str, Any], *, disable: Iterable[str] = (),
                   enable: Iterable[str] = (), restore: dict[str, Any] | None = None,
                   ) -> dict[str, Any]:
    """`entries` with catalog labels set to true (`disable`), false (`enable`)
    or put back (`restore`: label -> previous value, None removes the entry).
    A label outside the catalog is refused; every other entry is untouched."""
    merged = dict(entries)
    touched = [*disable, *enable, *(restore or {})]
    foreign = sorted(set(touched) - CATALOG_LABELS)
    if foreign:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"{foreign[0]} is outside the slim catalog and is never written",
            "Only catalog labels can be enabled or disabled.", label=foreign[0])
    for label in disable:
        merged[label] = True
    for label in enable:
        merged[label] = False
    for label, previous in (restore or {}).items():
        if previous is None:
            merged.pop(label, None)
        else:
            merged[label] = previous
    return merged


def write_store(udid: str, entries: dict[str, Any]) -> Path:
    """Replace the store with `entries`: XML, sorted keys, a temp file renamed
    over the store so launchd_sim never reads half a file."""
    directory = store_dir(udid)
    path = directory / STORE_NAME
    try:
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o700)
        handle, temp = tempfile.mkstemp(prefix=STORE_NAME + ".", dir=str(directory))
        try:
            with os.fdopen(handle, "wb") as stream:
                plistlib.dump(entries, stream, fmt=plistlib.FMT_XML, sort_keys=True)
            os.chmod(temp, 0o644)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        # a foreign entry plistlib cannot write back (an oversized integer) too
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"cannot write the launchd override store {path}: "
            f"{getattr(exc, 'strerror', None) or exc}",
            "Check that the simulator's directory under "
            f"{tmp_root()} is writable, then retry; nothing was changed.",
        ) from exc
    return path


# --- read-back ----------------------------------------------------------------------------


def parse_print_disabled(text: str) -> frozenset[str]:
    """The disabled labels in `launchctl print-disabled` output. Recent
    launchd prints `"label" => disabled`, older builds `=> true`; a label that
    is absent, `enabled` or `false` is enabled."""
    disabled: set[str] = set()
    for line in (text or "").splitlines():
        start = line.find('"')
        if start < 0:
            continue
        end = line.find('"', start + 1)
        if end < 0:
            continue
        arrow = line.find("=>", end)
        if arrow < 0:
            continue
        value = line[arrow + 2:].strip().lower()
        if value.startswith("disabled") or value.startswith("true"):
            disabled.add(line[start + 1:end])
    return frozenset(disabled)


def delta(current: Iterable[str], desired: Iterable[str]) -> tuple[list[str], list[str]]:
    """(to_disable, to_enable) that take `current` to `desired`, scoped to
    catalog labels: a disabled label outside the catalog is nobody's drift."""
    now = set(current) & CATALOG_LABELS
    want = set(desired) & CATALOG_LABELS
    return sorted(want - now), sorted(now - want)


def summarize(disabled: Iterable[str], expected: Iterable[str] | None = None) -> dict[str, Any]:
    """`stock` (no catalog label disabled), `slim` (exactly a preset, or what
    Autonom's state file expects) or `partial`, with counts and categories."""
    off = set(disabled) & CATALOG_LABELS
    categories: dict[str, list[str]] = {"on": [], "off": [], "partial": []}
    for item in CATEGORIES:
        labels = category_labels(item["id"])
        hit = len(labels & off)
        key = "on" if hit == 0 else ("off" if hit == len(labels) else "partial")
        categories[key].append(item["id"])
    matches = next((preset_id for preset_id in PRESETS
                    if preset_id != "stock" and resolve({"preset": preset_id}) == off), None)
    if not off:
        state = "stock"
    elif matches or (expected is not None and set(expected) & CATALOG_LABELS == off):
        state = "slim"
    else:
        state = "partial"
    return {"state": state, "disabled": len(off), "total": len(CATALOG_LABELS),
            "categories": categories, "matches_preset": matches}


def compare(disabled: Iterable[str], desired: Iterable[str]) -> dict[str, Any]:
    """`missing`: should be disabled and is enabled; `extra`: disabled and
    should be on. Catalog labels only."""
    missing, extra = delta(disabled, desired)
    return {"ok": not missing and not extra, "missing": missing, "extra": extra}


# --- Autonom's state file -------------------------------------------------------------------


def state_path(udid: str) -> Path:
    return ios_prefs.state_root() / "simulator-slim" / f"{require_udid(udid)}.json"


def read_state(udid: str) -> dict[str, Any] | None:
    path = state_path(udid)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(loaded, dict) or loaded.get("schema") != STATE_SCHEMA:
        return None
    if not isinstance(loaded.get("disabled"), list) or not isinstance(
            loaded.get("previous"), dict):
        return None
    if not all(isinstance(label, str) for label in loaded["disabled"]):
        return None
    live = loaded.get("live") if isinstance(loaded.get("live"), dict) else {}
    loaded["live"] = {key: [label for label in (live.get(key) or ())
                            if isinstance(label, str)]
                      if isinstance(live.get(key), list) else []
                      for key in ("enabled", "disabled")}
    return loaded


def new_state(udid: str, profile: dict[str, Any] | None, disabled: Iterable[str],
              previous: dict[str, Any], live: dict[str, list[str]] | None = None,
              ) -> dict[str, Any]:
    labels = sorted(set(disabled) & CATALOG_LABELS)
    return {
        "schema": STATE_SCHEMA, "udid": udid, "profile": profile,
        "disabled": labels,
        # label -> the store entry before Autonom disabled it (null: no entry)
        "previous": {label: previous.get(label) for label in labels},
        "live": {"enabled": sorted(set((live or {}).get("enabled") or ())),
                 "disabled": sorted(set((live or {}).get("disabled") or ()))},
        "applied_at": utc_now(),
    }


def write_state(udid: str, state: dict[str, Any]) -> Path:
    path = state_path(udid)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return path


def delete_state(udid: str) -> bool:
    """Remove the state file; False when there was none. A file that cannot
    be removed raises OSError: a stale file would claim an apply that is gone."""
    try:
        state_path(udid).unlink()
    except FileNotFoundError:
        return False
    return True


def expected_disabled(state: dict[str, Any] | None) -> frozenset[str]:
    """What the state file says should be disabled now: its profile, then the
    live `enable`/`disable` changes made since."""
    if not state:
        return frozenset()
    profile = state.get("profile")
    try:
        base = set(resolve(validate_profile(profile))) if isinstance(profile, dict) else set()
    except errors.AutonomError:
        base = set()
    live = state.get("live") if isinstance(state.get("live"), dict) else {}
    base |= set(live.get("disabled") or ())
    base -= set(live.get("enabled") or ())
    return frozenset(base & CATALOG_LABELS)
