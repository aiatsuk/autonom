"""Platform-neutral target identity and resolution (CAP-PLAT-001, CAP-PLAT-002).

One precedence order serves both platforms, so `autonom ui tap --text Continue`
means the same thing whether the target is an Android emulator or an iOS
simulator:

1. explicit ``--platform`` and ``--target``
2. ``--target`` alone, when the id is unambiguous across platforms
3. ``--serial`` (implies android) or ``--udid`` (implies ios)
4. the active session record
5. the sole ready target across both platforms
6. otherwise an error that lists the candidates

``--serial`` is a permanent Android alias (DEC-004): responses keep emitting
``serial`` alongside ``target_id`` so every 0.4.0 caller keeps working.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

from . import adb as adb_mod
from . import errors
from . import ios_simctl

ANDROID = "android"
IOS = "ios"
PLATFORMS = (ANDROID, IOS)

# adb reports many states; only these can accept commands.
ANDROID_READY = {"device"}
IOS_READY = {"Booted"}


@dataclass(frozen=True)
class Target:
    platform: str
    target_id: str
    tool: str
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def serial(self) -> str | None:
        return self.aliases.get("serial")

    @property
    def udid(self) -> str | None:
        return self.aliases.get("udid")

    def identity(self) -> dict[str, Any]:
        """The identity block every device-touching response carries."""
        payload: dict[str, Any] = {"platform": self.platform, "target_id": self.target_id}
        if self.platform == ANDROID:
            payload["serial"] = self.target_id
        return payload


def _android_target(serial: str, adb_path: str) -> Target:
    return Target(ANDROID, serial, adb_path, {"serial": serial})


def _ios_target(udid: str, xcrun_path: str) -> Target:
    return Target(IOS, udid, xcrun_path, {"udid": udid})


def _flag(args: argparse.Namespace, name: str) -> Any:
    return getattr(args, name, None)


TOOL_ENV = {
    "adb": "AUTONOM_ADB",
    "simctl": "AUTONOM_SIMCTL",
    "idb": "AUTONOM_IDB",
}


def apply_tool_overrides(args: argparse.Namespace) -> None:
    """Publish `--adb/--simctl/--idb` into the environment.

    Backends resolve their binaries independently (a lazily imported module must
    not need a Namespace threaded through six call sites), so an explicit flag is
    promoted to the same environment override those resolvers already honour.
    """
    import os

    for flag, variable in TOOL_ENV.items():
        value = _flag(args, flag)
        if value:
            os.environ[variable] = str(value)
    endpoint_host = _flag(args, "idb_host")
    if endpoint_host:
        port = _flag(args, "idb_port") or 10882
        os.environ["AUTONOM_IDB_COMPANION"] = f"{endpoint_host}:{port}"


def _check_conflicts(args: argparse.Namespace) -> None:
    platform = _flag(args, "platform")
    target = _flag(args, "target")
    serial = _flag(args, "serial")
    udid = _flag(args, "udid")

    if serial and udid:
        raise errors.AutonomError(
            errors.CONFLICTING_TARGET_FLAGS,
            "--serial and --udid select different platforms",
            "Pass one of them, or use --platform with --target.",
        )
    if platform == IOS and serial:
        raise errors.AutonomError(
            errors.CONFLICTING_TARGET_FLAGS,
            "--platform ios cannot be combined with --serial (an Android alias)",
            "Use --target <udid> or --udid <udid> for iOS.",
        )
    if platform == ANDROID and udid:
        raise errors.AutonomError(
            errors.CONFLICTING_TARGET_FLAGS,
            "--platform android cannot be combined with --udid (an iOS alias)",
            "Use --target <serial> or --serial <serial> for Android.",
        )
    for alias, value in (("--serial", serial), ("--udid", udid)):
        if value and target and value != target:
            raise errors.AutonomError(
                errors.CONFLICTING_TARGET_FLAGS,
                f"{alias}={value} disagrees with --target={target}",
                "Pass a single target identifier.",
            )
    if platform is not None and platform not in PLATFORMS:
        raise errors.AutonomError(
            errors.UNKNOWN_PLATFORM,
            f"unknown platform: {platform}",
            "Valid platforms: android, ios.",
        )


def list_all(
    args: argparse.Namespace | None = None,
    *,
    only: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Unified device inventory (CAP-PLAT-002).

    Returns ``(devices, warnings)``. A platform whose toolchain is missing
    contributes a warning rather than failing the whole listing (DEC-011) —
    on a Mac with Xcode and no Android SDK the old behavior made `devices`
    useless. Requesting that platform explicitly still raises.
    """
    args = args or argparse.Namespace()
    devices: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for platform in PLATFORMS:
        if only and only != platform:
            continue
        try:
            if platform == ANDROID:
                adb_path = adb_mod.find_adb(_flag(args, "adb"))
                devices.extend(device.as_dict() for device in adb_mod.list_devices(adb_path))
            else:
                xcrun_path = ios_simctl.find_simctl(_flag(args, "simctl"))
                devices.extend(sim.as_dict() for sim in ios_simctl.list_devices(xcrun_path))
        except errors.AutonomError as exc:
            if only == platform:
                raise
            warnings.append({"platform": platform, "error_code": exc.code, "error": exc.message,
                             "hint": exc.hint})
    return devices, warnings


def _ready_targets(args: argparse.Namespace) -> list[Target]:
    found: list[Target] = []
    try:
        adb_path = adb_mod.find_adb(_flag(args, "adb"))
        found.extend(
            _android_target(device.serial, adb_path)
            for device in adb_mod.list_devices(adb_path)
            if device.state in ANDROID_READY
        )
    except errors.AutonomError:
        pass
    try:
        xcrun_path = ios_simctl.find_simctl(_flag(args, "simctl"))
        found.extend(
            _ios_target(sim.udid, xcrun_path)
            for sim in ios_simctl.list_devices(xcrun_path)
            if sim.state in IOS_READY
        )
    except errors.AutonomError:
        pass
    return found


def _match_by_id(args: argparse.Namespace, target_id: str) -> Target | None:
    """Find which platform owns an id when only --target was supplied."""
    matches: list[Target] = []
    try:
        adb_path = adb_mod.find_adb(_flag(args, "adb"))
        if any(device.serial == target_id for device in adb_mod.list_devices(adb_path)):
            matches.append(_android_target(target_id, adb_path))
    except errors.AutonomError:
        adb_path = None
    try:
        xcrun_path = ios_simctl.find_simctl(_flag(args, "simctl"))
        if ios_simctl.find_simulator(xcrun_path, target_id):
            matches.append(_ios_target(target_id, xcrun_path))
    except errors.AutonomError:
        xcrun_path = None

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise errors.AutonomError(
            errors.AMBIGUOUS_TARGET,
            f"target {target_id} exists on more than one platform",
            "Add --platform android or --platform ios.",
        )
    # Unknown id: a UDID shape is unmistakably iOS; otherwise assume Android so
    # offline/fixture flows keep working when no device is attached.
    looks_like_udid = len(target_id) == 36 and target_id.count("-") == 4
    if looks_like_udid:
        return _ios_target(target_id, ios_simctl.find_simctl(_flag(args, "simctl")))
    return _android_target(target_id, adb_mod.find_adb(_flag(args, "adb")))


def _from_platform_and_id(args: argparse.Namespace, platform: str, target_id: str) -> Target:
    if platform == ANDROID:
        return _android_target(target_id, adb_mod.find_adb(_flag(args, "adb")))
    return _ios_target(target_id, ios_simctl.find_simctl(_flag(args, "simctl")))


def resolve(args: argparse.Namespace, *, session_record: dict[str, Any] | None = None) -> Target:
    """Resolve exactly one target, or raise with the candidate list."""
    _check_conflicts(args)
    platform = _flag(args, "platform")
    target = _flag(args, "target")
    serial = _flag(args, "serial")
    udid = _flag(args, "udid")

    # 1 + 2: explicit identifiers.
    explicit_id = target or serial or udid
    if explicit_id:
        if platform:
            return _from_platform_and_id(args, platform, explicit_id)
        if serial:
            return _android_target(serial, adb_mod.find_adb(_flag(args, "adb")))
        if udid:
            return _ios_target(udid, ios_simctl.find_simctl(_flag(args, "simctl")))
        return _match_by_id(args, explicit_id)

    # 4: the active session.
    if session_record:
        record_platform = session_record.get("platform") or ANDROID
        record_id = session_record.get("target_id") or session_record.get("serial")
        if record_id and (platform is None or platform == record_platform):
            return _from_platform_and_id(args, record_platform, record_id)

    # 5: a single ready target.
    ready = _ready_targets(args)
    if platform:
        ready = [item for item in ready if item.platform == platform]
    if len(ready) == 1:
        return ready[0]
    if not ready:
        raise errors.AutonomError(
            errors.NO_TARGET,
            "no ready target found",
            "Start an emulator or boot a simulator, then run 'autonom devices'.",
        )
    listed = ", ".join(f"{item.platform}:{item.target_id}" for item in ready)
    raise errors.AutonomError(
        errors.AMBIGUOUS_TARGET,
        f"{len(ready)} ready targets; pass --target (or --platform)",
        f"Candidates: {listed}",
        candidates=[{"platform": item.platform, "target_id": item.target_id} for item in ready],
    )
