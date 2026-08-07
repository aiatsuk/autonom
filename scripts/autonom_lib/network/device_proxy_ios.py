"""iOS Simulator proxy attach (CAP-ATTACH-004, DEC-006, N-13).

The Simulator uses the **host Mac's** network stack, so the obvious way to route
its traffic is to change macOS network-service settings. Autonom refuses to do
that: it is a system-wide network change whose blast radius is the operator's
whole machine, and the consent rule forbids an agent making it autonomously.

What is used instead, in priority order:

1. **Per-process proxy environment at launch.** `simctl launch` forwards any
   `SIMCTL_CHILD_*` variable into the app. Clients that honour proxy environment
   variables — notably Dart's `HttpClient.findProxyFromEnvironment`, which Flutter
   apps use — then route through the proxy. Native `URLSession` reads the *system*
   proxy configuration, not the environment, so it is **not** covered.
2. **Trust-store seeding**, behind its own `--install-ca` flag:
   `xcrun simctl keychain <udid> add-root-cert <cert>` adds the MITM CA to the
   simulator's trusted root store. Scoped to one simulator; the host is untouched.
3. **Documented manual steps** plus a `network status` healthcheck, when neither
   of the above applies.

The mode actually used is always reported, and `attached` is never claimed
without evidence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import consent, errors, ios_simctl
from ..platform import Target
from . import proxy as proxy_mod

PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

URLSESSION_CAVEAT = (
    "The per-process proxy covers clients that honour proxy environment variables "
    "(Dart/Flutter HttpClient.findProxyFromEnvironment, curl, many SDKs). Native "
    "URLSession reads the system proxy configuration instead and will NOT be "
    "captured this way."
)


def proxy_environment(port: int, host: str = "127.0.0.1") -> dict[str, str]:
    endpoint = f"http://{host}:{port}"
    return {key: endpoint for key in PROXY_ENV_KEYS}


def manual_steps(port: int, certificate: Path | None) -> list[str]:
    steps = [
        f"1. Keep the proxy running on 127.0.0.1:{port}.",
        "2. In the Simulator, open Settings → Wi-Fi → (i) → Configure Proxy → Manual, "
        f"   then set Server 127.0.0.1 and Port {port}.",
    ]
    if certificate:
        steps.append(
            f"3. Trust the CA: xcrun simctl keychain <udid> add-root-cert {certificate} "
            "(or re-run attach with --install-ca)."
        )
    steps.append(
        "4. Exercise the app, then run 'autonom network status' — it reports attached "
        "only once traffic has actually been observed."
    )
    return steps


def install_ca_certificate(target: Target, record: dict[str, Any], *, acknowledged: bool) -> Path:
    certificate = proxy_mod.ca_certificate(record)
    if not certificate:
        raise errors.AutonomError(
            errors.PROXY_NOT_RUNNING,
            "no CA certificate has been generated yet",
            "Start the proxy first; mitmproxy writes its CA on first run.",
        )
    operation = consent.Operation(
        kind="ca_install",
        target=f"ios:{target.target_id}",
        effect=(
            f"add the MITM CA certificate {certificate.name} to the trusted root store "
            f"of simulator {target.target_id}, so the proxy can decrypt its TLS traffic"
        ),
        flags=("--i-understand-mitm", "--install-ca"),
    )
    entry = consent.require(operation, acknowledged=acknowledged)
    ios_simctl.run_simctl(
        target.tool, ["keychain", target.target_id, "add-root-cert", str(certificate)],
        timeout=60,
    )
    consent.record(record, entry)
    return certificate


def attach(
    target: Target,
    record: dict[str, Any],
    *,
    port: int,
    acknowledged: bool,
    install_ca: bool = False,
) -> dict[str, Any]:
    operation = consent.Operation(
        kind="device_proxy",
        target=f"ios:{target.target_id}",
        effect=(
            f"route apps launched by Autonom on simulator {target.target_id} through the "
            f"local MITM proxy on 127.0.0.1:{port} by injecting proxy environment variables"
        ),
        flags=("--i-understand-mitm",),
    )
    entry = consent.require(operation, acknowledged=acknowledged)

    certificate = None
    if install_ca:
        certificate = install_ca_certificate(target, record, acknowledged=acknowledged)

    environment = proxy_environment(port)
    network = record.setdefault("network", {})
    network.update({
        "enabled": True,
        "proxy_host": "127.0.0.1",
        "proxy_port": port,
        "device_proxy": f"127.0.0.1:{port}",
        "attached": True,
        "previous_http_proxy": None,
        "launch_env": environment,
        "ca_installed": bool(certificate),
        # No traffic has been seen yet, so status must not claim success.
        "platform_manual": not install_ca,
    })
    consent.record(record, entry)

    return {
        "mode": "automated",
        "mechanism": "per-process proxy environment injected at 'session launch'",
        "device_proxy": f"127.0.0.1:{port}",
        "ca_installed": bool(certificate),
        "ca_certificate": str(certificate) if certificate else None,
        "attached": "unknown",
        "warnings": [{
            "code": "urlsession_not_covered",
            "error": URLSESSION_CAVEAT,
            "hint": "For native URLSession traffic, follow the manual proxy steps below.",
        }],
        "manual_steps": manual_steps(port, proxy_mod.ca_certificate(record)),
        "next_action": (
            "Relaunch the app with 'autonom session launch <bundle>' so the proxy "
            "environment is applied, exercise it, then run 'autonom network status'."
        ),
    }


def detach(target: Target, record: dict[str, Any]) -> dict[str, Any]:
    """Idempotent. Nothing host-wide was changed, so nothing host-wide is restored.

    A CA seeded into the simulator's trust store is deliberately left in place:
    removing it is a second privileged trust-store operation and needs its own
    consent. `simctl keychain <udid> reset` (or erasing the simulator) clears it.
    """
    network = record.setdefault("network", {})
    if not network.get("attached"):
        return {"was_attached": False}
    ca_installed = network.get("ca_installed")
    network.update({"attached": False, "device_proxy": None, "launch_env": None,
                    "platform_manual": False})
    result: dict[str, Any] = {"was_attached": True, "restored_http_proxy": None}
    if ca_installed:
        result["note"] = (
            f"The MITM CA remains in simulator {target.target_id}'s trust store. "
            f"Remove it with 'xcrun simctl keychain {target.target_id} reset' or by "
            "erasing the simulator."
        )
    return result


def launch_environment(record: dict[str, Any]) -> dict[str, str]:
    """Proxy variables to inject at `session launch`, or an empty mapping."""
    network = record.get("network") or {}
    if not network.get("attached"):
        return {}
    return dict(network.get("launch_env") or {})
