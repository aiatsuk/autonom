"""Typed provider façade over the existing Android/iOS control backends."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import errors
from .contracts import Capability, CapabilitySnapshot, utc_now
from .platform import ANDROID, IOS, Target


SEMANTIC_CAPABILITIES = (
    "ui.accessibility", "ui.input", "screenshots", "screen.stream", "logs",
    "network.capture", "checkpoint.create", "checkpoint.restore",
    "simulator.location", "simulator.permissions", "simulator.clipboard",
    "simulator.appearance", "simulator.text_size", "simulator.status_bar",
    "simulator.battery", "simulator.network", "simulator.push",
    "simulator.sms", "simulator.call", "simulator.biometric",
)


class DeviceSession(Protocol):
    target: Target
    record: dict[str, Any]

    def capabilities(self) -> CapabilitySnapshot: ...
    def checkpoint(self, name: str) -> dict[str, Any]: ...
    def restore(self, checkpoint: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class LocalDeviceSession:
    target: Target
    record: dict[str, Any]

    def capabilities(self) -> CapabilitySnapshot:
        tooling = self.record.get("tooling") or {}
        network = self.record.get("network") or {}
        is_android = self.target.platform == ANDROID
        idb_state = (tooling.get("idb") or {}).get("state")
        if not is_android and idb_state is None:
            try:
                from . import ios_idb
                ios_idb.find_idb()
                idb_state = "ready"
            except errors.AutonomError:
                idb_state = "missing"
        ui_ready = is_android or idb_state == "ready"
        simulated = self._device_class() == "simulator"
        values: dict[str, tuple[str, str | None]] = {
            "ui.accessibility": ("available" if ui_ready else "unavailable",
                                 None if ui_ready else "idb is not ready"),
            "ui.input": ("available" if ui_ready else "unavailable",
                         None if ui_ready else "UI backend is not ready"),
            "screenshots": ("available", None),
            "screen.stream": ("available", None),
            "logs": ("available", None),
            "network.capture": (
                "available" if network.get("attached") else "unavailable",
                None if network.get("attached") else "capture proxy is not attached"),
            "checkpoint.create": (
                "degraded" if simulated else "unavailable",
                "portable flow-prefix checkpoint" if simulated
                else "physical devices cannot be snapshotted"),
            "checkpoint.restore": (
                "degraded" if simulated else "unavailable",
                "replay from flow start" if simulated
                else "physical devices require baseline replay"),
        }
        supported_simulator = {
            "simulator.location", "simulator.permissions", "simulator.clipboard",
            "simulator.appearance", "simulator.text_size", "simulator.status_bar",
            "simulator.battery", "simulator.biometric",
        }
        if is_android:
            supported_simulator |= {"simulator.network", "simulator.sms", "simulator.call"}
        else:
            supported_simulator |= {"simulator.push"}
        for name in SEMANTIC_CAPABILITIES:
            if not name.startswith("simulator."):
                continue
            available = simulated and name in supported_simulator
            values[name] = (
                "available" if available else "unavailable",
                None if available else (
                    "requires an emulator or simulator" if not simulated
                    else "the platform exposes no provider-neutral control"),
            )
        capabilities = tuple(
            Capability(name, state, reason)
            for name, (state, reason) in sorted(values.items())
        )
        return CapabilitySnapshot(
            provider=f"local.{self.target.platform}",
            target_id=self.target.target_id,
            device_class=self._device_class(), captured_at=utc_now(),
            capabilities=capabilities,
        )

    def _device_class(self) -> str:
        if self.target.platform == IOS:
            return "simulator"
        serial = self.target.target_id
        return "simulator" if serial.startswith("emulator-") else "physical"

    def checkpoint(self, name: str) -> dict[str, Any]:
        snapshot = self.capabilities()
        if not snapshot.supports("checkpoint.create"):
            raise errors.AutonomError(
                errors.UNSUPPORTED_CAPABILITY,
                "this target cannot create a checkpoint",
                hint="Use baseline replay from the start of the flow.",
                capability="checkpoint.create", device_class=snapshot.device_class,
            )
        return {
            "kind": "portable-prefix", "name": name,
            "provider": snapshot.provider, "target_id": snapshot.target_id,
            "created_at": utc_now(),
            "note": "The portable checkpoint restores by replaying the pinned prefix.",
        }

    def restore(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        if checkpoint.get("target_id") not in (None, self.target.target_id):
            raise errors.AutonomError(
                errors.REPLAY_MANIFEST_INVALID,
                "checkpoint target does not match the active target",
                expected=checkpoint.get("target_id"), actual=self.target.target_id,
            )
        if checkpoint.get("kind") != "portable-prefix":
            raise errors.AutonomError(
                errors.UNSUPPORTED_CAPABILITY,
                "this provider cannot restore the requested native checkpoint",
                hint="Use a portable-prefix checkpoint or baseline replay.",
            )
        return {"restored": False, "mode": "baseline-replay-required",
                "checkpoint": checkpoint.get("name")}


def open_session(target: Target, record: dict[str, Any]) -> LocalDeviceSession:
    return LocalDeviceSession(target=target, record=record)


def preflight(target: Target, record: dict[str, Any],
              required: list[str]) -> CapabilitySnapshot:
    snapshot = open_session(target, record).capabilities()
    unknown = sorted(set(required) - set(SEMANTIC_CAPABILITIES))
    if unknown:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "flow declares unknown semantic capabilities",
            hint="Use capabilities listed by 'autonom doctor'.", capabilities=unknown,
        )
    missing = snapshot.missing(required)
    if missing:
        by_name = {item.name: item.as_dict() for item in snapshot.capabilities}
        raise errors.AutonomError(
            errors.FLOW_REQUIREMENTS_UNMET,
            "active provider does not satisfy required capabilities: "
            + ", ".join(missing),
            hint="Inspect the immutable capability snapshot and select a compatible target.",
            missing_capabilities=missing,
            capability_details=[by_name[name] for name in missing],
        )
    return snapshot
