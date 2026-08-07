"""Android emulator proxy attach/detach (CAP-ATTACH-002, INV-07).

Attach points the emulator at the host's loopback proxy through `10.0.2.2`, the
address the emulator uses for the host loop-back interface. That is precisely why
a loopback-only bind is workable — and why physical devices are refused: reaching
them would require binding the proxy to a LAN address, turning the operator's
machine into an open proxy.

Detach restores the device's **exact** previous value rather than writing `:0`.
Blindly clearing would silently destroy a developer's corporate proxy setting.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import adb as adb_mod
from .. import consent, errors
from ..platform import Target
from . import proxy as proxy_mod

EMULATOR_HOST = "10.0.2.2"
SETTING = "http_proxy"
UNSET = ":0"

# Android's user CA store keys certificates by the OpenSSL "old" subject hash.
USER_CA_STORE = "/data/misc/user/0/cacerts-added"


def _subject_hash(certificate: Path) -> str:
    """The `<hash>.0` filename Android expects, via openssl.

    Recomputing OpenSSL's MD5-based `subject_hash_old` in pure Python is a
    liability; shelling out to openssl (present on macOS and virtually every
    Linux) is the honest choice, and a clear error beats a wrong hash.
    """
    openssl = shutil.which("openssl")
    if not openssl:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            "openssl is required to compute the Android CA filename",
            "Install openssl, or place the certificate manually.",
        )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [openssl, "x509", "-inform", "PEM", "-subject_hash_old", "-in", str(certificate)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    digest = (completed.stdout or "").strip().splitlines()
    if completed.returncode != 0 or not digest:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"could not read the CA subject hash: {(completed.stderr or '').strip()}",
            "Check that the certificate is a valid PEM.",
        )
    return digest[0]


def install_ca_certificate(
    target: Target, record: dict[str, Any], *, acknowledged: bool
) -> dict[str, Any]:
    """Add the MITM CA to the emulator's user trust store, behind consent.

    Mechanically universal and scriptable on a rootable image — `adb root` then
    a copy into the user CA store. The only barrier is that this is a privileged
    security change, so it takes the same consent gate as every other one; it is
    never silent. Mirrors the iOS `--install-ca` path, which shipped while this
    was left as a no-op.
    """
    certificate = proxy_mod.ca_certificate(record)
    if not certificate:
        raise errors.AutonomError(
            errors.PROXY_NOT_RUNNING,
            "no CA certificate has been generated yet",
            "Start the proxy first; mitmproxy writes its CA on first run.",
        )
    if not is_emulator(target):
        raise errors.AutonomError(
            errors.PHYSICAL_DEVICE_ATTACH_UNSUPPORTED,
            "CA install is emulator-only",
            "A physical device needs the certificate trusted through its own "
            "Settings UI; this path uses 'adb root', which physical devices refuse.",
        )

    operation = consent.Operation(
        kind="ca_install",
        target=f"android:{target.target_id}",
        effect=(
            f"add the MITM CA certificate {certificate.name} to the user trust store "
            f"of emulator {target.target_id} (via 'adb root'), so the proxy can decrypt "
            f"its TLS traffic"
        ),
        flags=("--i-understand-mitm", "--install-ca"),
    )
    entry = consent.require(operation, acknowledged=acknowledged)

    digest = _subject_hash(certificate)
    remote = f"{USER_CA_STORE}/{digest}.0"
    staging = f"/data/local/tmp/{digest}.0"

    rooted = adb_mod.run_adb(target.tool, ["root"], serial=target.target_id,
                             timeout=30, check=False)
    root_out = (getattr(rooted, "stdout", "") or "") + (getattr(rooted, "stderr", "") or "")
    if "cannot run as root" in root_out.lower() or "not permitted" in root_out.lower():
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            "adb root was refused — the emulator image is not rootable",
            "Use a 'google_apis' image (not 'google_apis_playstore'); a Play image "
            "blocks adb root, so trust a debug network_security_config instead.",
        )
    # adb root restarts adbd; wait for it to come back before pushing.
    adb_mod.run_adb(target.tool, ["wait-for-device"], serial=target.target_id,
                    timeout=60, check=False)
    adb_mod.run_adb(target.tool, ["push", str(certificate), staging],
                    serial=target.target_id, timeout=30, check=True)
    adb_mod.run_adb(
        target.tool,
        ["shell", "mkdir -p %s && cp %s %s && chown system:system %s && chmod 644 %s"
         % (USER_CA_STORE, staging, remote, remote, remote)],
        serial=target.target_id, timeout=30, check=True,
    )
    consent.record(record, entry)
    return {"installed": remote, "hash": digest, "certificate": str(certificate)}


def _get_setting(target: Target) -> str | None:
    completed = adb_mod.run_adb(
        target.tool, ["shell", "settings", "get", "global", SETTING],
        serial=target.target_id, timeout=15, check=False,
    )
    value = (completed.stdout or "").strip() if isinstance(completed.stdout, str) else ""
    if not value or value == "null":
        return None
    return value


def _put_setting(target: Target, value: str) -> None:
    adb_mod.run_adb(
        target.tool, ["shell", "settings", "put", "global", SETTING, value],
        serial=target.target_id, timeout=15, check=True,
    )


def apply_proxy_setting(target: Target) -> dict[str, Any]:
    """Make the framework actually adopt the proxy that was just written.

    `settings put global http_proxy` only writes a row. ConnectivityService
    reads it through `ProxyTracker` at startup, so a value written afterwards is
    stored and ignored: `settings get` echoes it back, `attach` reports success,
    and not one byte reaches the proxy. That cost an hour of live debugging —
    every component reported success while nothing worked, which is the failure
    mode this project exists to prevent.

    Cycling Wi-Fi forces the network to be re-evaluated and the proxy adopted.
    It briefly drops connectivity on the device, which is why it is reported
    rather than done quietly, and why `--no-network-cycle` exists for a caller
    who has already arranged the re-read another way.
    """
    result: dict[str, Any] = {"method": "wifi_cycle", "applied": False}
    for action in ("disable", "enable"):
        completed = adb_mod.run_adb(
            target.tool, ["shell", "svc", "wifi", action],
            serial=target.target_id, timeout=20, check=False,
        )
        if getattr(completed, "returncode", 0) not in (0, None):
            result["error"] = f"svc wifi {action} failed"
            return result
        if action == "disable":
            time.sleep(2)
    time.sleep(6)
    result["applied"] = True
    return result


def is_emulator(target: Target) -> bool:
    if target.target_id.startswith("emulator-"):
        return True
    completed = adb_mod.run_adb(
        target.tool, ["shell", "getprop", "ro.kernel.qemu"],
        serial=target.target_id, timeout=15, check=False,
    )
    value = (completed.stdout or "").strip() if isinstance(completed.stdout, str) else ""
    return value == "1"


def attach(
    target: Target,
    record: dict[str, Any],
    *,
    port: int,
    acknowledged: bool,
    network_cycle: bool = True,
) -> dict[str, Any]:
    if not is_emulator(target):
        raise errors.AutonomError(
            errors.PHYSICAL_DEVICE_ATTACH_UNSUPPORTED,
            f"{target.target_id} is not an emulator",
            "The proxy binds to 127.0.0.1, which a physical device cannot reach. "
            "Widening the bind would expose an open proxy on your network and is out "
            "of scope for this version; use an emulator, or configure the device's "
            "Wi-Fi proxy by hand.",
        )

    device_proxy = f"{EMULATOR_HOST}:{port}"
    operation = consent.Operation(
        kind="device_proxy",
        target=f"android:{target.target_id}",
        effect=(
            f"set the emulator's global HTTP proxy to {device_proxy}, routing its "
            f"traffic through a local MITM proxy until detached"
            + (", and cycle its Wi-Fi so the framework adopts the setting "
               "(brief loss of connectivity on the device)" if network_cycle else "")
        ),
        flags=("--i-understand-mitm",),
    )
    entry = consent.require(operation, acknowledged=acknowledged)

    previous = _get_setting(target)
    _put_setting(target, device_proxy)
    applied = apply_proxy_setting(target) if network_cycle else {
        "method": "none", "applied": False,
        "hint": "The framework adopts a proxy written this way only after the "
                "network is re-evaluated; without that, nothing reaches the proxy.",
    }

    network = record.setdefault("network", {})
    network.update({
        "enabled": True,
        "proxy_host": "127.0.0.1",
        "proxy_port": port,
        "device_proxy": device_proxy,
        "attached": True,
        "previous_http_proxy": previous,
    })
    consent.record(record, entry)
    result = {"attached": True, "device_proxy": device_proxy,
              "previous_http_proxy": previous, "setting_applied": applied}
    if not applied.get("applied"):
        result["warnings"] = [{
            "code": "proxy_setting_not_applied",
            "error": "the proxy was written but the framework was not made to adopt it",
            "hint": applied.get("hint") or "Re-run without --no-network-cycle, or "
                                          "cycle the device's Wi-Fi by hand.",
        }]
    return result


def detach(target: Target, record: dict[str, Any]) -> dict[str, Any]:
    """Idempotent, and restores the value observed at attach time."""
    network = record.setdefault("network", {})
    if not network.get("attached"):
        return {"was_attached": False}

    previous = network.get("previous_http_proxy")
    restore = previous if previous else UNSET
    _put_setting(target, restore)

    network.update({"attached": False, "device_proxy": None, "previous_http_proxy": None})
    return {"was_attached": True, "restored_http_proxy": previous, "wrote": restore}


def observed_setting(target: Target) -> str | None:
    try:
        return _get_setting(target)
    except errors.AutonomError:
        return None
