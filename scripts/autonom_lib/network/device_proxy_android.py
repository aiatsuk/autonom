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

import shlex
import shutil
import socket
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
# Android also persists a canonical proxy separate from the legacy http_proxy row.
PROXY_SETTINGS = ("global_http_proxy_host", "global_http_proxy_port",
                  "global_http_proxy_exclusion_list", "global_proxy_pac_url")

# Android's user CA store keys certificates by the OpenSSL "old" subject hash.
USER_CA_STORE = "/data/misc/user/0/cacerts-added"

# System trust stores. Apps targeting API 24+ do not trust the user store, so a
# transparent (no-app-change) capture has to land the CA here instead. From
# API 34 the store apps read lives in the conscrypt APEX and is bind-mounted per
# zygote mount namespace; before that it is the writable /system copy.
SYSTEM_CA_STORE = "/system/etc/security/cacerts"
APEX_CA_STORE = "/apex/com.android.conscrypt/cacerts"
APEX_MIN_API = 34
CA_STAGING = "/data/local/tmp/ca-copy"

# Capabilities reported when the transparent path is refused. New error *codes*
# are owned by errors.py; these reuse `unsupported_capability` with a capability
# extra, the pattern the rest of the CLI already uses for "not here".
SYSTEM_CA_CAPABILITY = "network.system_ca"
TRANSPARENT_CAPABILITY = "network.transparent_capture"


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


# --- Transparent capture: system-CA install on a rooted emulator --------------
#
# The proven live mechanism (WoolBox, a Flutter/Dio app, decrypted with zero app
# changes): the emulator is LAUNCHED routed through the proxy (`-http-proxy`, see
# emulator.boot_avd), and the MITM CA is added to the SYSTEM trust store via root
# — reversibly, with a tmpfs over the store and, on API>=34, a bind of that
# tmpfs into each zygote mount namespace so apps see it. A reboot clears it all.


def device_api_level(target: Target) -> int | None:
    """`ro.build.version.sdk` as an int, or None when it cannot be read.

    The store to seed and the way to make apps see it are API-dependent, so the
    level is a hard input, not a guess.
    """
    completed = adb_mod.run_adb(
        target.tool, ["shell", "getprop", "ro.build.version.sdk"],
        serial=target.target_id, timeout=15, check=False,
    )
    text = (completed.stdout or "").strip() if isinstance(completed.stdout, str) else ""
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _confirm_root(target: Target) -> tuple[bool, str]:
    """Best-effort `adb root`, then prove it with `id -u` == 0.

    A userdebug `google_apis` image already runs adbd as uid 0, so `adb root`
    is a no-op there; a Play image (or a production build) refuses it. Either
    way the truth is `id -u`, not adb root's message. Returns (is_root, the
    lower-cased root output so the caller can tell a refusal from an absence).
    """
    rooted = adb_mod.run_adb(target.tool, ["root"], serial=target.target_id,
                             timeout=30, check=False)
    root_out = ((getattr(rooted, "stdout", "") or "")
                + (getattr(rooted, "stderr", "") or "")).lower()
    # adb root restarts adbd; wait for it to come back before asking anything.
    adb_mod.run_adb(target.tool, ["wait-for-device"], serial=target.target_id,
                    timeout=60, check=False)
    completed = adb_mod.run_adb(target.tool, ["shell", "id", "-u"],
                                serial=target.target_id, timeout=15, check=False)
    uid = (completed.stdout or "").strip() if isinstance(completed.stdout, str) else ""
    return uid == "0", root_out


def _system_ca_script(digest: str, source_store: str, *, use_apex: bool) -> str:
    """The exact reversible install sequence, one shell program.

    Copy the existing store aside, mount a tmpfs over `/system/etc/security/
    cacerts`, restore the copy plus our cert, relabel, and (API>=34) bind that
    tmpfs into every zygote mount namespace so already-forked apps pick it up.
    """
    steps = [
        f"mkdir -p -m 700 {CA_STAGING}",
        f"cp {source_store}/* {CA_STAGING}/",
        f"mount -t tmpfs tmpfs {SYSTEM_CA_STORE}",
        f"mv {CA_STAGING}/* {SYSTEM_CA_STORE}/",
        f"cp /data/local/tmp/{digest}.0 {SYSTEM_CA_STORE}/{digest}.0",
        f"chown root:root {SYSTEM_CA_STORE}/*",
        f"chmod 644 {SYSTEM_CA_STORE}/*",
        f"chcon u:object_r:system_file:s0 {SYSTEM_CA_STORE}/*",
    ]
    if use_apex:
        steps.append(
            "for Z in $(pidof zygote) $(pidof zygote64); do "
            f"nsenter --mount=/proc/$Z/ns/mnt -- mount --bind "
            f"{SYSTEM_CA_STORE} {APEX_CA_STORE}; done"
        )
    return "\n".join(steps)


def install_system_ca(target: Target, cert: Path) -> dict[str, Any]:
    """Add the MITM CA to the SYSTEM trust store of a rooted emulator.

    Reversible (tmpfs + namespace binds, cleared by a reboot), API-aware, and
    only on a rootable emulator. Refuses cleanly on a non-rootable / Play image
    with `unsupported_capability` (capability ``network.system_ca``) and the
    exact reason, rather than pushing to a store that would not take. Returns
    ``{installed, hash, api, method, reversible}``. Consent is the caller's job
    (see ``attach_transparent``); this is the mechanism only.
    """
    if not is_emulator(target):
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "system-CA install is emulator-only",
            "A physical device needs the certificate trusted through its own "
            "Settings UI, or a debug network_security_config; this path uses root, "
            "which physical devices refuse.",
            capability=SYSTEM_CA_CAPABILITY,
            reason="physical_device",
        )
    is_root, root_out = _confirm_root(target)
    if not is_root:
        refused = any(marker in root_out for marker in
                      ("cannot run as root", "not permitted", "production"))
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "the emulator is not rootable, so the MITM CA cannot be added to the "
            "system trust store",
            "Boot a rootable 'google_apis' image (not 'google_apis_playstore' and not "
            "a production build); a Play image blocks 'adb root'. On a non-rootable "
            "device, use 'network attach --install-ca' with a debug "
            "network_security_config instead (Flutter/pinned traffic is not captured "
            "that way).",
            capability=SYSTEM_CA_CAPABILITY,
            reason="adb_root_refused" if refused else "adb_root_unavailable",
        )

    api = device_api_level(target)
    use_apex = api is not None and api >= APEX_MIN_API
    source_store = APEX_CA_STORE if use_apex else SYSTEM_CA_STORE
    method = "apex_conscrypt" if use_apex else "system_cacerts"

    digest = _subject_hash(cert)
    staging = f"/data/local/tmp/{digest}.0"
    adb_mod.run_adb(target.tool, ["push", str(cert), staging],
                    serial=target.target_id, timeout=30, check=True)
    adb_mod.run_adb(
        target.tool, ["shell", _system_ca_script(digest, source_store, use_apex=use_apex)],
        serial=target.target_id, timeout=60, check=False,
    )
    return {
        "installed": f"{SYSTEM_CA_STORE}/{digest}.0",
        "hash": digest,
        "api": api,
        "method": method,
        "reversible": True,
    }


def system_ca_installed(target: Target, cert: Path) -> dict[str, Any]:
    """Verify the CA is present *from an app's point of view*.

    A fresh `adb shell` does not see the bind (a different mount namespace), so
    on API>=34 the check enters a zygote's mount namespace with `nsenter`; on
    older images the tmpfs over `/system/etc/security/cacerts` is what apps read
    directly. Returns ``{present, hash, api, method, checked_via}``.
    """
    api = device_api_level(target)
    use_apex = api is not None and api >= APEX_MIN_API
    digest = _subject_hash(cert)
    if use_apex:
        checked_via = "zygote_namespace"
        script = (
            "Z=$(pidof zygote64); [ -z \"$Z\" ] && Z=$(pidof zygote); "
            "set -- $Z; Z=$1; "
            f"nsenter --mount=/proc/$Z/ns/mnt -- ls {APEX_CA_STORE}/{digest}.0"
        )
    else:
        checked_via = "system_store"
        script = f"ls {SYSTEM_CA_STORE}/{digest}.0"
    completed = adb_mod.run_adb(target.tool, ["shell", script],
                                serial=target.target_id, timeout=20, check=False)
    present = getattr(completed, "returncode", 1) == 0
    return {
        "present": present,
        "hash": digest,
        "api": api,
        "method": "apex_conscrypt" if use_apex else "system_cacerts",
        "checked_via": checked_via,
    }


def attach_transparent(
    target: Target, record: dict[str, Any], *, port: int, acknowledged: bool
) -> dict[str, Any]:
    """Transparent MITM capture: no device proxy setting, no app change.

    Requires the emulator to have been LAUNCHED routed through this session's
    proxy (``devices boot --http-proxy``); `-http-proxy` cannot be injected into
    a running emulator, so if the routing is absent this refuses with a hint to
    reboot rather than half-attaching. On success it installs the system CA
    (consent-gated) and marks the session ``capture_mode: transparent``.
    """
    if not is_emulator(target):
        raise errors.AutonomError(
            errors.PHYSICAL_DEVICE_ATTACH_UNSUPPORTED,
            f"{target.target_id} is not an emulator",
            "Transparent capture routes the emulator through the loopback proxy at "
            "launch time; a physical device cannot reach it. Use an emulator, or "
            "configure the device's Wi-Fi proxy and trust by hand.",
        )

    # An app-proxy attach wrote the device's global http_proxy and saved the
    # value to restore. Switching modes on top of it would overwrite that
    # record (transparent mode keeps no previous proxy) and the transparent
    # detach writes nothing, so the device would keep pointing at the proxy.
    # Refused before anything else runs; `network detach` first.
    current = record.get("network") or {}
    if current.get("attached") and current.get("capture_mode") != "transparent":
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "an app-proxy attach is active on this session; switching to transparent "
            "capture on top of it would lose the device's previous global HTTP proxy, "
            "which only 'network detach' restores",
            "Run 'autonom network detach' first (it restores the device's global "
            "http_proxy), then re-run 'autonom network attach --system-ca'.",
            capability=TRANSPARENT_CAPABILITY,
            reason="app_proxy_attached",
            device_proxy=current.get("device_proxy"),
        )

    from .. import emulator as emulator_mod

    expected = f"{proxy_mod.LISTEN_HOST}:{port}"
    routed = emulator_mod.proxy_routing(target.target_id)
    if routed != expected:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "the emulator was not booted routed to this session's proxy, so transparent "
            "capture is unavailable"
            + (f" (it is routed through {routed})" if routed else ""),
            "'-http-proxy' is a launch-time flag and cannot be applied to a running "
            "emulator. Reboot the AVD routed to the proxy: 'autonom devices shutdown', "
            f"then 'autonom devices boot --avd <name> --http-proxy {expected}', then "
            "re-run 'autonom network attach --system-ca'.",
            capability=TRANSPARENT_CAPABILITY,
            reason="emulator_not_proxy_routed",
            expected=expected,
            observed=routed,
        )

    certificate = proxy_mod.ca_certificate(record)
    if not certificate:
        raise errors.AutonomError(
            errors.PROXY_NOT_RUNNING,
            "no CA certificate has been generated yet",
            "Start the proxy first; mitmproxy writes its CA on first run.",
        )

    operation = consent.Operation(
        kind="ca_install",
        target=f"android:{target.target_id}",
        effect=(
            f"add the MITM CA certificate {certificate.name} to the SYSTEM trust store of "
            f"emulator {target.target_id} via root — a reversible tmpfs plus zygote "
            f"mount-namespace bind that a reboot clears — so the proxy it is already routed "
            f"through can decrypt its TLS traffic without any change to the app"
        ),
        flags=("--i-understand-mitm", "--system-ca"),
    )
    entry = consent.require(operation, acknowledged=acknowledged)

    ca = install_system_ca(target, certificate)
    # Prove the install from an app's point of view (the zygote mount namespace
    # on API>=34), and report the real result rather than assuming success. A
    # failed verify is honest, not fatal: HTTP is still captured and the change
    # is reversible, so the attach stands with a warning instead of unwinding.
    try:
        verify = system_ca_installed(target, certificate)
    except errors.AutonomError:
        verify = {"present": False, "checked_via": "unavailable"}
    ca["verified"] = bool(verify.get("present"))
    ca["checked_via"] = verify.get("checked_via")
    consent.record(record, entry)

    network = record.setdefault("network", {})
    network.update({
        "enabled": True,
        "proxy_host": proxy_mod.LISTEN_HOST,
        "proxy_port": port,
        "attached": True,
        "capture_mode": "transparent",
        "http_proxy_routed": expected,
        "system_ca": ca,
        # Transparent mode writes no device proxy setting, so detach has nothing
        # to restore.
        "previous_http_proxy": None,
    })
    result: dict[str, Any] = {
        "attached": True,
        "attach_state": "automated",
        "capture_mode": "transparent",
        "http_proxy_routed": expected,
        "system_ca": ca,
        "certificate": str(certificate),
    }
    if not ca["verified"]:
        result["warnings"] = [{
            "code": "system_ca_unverified",
            "error": "the system CA was installed but could not be verified from a "
                     "zygote mount namespace; HTTPS decryption may fail although the "
                     "emulator's HTTP traffic is still captured",
            "hint": "Confirm the image is rootable and re-run 'autonom network attach "
                    "--system-ca'; a reboot ('autonom devices shutdown') clears the "
                    "reversible mount if you want to start over.",
        }]
    return result


def _get_setting(target: Target) -> str | None:
    """`read_setting`: a failed adb read raises instead of being taken as the
    value. It used to return adb's error text, which `attach` then saved as
    the previous proxy and `detach` wrote back to the device."""
    return read_setting(target)


def _put_setting(target: Target, value: str) -> None:
    adb_mod.run_adb(
        target.tool, ["shell", "settings", "put", "global", SETTING, shlex.quote(value)],
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
    try:
        completed = adb_mod.run_adb(
            target.tool, ["shell", "svc", "wifi", "disable"],
            serial=target.target_id, timeout=20, check=False)
        if getattr(completed, "returncode", 0) not in (0, None):
            result["error"] = "svc wifi disable failed"
        else:
            time.sleep(2)
    finally:
        enabled = adb_mod.run_adb(
            target.tool, ["shell", "svc", "wifi", "enable"],
            serial=target.target_id, timeout=20, check=False)
    if getattr(enabled, "returncode", 0) not in (0, None):
        result["error"] = "svc wifi enable failed"
    if result.get("error"):
        return result
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

    current = _get_setting(target)
    network = record.setdefault("network", {})
    if network.get("attached"):
        if (network.get("capture_mode") == "transparent"
                or current != network.get("device_proxy")):
            raise errors.AutonomError(
                errors.BACKEND_FAILED, "The proxy changed outside this attachment.",
                "Run 'autonom network detach' to finish the existing attachment, "
                "then attach again.")
        previous = network.get("previous_http_proxy")
        saved_settings = network.get("previous_proxy_settings")
    else:
        if current == device_proxy:
            raise errors.AutonomError(
                errors.BACKEND_FAILED, "The device already uses this proxy without a restore snapshot.",
                "A stopped capture left it behind, so the original setting is unknown. "
                f"Set the proxy the device should use (no proxy: 'adb -s {target.target_id} "
                f"shell settings put global {SETTING} {UNSET}'), then attach again.")
        previous = current
        # Raw values, so detach puts back an empty row as empty, not deleted.
        saved_settings = {key: _read_proxy_setting(target, key, raw=True)
                          for key in PROXY_SETTINGS}
    _put_setting(target, device_proxy)
    network = record.setdefault("network", {})
    network.update({
        "enabled": True,
        "proxy_host": "127.0.0.1",
        "proxy_port": port,
        "device_proxy": device_proxy,
        "attached": True,
        "previous_http_proxy": previous,
        "previous_proxy_settings": saved_settings,
        # The device-proxy + user-CA path: honest fallback for non-rootable
        # devices, but Flutter/`dart:io` and pinned traffic are not captured.
        "capture_mode": "app_proxy",
    })
    consent.record(record, entry)
    applied = apply_proxy_setting(target) if network_cycle else {
        "method": "none", "applied": False,
        "hint": "The framework adopts a proxy written this way only after the "
                "network is re-evaluated; without that, nothing reaches the proxy.",
    }
    # `attach_state` mirrors the iOS attach: "automated" here means the device
    # setting was written by Autonom itself.
    result = {"attached": True, "attach_state": "automated", "device_proxy": device_proxy,
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

    if network.get("capture_mode") == "transparent":
        # Transparent capture never wrote a device proxy setting: the emulator
        # is routed at launch time and the system CA is a reversible tmpfs/bind.
        # There is nothing to restore; a shutdown clears both.
        network.update({"attached": False, "capture_mode": None})
        return {
            "was_attached": True,
            "capture_mode": "transparent",
            "system_ca_persists": True,
            "note": "no device proxy setting was written; the emulator stays routed "
                    "through the proxy until it is shut down, and the system CA is "
                    "cleared by a reboot ('autonom devices shutdown').",
        }

    previous = network.get("previous_http_proxy")
    restore = previous if previous else UNSET
    current = _get_setting(target)
    device_proxy = network.get("device_proxy")
    repair = (f"adb -s {target.target_id} shell settings put global {SETTING} {UNSET}")
    if current not in (device_proxy, restore, None if restore == UNSET else restore):
        if not _points_at(target, device_proxy):
            # Someone else replaced the proxy: the device no longer depends on
            # this capture, so finishing never strands it. Their setting stays.
            network.update({"attached": False, "device_proxy": None,
                            "previous_http_proxy": None, "previous_proxy_settings": None,
                            "capture_mode": None})
            return {"was_attached": True, "restored_http_proxy": None, "wrote": None,
                    "left_unchanged": current,
                    "warnings": [{
                        "code": "device_proxy_changed_externally",
                        "error": f"the device proxy was changed to {current!r} outside "
                                 "this attachment and was left as it is",
                        "hint": "Check that this is the proxy the device should use.",
                    }]}
        raise errors.AutonomError(
            errors.BACKEND_FAILED, "The device proxy changed outside this attachment.",
            "Inspect the current proxy; detach will not overwrite another proxy. "
            f"To clear it by hand: '{repair}', then retry.")
    if previous and previous == device_proxy:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, "The saved proxy points back to the proxy being stopped.",
            f"The original setting is unknown. Clear the device proxy with '{repair}' "
            "(or set the proxy the device should use), then retry; the capture proxy "
            "keeps running until then.")
    if isinstance(previous, str) and previous.startswith(EMULATOR_HOST + ":"):
        try:
            port = int(previous.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except (OSError, ValueError, OverflowError) as exc:
            raise errors.AutonomError(
                errors.BACKEND_FAILED, f"The saved local proxy {previous} is not reachable.",
                "Restoring it would leave the device offline. Start that proxy again, or "
                f"clear the device proxy with '{repair}', then retry; the capture proxy "
                "keeps running until then.") from exc
    saved = network.get("previous_proxy_settings")
    expected = _canonical_proxy(network.get("device_proxy"))
    raw_restore = saved if isinstance(saved, dict) else _canonical_proxy(restore)
    restored = {key: _normalise_setting(key, raw_restore.get(key)) for key in PROXY_SETTINGS}
    for key in PROXY_SETTINGS:
        current_value = _read_proxy_setting(target, key)
        if current_value not in (None, expected[key], restored.get(key)):
            raise errors.AutonomError(
                errors.BACKEND_FAILED, "The canonical device proxy changed outside this attachment.",
                "Inspect the current proxy; detach will not overwrite another proxy.")
    if restored.get("global_http_proxy_host") == EMULATOR_HOST:
        canonical = f"{EMULATOR_HOST}:{restored.get('global_http_proxy_port')}"
        if canonical == network.get("device_proxy"):
            raise errors.AutonomError(
                errors.BACKEND_FAILED, "The saved canonical proxy points back to this capture.",
                f"The original setting is unknown. Clear the device proxy with '{repair}', "
                "then retry; the capture proxy keeps running until then.")
        if canonical != previous:
            try:
                with socket.create_connection(("127.0.0.1", int(restored["global_http_proxy_port"])),
                                              timeout=1):
                    pass
            except (OSError, TypeError, ValueError, OverflowError) as exc:
                raise errors.AutonomError(
                    errors.BACKEND_FAILED, f"The saved canonical local proxy {canonical} is not reachable.",
                    "Restoring it would leave the device offline. Start that proxy again, or "
                    f"clear the device proxy with '{repair}', then retry; the capture proxy "
                    "keeps running until then.") from exc
    retry = ("Retry 'autonom network detach' or 'network stop'; the restore snapshot "
             "and the capture proxy have been kept.")
    try:
        _put_setting(target, restore)
        # Legacy sessions saved only host:port; restored then contains its
        # canonical route, after refusing any unrelated PAC/exclusion edit.
        for key in PROXY_SETTINGS:
            value = raw_restore.get(key)
            argv = (["shell", "settings", "delete", "global", key] if value is None else
                    ["shell", "settings", "put", "global", key, shlex.quote(str(value))])
            adb_mod.run_adb(target.tool, argv, serial=target.target_id, timeout=15, check=True)
        applied = apply_proxy_setting(target)
    except errors.AutonomError as exc:
        # A half-written restore is retried from the same snapshot.
        raise errors.AutonomError(
            exc.code, f"Restoring the device proxy failed: {exc.message}", retry,
            **exc.extra) from exc
    if (not applied.get("applied") or _get_setting(target) != restore
            or any(_read_proxy_setting(target, key) != restored.get(key)
                   for key in PROXY_SETTINGS)):
        raise errors.AutonomError(
            errors.BACKEND_FAILED, "The restored proxy could not be verified"
            + (f" ({applied['error']})" if applied.get("error") else "") + ".", retry)
    network.update({"attached": False, "device_proxy": None, "previous_http_proxy": None,
                    "previous_proxy_settings": None, "capture_mode": None})
    return {"was_attached": True, "restored_http_proxy": previous, "wrote": restore,
            "setting_applied": applied}


def _points_at(target: Target, device_proxy: str | None) -> bool:
    """Does the legacy or the canonical device proxy still name `device_proxy`?"""
    if not device_proxy:
        return False
    if _get_setting(target) == device_proxy:
        return True
    host = _read_proxy_setting(target, "global_http_proxy_host")
    port = _read_proxy_setting(target, "global_http_proxy_port")
    return f"{host}:{port}" == device_proxy


def _canonical_proxy(value: str | None) -> dict[str, str | None]:
    settings = dict.fromkeys(PROXY_SETTINGS)
    if value and value != UNSET:
        try:
            host, port = value.rsplit(":", 1)
            if not host or not 0 < int(port) < 65536:
                raise ValueError(value)
        except ValueError as exc:
            raise errors.AutonomError(
                errors.BACKEND_FAILED, "The saved proxy address is invalid.",
                "Inspect and explicitly repair the device proxy before stopping capture.") from exc
        settings["global_http_proxy_host"] = host.removeprefix("[").removesuffix("]")
        settings["global_http_proxy_port"] = port
    return settings


def _read_proxy_setting(target: Target, key: str, *, raw: bool = False) -> str | None:
    """One canonical proxy row. `raw` keeps an empty row ("") apart from a
    missing one (None); otherwise both, and port 0, read as None (no proxy)."""
    completed = adb_mod.run_adb(
        target.tool, ["shell", "settings", "get", "global", key],
        serial=target.target_id, timeout=15, check=True)
    value = (completed.stdout or "").strip()
    if value == "null":
        return None
    return value if raw else _normalise_setting(key, value)


def _normalise_setting(key: str, value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value)
    return None if (not value or key == "global_http_proxy_port" and value == "0") else value


def observed_setting(target: Target) -> str | None:
    try:
        return _get_setting(target)
    except errors.AutonomError:
        return None


def read_setting(target: Target) -> str | None:
    """The device's current global proxy, telling "unset" from "unreadable".

    Returns None when the setting is empty or ``null``. Raises
    `backend_failed` when adb itself failed — a device gone offline, a dead
    server. `observed_setting` folds both into None, which made `network
    status` report an unreachable device as "proxy cleared externally".
    `run_adb` merges stderr into stdout, so a failed read must be caught by
    its exit status, never taken as the setting's value.
    """
    completed = adb_mod.run_adb(
        target.tool, ["shell", "settings", "get", "global", SETTING],
        serial=target.target_id, timeout=15, check=False,
    )
    output = (completed.stdout or "").strip() if isinstance(completed.stdout, str) else ""
    if getattr(completed, "returncode", 0):
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"could not read {SETTING} on {target.target_id}: "
            f"{output[:200] or 'adb exited ' + str(completed.returncode)}",
            "Check the device with 'autonom devices'.",
        )
    if not output or output == "null":
        return None
    return output
