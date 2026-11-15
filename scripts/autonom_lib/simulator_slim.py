"""`simulator slim`: switch iOS Simulator background services off and on.

The device-facing half of simulator slimming; the rules (presets, profiles,
the override store, the state file) are in `ios_slim.py`.

Two ways to change a simulator:

- `apply` / `restore` write the launchd override store of a shut-down
  simulator, which the runtime reads at its next boot. The store is an
  undocumented CoreSimulator detail, so after a boot this verb made itself
  the state is read back with `launchctl print-disabled`; overrides the
  runtime did not honour are set again one by one with `launchctl` and the
  simulator is rebooted once more. Success is never reported for a booted
  simulator without a matching read-back.
- `enable` / `disable` change a booted simulator at once with `launchctl`,
  and flip the same store entries so the next boot agrees.

Only labels of the slim catalog are ever written, enabled or disabled.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import errors, ios_simctl, ios_slim, simulator
from .platform import ANDROID, Target

CONTROL = "slim"
ACTIONS = ("presets", "show", "suggest", "apply", "verify", "enable", "disable", "restore")
READONLY_ACTIONS = frozenset({"presets", "show", "suggest", "verify"})
_JSON_SCALARS = (bool, int, float, str)


def run(target: Target | None, action: str, values: dict[str, Any]) -> dict[str, Any]:
    """Run one slim action. `presets` describes the catalog and needs no
    target; `suggest` needs one only to find an installed app by bundle id."""
    if action == "presets" or (target is None and action != "suggest"):
        return _presets()
    _check_keys(action, values)
    if action == "suggest" and target is None:
        return _suggest(None, values)
    if target.platform == ANDROID:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "simulator slim switches iOS Simulator launchd services off; an Android "
            "emulator has no such store",
            hint="Use it with --platform ios --udid <UDID>; on Android pick a lighter "
                 "system image instead.",
            capability="simulator.slim")
    handlers = {"show": _show, "suggest": _suggest, "apply": _apply, "verify": _verify, "enable": _enable,
                "disable": _disable, "restore": _restore}
    return handlers[action](target, values)


_SETTINGS = (*ios_slim.SETTING_KEYS, "profile")
VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "show": (), "suggest": ("app", "preset", "write", "overwrite"), "verify": _SETTINGS, "apply": (*_SETTINGS, "reboot", "details"),
    "enable": ("ids",), "disable": ("ids", "force"), "restore": ("reboot", "all", "details"),
}


def _check_keys(action: str, values: dict[str, Any]) -> None:
    """A mistyped key (`prest=minimal`) must not quietly apply the default."""
    allowed = VALUE_KEYS.get(action, ())
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ios_slim.invalid(
            f"simulator slim {action} takes no value {unknown[0]!r}",
            (f"Values of {action}: " + ", ".join(allowed) + ".") if allowed
            else f"{action} takes no values.",
            field=unknown[0], item=unknown[0])


def _presets() -> dict[str, Any]:
    return {"control": CONTROL, "action": "presets", **ios_slim.presets_payload()}


# --- device helpers -------------------------------------------------------------


def _device(target: Target) -> tuple[str, str]:
    """(udid, state) of the one exact simulator the verb addresses."""
    udid = ios_slim.require_udid(target.target_id)
    found = ios_simctl.find_simulator(target.tool, udid)
    if found is None:
        raise errors.AutonomError(
            errors.NO_TARGET,
            f"no simulator with UDID {udid}",
            "List the simulators with 'autonom devices --platform ios'.",
            target_id=udid)
    return udid, found.state


def _runtime(target: Target, udid: str) -> tuple[str | None, bool]:
    identifier = ios_simctl.device_runtime_identifier(target.tool, udid)
    return identifier, ios_slim.persistent_supported(identifier)


def _live_disabled(target: Target, udid: str) -> frozenset[str]:
    return ios_slim.parse_print_disabled(
        ios_simctl.launchctl_print_disabled(target.tool, udid))


def _actual(target: Target, udid: str, state: str) -> tuple[frozenset[str], str]:
    """The disabled labels and where they were read: the booted simulator's
    launchd, else the store it will read at its next boot."""
    if state == "Booted":
        return _live_disabled(target, udid), "launchctl"
    return ios_slim.disabled_in(ios_slim.read_store(udid)), "store"


def _refuse_in_flux(udid: str, state: str) -> None:
    if state in ("Booting", "Shutting Down"):
        raise errors.AutonomError(
            errors.SIMULATOR_MUST_BE_SHUTDOWN,
            f"simulator {udid} is {state}; its launchd overrides are in flux",
            hint="Wait until 'autonom devices' shows it Booted or Shutdown, then retry.",
            target_id=udid, state=state)


def _require_shutdown_or_reboot(udid: str, booted: bool, reboot: bool) -> None:
    if booted and not reboot:
        raise errors.AutonomError(
            errors.SIMULATOR_MUST_BE_SHUTDOWN,
            f"simulator {udid} is booted; launchd reads its overrides at boot, so a "
            "write now would not take effect",
            hint="Shut it down first ('autonom devices shutdown --udid <UDID>') or "
                 "pass --value reboot=true to let this verb shut down, write, and "
                 "boot it again. 'simulator slim disable' stops services live.",
            target_id=udid)


def _shutdown(target: Target, udid: str, *, written: bool = False) -> None:
    """`simctl shutdown` can exit 0 with the device still up; the device list
    is the only oracle, so nothing is written until it reads Shutdown.
    ``written``: the store was already written (the second reboot)."""
    ios_simctl.shutdown(target.tool, udid)
    after = ios_simctl.find_simulator(target.tool, udid)
    state = after.state if after is not None else None
    if state != "Shutdown":
        raise errors.AutonomError(
            errors.SIMULATOR_MUST_BE_SHUTDOWN,
            f"simulator {udid} is still {state or 'unknown'} after shutdown; "
            + ("the overrides are written and need a reboot to apply" if written
               else "nothing was written"),
            hint="Shut it down by hand ('xcrun simctl shutdown <UDID>') and "
                 + ("boot it again, then run 'simulator slim verify'." if written
                    else "retry without reboot=true."),
            target_id=udid, state=state)


def _boot_back(target: Target, udid: str) -> None:
    """Best effort after a failure: the caller asked for a running simulator
    back, and a boot failure must not replace the error that stopped the verb."""
    try:
        ios_simctl.boot(target.tool, udid)
    except Exception:  # noqa: BLE001 - the original failure is re-raised
        pass


def _scalar(value: Any) -> Any:
    return value if isinstance(value, _JSON_SCALARS) else None


def _state_block(udid: str, state: dict[str, Any] | None) -> dict[str, Any]:
    block: dict[str, Any] = {"exists": state is not None,
                             "path": str(ios_slim.state_path(udid))}
    if state is not None:
        block.update({"profile": state.get("profile"),
                      "disabled": len(state.get("disabled") or ()),
                      "live": state.get("live"),
                      "applied_at": state.get("applied_at")})
    return block


def _commit(udid: str, entries: dict[str, Any], planned: dict[str, Any],
            state: dict[str, Any] | None) -> None:
    """Write the store, then the state file that describes it (None deletes
    it). A store that could not be written leaves the state file as it was; a
    state file that could not be written puts the store back, so the state
    file never claims a change that is not in the store and the store never
    holds a change restore knows nothing about."""
    existed = ios_slim.store_path(udid).exists()
    if planned != entries:
        ios_slim.write_store(udid, planned)
    try:
        if state is None:
            ios_slim.delete_state(udid)
        else:
            ios_slim.write_state(udid, state)
    except OSError as exc:
        reverted = True
        if planned != entries:
            try:
                if existed:
                    ios_slim.write_store(udid, entries)
                else:
                    ios_slim.store_path(udid).unlink()
            except (OSError, errors.AutonomError):
                reverted = False
        hint = ("Check that the Autonom state directory is writable; the simulator's "
                "overrides were put back as they were." if reverted else
                "Check that the Autonom state directory is writable. The simulator's "
                f"override store {ios_slim.store_path(udid)} could NOT be put back and "
                "holds the new entries; 'simulator slim show' reads what it says now.")
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"cannot write the slim state file {ios_slim.state_path(udid)}: "
            f"{exc.strerror or exc}",
            hint, store_reverted=reverted) from exc


def _converge(target: Target, udid: str, *, want_disabled: Iterable[str],
              want_enabled: Iterable[str], boot_timeout: float = 120) -> dict[str, Any]:
    """Boot, read the overrides back, and when the runtime ignored the store
    set the missing ones with `launchctl` and reboot once. Still missing after
    that is `slim_not_persisted`."""
    off, on = frozenset(want_disabled), frozenset(want_enabled)

    def lost() -> tuple[list[str], list[str]]:
        after = _live_disabled(target, udid)
        return sorted(off - after), sorted(on & after)

    ios_simctl.boot(target.tool, udid, timeout=boot_timeout)
    missing, extra = lost()
    if not missing and not extra:
        return {"fallback": False}
    for label in missing:
        ios_simctl.launchctl_disable(target.tool, udid, label)
    for label in extra:
        ios_simctl.launchctl_enable(target.tool, udid, label)
    _shutdown(target, udid, written=True)
    ios_simctl.boot(target.tool, udid, timeout=boot_timeout)
    still_missing, still_extra = lost()
    if still_missing or still_extra:
        labels = sorted({*still_missing, *still_extra})
        raise errors.AutonomError(
            errors.SLIM_NOT_PERSISTED,
            f"{len(labels)} launchd override(s) did not survive the reboot of "
            f"simulator {udid}",
            hint="This runtime does not keep the overrides. 'simulator slim disable' "
                 "stops services for the current boot only; 'simulator slim restore' "
                 "puts the store back.",
            target_id=udid, labels=labels, missing=still_missing, extra=still_extra,
            state_file=str(ios_slim.state_path(udid)))
    return {"fallback": True, "fallback_labels": sorted({*missing, *extra})}


def _finish_reboot(target: Target, result: dict[str, Any]) -> None:
    # the shutdown ended the session's `log stream`; bring it back
    simulator._merge(result, simulator.restart_session_log_stream(target, cause="reboot"))


# --- show / verify -----------------------------------------------------------------


def _show(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    udid, sim_state = _device(target)
    runtime, persistent = _runtime(target, udid)
    disabled, source = _actual(target, udid, sim_state)
    state = ios_slim.read_state(udid)
    summary = ios_slim.summarize(
        disabled, ios_slim.expected_disabled(state) if state else None)
    return {"control": CONTROL, "action": "show", **summary, "source": source,
            "simulator_state": sim_state, "runtime": runtime, "persistent": persistent,
            "core_keep": {label: label not in disabled for label in ios_slim.CORE_KEEP},
            "state_file": _state_block(udid, state),
            "verified": True,
            "verification": "read_back" if source == "launchctl" else "store_read"}


def _verify(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    udid, sim_state = _device(target)
    result: dict[str, Any] = {"control": CONTROL, "action": "verify"}
    if ios_slim.has_settings(values):
        profile = ios_slim.profile_from_values(values)
        desired = ios_slim.resolve(profile)
        result.update({"compared_with": "settings", "profile": profile})
    else:
        state = ios_slim.read_state(udid)
        desired = ios_slim.expected_disabled(state)
        if state is None:
            result.update({"compared_with": "stock", "profile": None,
                           "note": "no slim state file for this simulator, so it is "
                                   "compared with stock; pass preset=... to compare "
                                   "with a profile"})
        else:
            result.update({"compared_with": "state_file", "profile": state.get("profile")})
    disabled, source = _actual(target, udid, sim_state)
    result.update(ios_slim.compare(disabled, desired))
    result.update({"expected_disabled": len(desired), "source": source,
                   "simulator_state": sim_state, "verified": True,
                   "verification": "read_back" if source == "launchctl" else "store_read"})
    return result


# --- suggest -------------------------------------------------------------------------


def app_is_path(values: dict[str, Any]) -> bool:
    """Whether `app=` names a bundle on disk (no simulator needed) rather
    than a bundle id to look up on one."""
    app = values.get("app")
    if not isinstance(app, str) or not app.strip():
        return False
    app = app.strip()
    return "/" in app or app.endswith(".app") or Path(app).expanduser().exists()


def _app_bundle(target: Target | None, app: str) -> Path:
    if app_is_path({"app": app}):
        path = Path(app).expanduser()
        if not path.exists():
            raise errors.AutonomError(
                errors.INSTALL_PATH_NOT_FOUND, f"app not found: {path}",
                "Pass app=<path to a built .app bundle>, or app=<bundle id> of an app "
                "installed on the simulator.", path=str(path))
        if not path.is_dir() or path.suffix != ".app":
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"{path} is not an .app bundle",
                "Pass the built .app directory (for an .ipa, unzip it and pass "
                "Payload/<name>.app).", path=str(path))
        return path
    if target is None:
        raise errors.AutonomError(
            errors.NO_TARGET,
            f"app={app} is a bundle id; a simulator is needed to find the installed app",
            "Add --udid <UDID>, or pass app=<path to the .app bundle>.")
    udid, _state = _device(target)
    container = ios_simctl.app_container(target.tool, udid, app, "app")
    if container is None or not container.is_dir():
        raise errors.AutonomError(
            errors.APP_NOT_INSTALLED,
            f"{app} is not installed on simulator {udid} (or the simulator is not booted)",
            "Boot the simulator and install the app, or pass app=<path to the .app "
            "bundle>.", app_id=app, target_id=udid)
    return container


def _write_profile(path: Path, profile: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
            os.chmod(temp, 0o644)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"cannot write the slim profile {path}: {exc.strerror or exc}",
            "Pass write=<path> in a writable directory.", path=str(path)) from exc


def _suggest(target: Target | None, values: dict[str, Any]) -> dict[str, Any]:
    from . import ios_app_scan

    app = values.get("app")
    if not isinstance(app, str) or not app.strip():
        raise errors.AutonomError(
            errors.INVALID_VALUE, "simulator slim suggest needs app=<path | bundle id>",
            "Pass --value app=<path to the built .app> or --value app=<bundle id>.")
    preset = values.get("preset", ios_slim.SUGGEST_DEFAULT_PRESET)
    ios_slim.validate_profile({"preset": preset})
    overwrite = simulator._bool(values, "overwrite", False)
    write = values.get("write")
    out: Path | None = None
    if write is not None:
        if not isinstance(write, str) or not write.strip():
            raise errors.AutonomError(
                errors.INVALID_VALUE, "write needs a file path",
                "Pass --value write=.autonom/slim-profile.json.")
        out = Path(write.strip()).expanduser()
        if out.is_dir():
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"{out} is a directory",
                "Pass write=<path to a .json file>.", path=str(out))
        if out.exists() and not overwrite:
            raise errors.AutonomError(
                errors.INVALID_VALUE, f"{out} already exists and was left as it is",
                "Pass --value overwrite=true to replace it, or another write=<path>.",
                path=str(out))
    bundle = _app_bundle(target, app.strip())
    facts, described, limitations = ios_app_scan.scan(bundle)
    label = described.get("bundle_id") or described.get("name") or bundle.stem
    body = ios_slim.suggestion(
        facts, preset=preset, name=f"{label} slim profile",
        description=f"Suggested from a scan of {bundle.name}; review before use.")
    if out is not None:
        _write_profile(out, body["profile"])
    udid = target.target_id if target is not None else "<UDID>"
    if out is not None:
        settings = f"--value profile={out}"
    else:
        settings = f"--value preset={body['profile']['preset']}"
        if body["need"]:
            settings += " --value need=" + ",".join(body["need"])
    return {"control": CONTROL, "action": "suggest", "app": described, **body,
            "limitations": limitations, "written": str(out) if out is not None else None,
            "next": f"autonom simulator slim apply --platform ios --udid {udid} {settings}",
            "verified": True, "verification": "bundle_read"}


# --- apply / restore -----------------------------------------------------------------


def _apply(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    profile = ios_slim.profile_from_values(values)
    desired = ios_slim.resolve(profile)
    reboot = simulator._bool(values, "reboot", False)
    details = simulator._bool(values, "details", False)
    udid, sim_state = _device(target)
    runtime, persistent = _runtime(target, udid)
    if desired and not persistent:
        raise errors.AutonomError(
            errors.SLIM_UNSUPPORTED_RUNTIME,
            f"the runtime of simulator {udid} ({runtime or 'unknown'}) does not keep "
            "launchd overrides across a reboot; apply needs iOS 18.5 or newer",
            hint="'autonom simulator slim disable --value ids=<category,...>' stops "
                 "services live, for the current boot only.",
            target_id=udid, runtime=runtime)
    _refuse_in_flux(udid, sim_state)
    booted = sim_state == "Booted"
    result: dict[str, Any] = {"control": CONTROL, "action": "apply", "profile": profile,
                              "desired_disabled": len(desired), "persistent": persistent}
    old = ios_slim.read_state(udid)
    if booted:
        to_disable, to_enable = ios_slim.delta(_live_disabled(target, udid), desired)
        if not to_disable and not to_enable:
            # Already there: nothing is written and nothing is rebooted.
            if old is not None and (old.get("profile") != profile or any(
                    (old.get("live") or {}).values())):
                ios_slim.write_state(udid, {**old, "profile": profile,
                                            "live": {"enabled": [], "disabled": []}})
            result.update({"changed": 0, "disabled_now": 0, "enabled_now": 0,
                           "rebooted": False, "state_file": str(ios_slim.state_path(udid))
                           if ios_slim.read_state(udid) else None})
            return simulator._checked(result, True)
        _require_shutdown_or_reboot(udid, booted, reboot)
        _shutdown(target, udid)
    try:
        entries = ios_slim.read_store(udid)
        to_disable, to_enable = ios_slim.delta(ios_slim.disabled_in(entries), desired)
        owned_before = set(old["disabled"]) if old else set()
        previous_before = old["previous"] if old else {}
        # A label Autonom disabled earlier goes back to what it was; any other
        # label that must be on gets an explicit `false`.
        put_back = {label: previous_before.get(label) for label in to_enable
                    if label in owned_before and previous_before.get(label) is not True}
        planned = ios_slim.merged_entries(
            entries, disable=to_disable,
            enable=[label for label in to_enable if label not in put_back],
            restore=put_back)
        owned = (owned_before & desired) | set(to_disable)
        previous = {label: previous_before[label] if label in previous_before
                    else _scalar(entries.get(label)) for label in owned}
        state = ios_slim.new_state(udid, profile, owned, previous) if owned else None
        _commit(udid, entries, planned, state)
    except BaseException:
        if booted:
            _boot_back(target, udid)
        raise
    result.update({"changed": len(to_disable) + len(to_enable),
                   "disabled_now": len(to_disable), "enabled_now": len(to_enable),
                   "store": str(ios_slim.store_path(udid)),
                   "state_file": str(ios_slim.state_path(udid)) if state else None,
                   "rebooted": booted})
    if details:
        result.update({"disabled_labels": to_disable, "enabled_labels": to_enable})
    if not booted:
        # Nothing reads a shut-down simulator's launchd: the store is all
        # there is to say, and it is not proof.
        result["verified"] = False
        result["verification"] = "store_written" if planned != entries else "store_matches"
        return result
    result.update(_converge(target, udid, want_disabled=desired,
                            want_enabled=ios_slim.CATALOG_LABELS - desired))
    simulator._checked(result, True)
    _finish_reboot(target, result)
    return result


def _restore(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    reboot = simulator._bool(values, "reboot", False)
    everything = simulator._bool(values, "all", False)
    udid, sim_state = _device(target)
    result: dict[str, Any] = {"control": CONTROL, "action": "restore"}
    state = ios_slim.read_state(udid)
    result["state_file_found"] = state is not None
    if state is None and not everything:
        result.update({"changed": 0, "restored": 0, "rebooted": False, "verified": True,
                       "verification": "nothing_to_restore",
                       "note": "no slim state file for this simulator: Autonom has "
                               "nothing to undo. Pass --value all=true to re-enable "
                               "every catalog service that is disabled."})
        return result
    _refuse_in_flux(udid, sim_state)
    booted = sim_state == "Booted"
    entries = ios_slim.read_store(udid)
    live = _live_disabled(target, udid) if booted else frozenset()
    put_back = {label: (state["previous"].get(label))
                for label in (state["disabled"] if state else ())
                if label in ios_slim.CATALOG_LABELS}
    others: list[str] = []
    if everything:
        off = (ios_slim.disabled_in(entries) | live) & ios_slim.CATALOG_LABELS
        others = sorted(off - set(put_back))
    planned = ios_slim.merged_entries(entries, enable=others, restore=put_back)
    # what must read enabled afterwards (a label that was disabled before
    # Autonom touched it goes back to disabled, and is not expected on)
    expect_on = {label for label, value in put_back.items() if value is not True}
    expect_on |= set(others)
    if planned == entries and not (live & expect_on):
        _commit(udid, entries, entries, None)
        result.update({"changed": 0, "restored": 0, "rebooted": False})
        if booted:
            return simulator._checked(result, True)
        result.update({"verified": False, "verification": "store_matches"})
        return result
    _require_shutdown_or_reboot(udid, booted, reboot)
    if booted:
        _shutdown(target, udid)
    try:
        _commit(udid, entries, planned, None)
    except BaseException:
        if booted:
            _boot_back(target, udid)
        raise
    restored = sorted({*put_back, *others})
    result.update({"changed": len(restored), "restored": len(restored),
                   "store": str(ios_slim.store_path(udid)), "rebooted": booted})
    if simulator._bool(values, "details", False):
        result["restored_labels"] = restored
    if not booted:
        result.update({"verified": False, "verification": "store_written"})
        return result
    result.update(_converge(target, udid, want_disabled=(), want_enabled=expect_on))
    simulator._checked(result, True)
    _finish_reboot(target, result)
    return result


# --- slim at boot (`devices boot --slim`, `session start --slim`) -----------------------


def refuse_android(flag: str = "--slim") -> errors.AutonomError:
    return errors.AutonomError(
        errors.UNSUPPORTED_CAPABILITY,
        f"{flag} switches iOS Simulator background services off; an Android emulator "
        "has no such store",
        hint=f"Drop {flag}, or use it with --platform ios --udid <UDID>.",
        capability="simulator.slim")


def flag_values(value: str) -> dict[str, Any]:
    """`--slim <preset | profile path>` as `apply` values: one of the preset
    ids is a preset, anything else is a profile file."""
    value = (value or "").strip()
    if not value:
        raise ios_slim.invalid(
            "--slim needs a preset or a profile path",
            "Presets: " + ", ".join(ios_slim.PRESETS) + "; or pass a profile .json file.",
            field="slim", item="slim")
    return {"preset": value} if value in ios_slim.PRESETS else {"profile": value}


def boot(target: Target, value: str, *, timeout: float = 120) -> tuple[bool, dict[str, Any]]:
    """Boot one simulator slimmed: (this call booted it, the `slim` object of
    the reply). A shut-down simulator gets the profile written, is booted and
    read back; a booted one is left alone when it already matches and is
    refused otherwise."""
    if target.platform == ANDROID:
        raise refuse_android()
    values = flag_values(value)
    profile = ios_slim.profile_from_values(values)
    desired = ios_slim.resolve(profile)
    udid, sim_state = _device(target)
    _refuse_in_flux(udid, sim_state)
    block: dict[str, Any] = {"preset": profile["preset"],
                             "profile": values.get("profile")}
    if sim_state == "Booted":
        live = _live_disabled(target, udid)
        to_disable, to_enable = ios_slim.delta(live, desired)
        if to_disable or to_enable:
            raise errors.AutonomError(
                errors.SIMULATOR_MUST_BE_SHUTDOWN,
                f"simulator {udid} is already booted and does not match the slim "
                f"profile ({len(to_disable)} service(s) to disable, {len(to_enable)} "
                "to enable); launchd reads its overrides at boot",
                hint="Shut it down first ('autonom devices shutdown --udid <UDID>') and "
                     "retry, or run 'autonom simulator slim apply --value reboot=true' "
                     "with the same settings.",
                target_id=udid, to_disable=len(to_disable), to_enable=len(to_enable))
        summary = ios_slim.summarize(live, desired)
        block.update({"state": summary["state"], "disabled": summary["disabled"],
                      "verified": True, "verification": "read_back", "changed": 0,
                      "note": "already booted with this slim state; nothing changed"})
        return False, block
    applied = _apply(target, values)
    converged = _converge(target, udid, want_disabled=desired,
                          want_enabled=ios_slim.CATALOG_LABELS - desired,
                          boot_timeout=timeout)
    summary = ios_slim.summarize(_live_disabled(target, udid), desired)
    block.update({"state": summary["state"], "disabled": summary["disabled"],
                  "verified": True, "verification": "read_back",
                  "changed": applied.get("changed", 0),
                  "fallback": converged["fallback"]})
    return True, block


# --- live enable / disable --------------------------------------------------------------


def _ids(values: dict[str, Any]) -> list[str]:
    ids = ios_slim._as_list("ids", values.get("ids"))
    if not ids:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            "name at least one category id, feature id or service label",
            "Pass --value ids=<id,...>; 'autonom simulator slim presets' lists them.")
    return ids


def _require_booted(udid: str, sim_state: str, action: str) -> None:
    if sim_state != "Booted":
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            f"simulator {udid} is {sim_state}; 'simulator slim {action}' changes a "
            "booted simulator live",
            hint="Boot it first ('autonom devices boot --udid <UDID>'), or use "
                 "'simulator slim apply' to change a shut-down simulator.",
            capability="simulator.slim", target_id=udid, state=sim_state)


def _enable(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    labels, _named, _protected = ios_slim.resolve_ids(_ids(values))
    udid, sim_state = _device(target)
    _require_booted(udid, sim_state, "enable")
    tool = target.tool
    before = _live_disabled(target, udid)
    loaded = ios_simctl.launchctl_loaded_labels(tool, udid)
    root = ios_simctl.runtime_root(tool, ios_simctl.device_runtime_identifier(tool, udid))
    started: list[str] = []
    already: list[str] = []
    on_demand: list[str] = []
    pending: list[str] = []
    for label in sorted(labels):
        if label not in before and label in loaded:
            already.append(label)
            continue
        if label in before:
            ios_simctl.launchctl_enable(tool, udid, label)
        plist = ios_simctl.launchd_job_plist(root, label)
        if plist is None:
            # no job plist (an XPC service, say): launchd starts it on demand
            on_demand.append(label)
        elif ios_simctl.launchctl_bootstrap(tool, udid, plist):
            started.append(label)
        else:
            pending.append(label)
    if pending:
        loaded = ios_simctl.launchctl_loaded_labels(tool, udid)
        started.extend(label for label in pending if label in loaded)
    not_started = sorted(label for label in pending if label not in loaded)
    after = _live_disabled(target, udid)
    still_disabled = sorted(labels & after)
    enabled = sorted(labels - after)
    # the next boot must agree: flip the store entries too
    entries = ios_slim.read_store(udid)
    flip = [label for label in enabled if entries.get(label) is True]
    state = ios_slim.read_state(udid)
    if state is not None:
        gone = set(enabled)
        live = state.get("live") or {}
        state = {**state,
                 "disabled": [label for label in state["disabled"] if label not in gone],
                 "previous": {label: value for label, value in state["previous"].items()
                              if label not in gone},
                 "live": {"enabled": sorted(set(live.get("enabled") or ()) | gone),
                          "disabled": sorted(set(live.get("disabled") or ()) - gone)}}
    _commit(udid, entries, ios_slim.merged_entries(entries, enable=flip), state)
    result: dict[str, Any] = {
        "control": CONTROL, "action": "enable", "labels": len(labels),
        "started": sorted(started), "not_started": not_started,
        "already_running": already, "on_demand": on_demand,
        "still_disabled": still_disabled, "store_updated": len(flip), "rebooted": False}
    if not_started:
        result.setdefault("warnings", []).append({
            "code": "slim_not_started",
            "error": f"{len(not_started)} service(s) are enabled again and would not "
                     "load in this boot session",
            "hint": "They start at the next boot of the simulator."})
    return simulator._checked(result, not still_disabled)


def _disable(target: Target, values: dict[str, Any]) -> dict[str, Any]:
    labels, named, protected = ios_slim.resolve_ids(_ids(values))
    force = simulator._bool(values, "force", False)
    core_named = [label for label in named if label in ios_slim.CORE_KEEP]
    if core_named and not force:
        label = core_named[0]
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"{label} is kept on for {ios_slim.CORE_KEEP[label]}; it is not disabled "
            "without force=true",
            "Pass --value force=true to stop it anyway.", label=label)
    udid, sim_state = _device(target)
    _require_booted(udid, sim_state, "disable")
    tool = target.tool
    _runtime_id, persistent = _runtime(target, udid)
    kept_core = sorted(labels & set(ios_slim.CORE_KEEP)) if not force else []
    todo = sorted(labels - set(kept_core))
    entries_before = ios_slim.read_store(udid)
    still_running: list[str] = []
    for label in todo:
        ios_simctl.launchctl_disable(tool, udid, label)
        if not ios_simctl.launchctl_bootout(tool, udid, label):
            still_running.append(label)
    after = _live_disabled(target, udid)
    stopped = sorted(set(todo) & after)
    not_disabled = sorted(set(todo) - after)
    entries = ios_slim.read_store(udid)
    flip = [label for label in stopped if entries.get(label) is not True]
    old = ios_slim.read_state(udid)
    owned = set(old["disabled"]) if old else set()
    previous = dict(old["previous"]) if old else {}
    for label in stopped:
        if label not in owned and entries_before.get(label) is not True:
            owned.add(label)
            previous[label] = _scalar(entries_before.get(label))
    live = (old.get("live") or {}) if old else {}
    state = None
    if owned or old is not None:
        state = ios_slim.new_state(
            udid, old.get("profile") if old else None, owned, previous,
            {"enabled": set(live.get("enabled") or ()) - set(stopped),
             "disabled": set(live.get("disabled") or ()) | set(stopped)})
    _commit(udid, entries, ios_slim.merged_entries(entries, disable=flip), state)
    result: dict[str, Any] = {
        "control": CONTROL, "action": "disable", "labels": len(labels),
        "stopped": stopped,
        "still_enabled": sorted({*not_disabled, *kept_core, *protected}),
        "kept_core": {label: ios_slim.CORE_KEEP[label] for label in kept_core},
        "still_running": sorted(set(still_running) & set(stopped)),
        "store_updated": len(flip), "persistent": persistent, "rebooted": False}
    if kept_core:
        result.setdefault("warnings", []).append({
            "code": "slim_core_kept",
            "error": "core services were left running: " + ", ".join(kept_core),
            "hint": "Pass --value force=true to stop them as well."})
    return simulator._checked(result, not not_disabled)
