"""Is the target's traffic really going through the proxy? (`network status`)

`network status` used to answer `attached: true, evidence: recent_flows` for
any flow seen in the last minute — and a `curl` run on the host through the
session's proxy is a flow. During a live iOS run every recorded flow came from
the host's curl while the app under test sent nothing, and status still said
attached.

Only a flow whose *client* is the target counts now:

- **Android emulator.** A flow from the guest network (``10.0.2.x``) is the
  emulator's. The emulator's user-mode NAT usually presents guest connections
  to the host as loopback, though, which is indistinguishable from a host
  process, so loopback flows are reported as unattributed and the device's
  own proxy setting — read back from the device — decides instead.
- **iOS Simulator.** It shares the host Mac's network stack: its connections
  and a host process's arrive from the same loopback address. No flow can be
  attributed, so traffic makes the answer ``"unknown"`` with the reason, never
  ``true``.

The addon records each flow's client address as ``client_ip``; flows recorded
before that field existed carry none and are likewise unattributed.
"""
from __future__ import annotations

from typing import Any, Callable

from . import store

ANDROID = "android"
IOS = "ios"
EMULATOR_GUEST_PREFIX = "10.0.2."
CLEARED_SETTING = ":0"  # what `settings put global http_proxy :0` leaves: no proxy
RECENT_SECONDS = 60
_SAMPLE_AGENTS = 5

IOS_UNATTRIBUTABLE = (
    "The iOS Simulator shares the host Mac's network stack, so the app's flows "
    "and a host process's (curl, a browser) arrive from the same loopback "
    "address and cannot be told apart."
)
ANDROID_LOOPBACK = (
    "Loopback flows may come from the emulator (its NAT presents guest "
    "connections as loopback) or from any host process; they are not "
    "counted as the device's."
)


def _normalise(ip: str) -> str:
    ip = ip.strip().lower()
    if ip.startswith("::ffff:"):
        ip = ip[len("::ffff:"):]
    return ip


def client_origin(flow: dict[str, Any]) -> str:
    """``android_emulator`` | ``loopback`` | ``remote`` | ``unknown``."""
    raw = flow.get("client_ip")
    if not isinstance(raw, str) or not raw.strip():
        return "unknown"
    ip = _normalise(raw)
    if ip.startswith(EMULATOR_GUEST_PREFIX):
        return "android_emulator"
    if ip.startswith("127.") or ip in ("::1", "localhost"):
        return "loopback"
    return "remote"


def attributed_flows(flows: list[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    """The flows provably sent by the target (never any on iOS)."""
    if platform != ANDROID:
        return []
    return [flow for flow in flows if client_origin(flow) == "android_emulator"]


def _user_agents(flows: list[dict[str, Any]]) -> list[str]:
    agents: list[str] = []
    for flow in reversed(flows):
        headers = flow.get("request_headers_preview") or {}
        agent = headers.get("user-agent") if isinstance(headers, dict) else None
        if isinstance(agent, str) and agent and agent not in agents:
            agents.append(agent[:120])
        if len(agents) >= _SAMPLE_AGENTS:
            break
    return agents


def attachment_evidence(
    record: dict[str, Any],
    *,
    platform: str,
    flows: list[dict[str, Any]] | None = None,
    observe_setting: Callable[[], str | None] | None = None,
    since_seconds: float = RECENT_SECONDS,
) -> dict[str, Any]:
    """What `network status` may claim about the attachment, and why.

    Returns ``attached`` (``True`` / ``False`` / ``"unknown"`` — the value
    types `network status` has always used), ``evidence`` (the existing
    vocabulary plus ``target_flows``, ``host_traffic_indistinguishable`` and
    ``setting_unreadable``), an optional ``reason``, and the counts behind it:
    ``recent_flow_count``, ``target_flow_count``, ``unattributed_flow_count``
    and a few ``recent_user_agents`` so a reader can judge the unattributed
    traffic themselves.

    `observe_setting` reads the device's current proxy setting (Android)
    and must *raise* when the device cannot be read —
    `device_proxy_android.read_setting` does; `observed_setting` folds a
    failure into None, which would read as "cleared". It is only called when
    no flow decides the question. ``recent_flows`` is never answered any
    more: a flow is evidence only when its client is the target.
    """
    network = record.get("network") or {}
    result: dict[str, Any] = {"attached": False, "evidence": "not_attached",
                              "recent_flow_count": 0, "target_flow_count": 0,
                              "unattributed_flow_count": 0}
    if flows is None:
        flows, _warnings = store.read_all(record)
    recent = store.filter_flows(flows, since_seconds=since_seconds)
    mine = attributed_flows(recent, platform)
    result.update(recent_flow_count=len(recent), target_flow_count=len(mine),
                  unattributed_flow_count=len(recent) - len(mine))
    if recent and len(mine) < len(recent):
        result["recent_user_agents"] = _user_agents(
            [flow for flow in recent if flow not in mine])
    if not network.get("attached"):
        return result

    if mine:
        result.update(attached=True, evidence="target_flows")
        return result

    if platform == IOS:
        if recent:
            result.update(attached="unknown", evidence="host_traffic_indistinguishable",
                          reason=IOS_UNATTRIBUTABLE)
        elif network.get("platform_manual"):
            result.update(attached="unknown", evidence="manual_attach_unverified")
        else:
            result.update(attached="unknown", evidence="no_traffic_and_no_readable_setting")
        return result

    observed: str | None = None
    if observe_setting is not None:
        try:
            observed = observe_setting()
        except Exception:  # noqa: BLE001 - an unreadable device is an answer, not a crash
            result.update(attached="unknown", evidence="setting_unreadable")
            if recent:
                result["reason"] = ANDROID_LOOPBACK
            return result
    if observed and observed == network.get("device_proxy"):
        result.update(attached=True, evidence="device_setting")
    elif observe_setting is not None and observed in (None, CLEARED_SETTING):
        result.update(attached=False, evidence="device_proxy_cleared_externally")
    else:
        result.update(attached="unknown", evidence="no_traffic_and_no_readable_setting")
    if recent:
        result["reason"] = ANDROID_LOOPBACK
    return result
