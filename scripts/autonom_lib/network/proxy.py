"""mitmdump process control (CAP-NET-001, INV-05).

The proxy is an external process, not a library: the local mitmproxy is usually a
self-contained binary bundle with its own interpreter, so it cannot be imported.
Autonom therefore owns its lifecycle by pid and treats the running process — not
the pid file — as the truth.

The listen host is hard-wired to `127.0.0.1`. There is no flag to widen it: a
LAN-reachable MITM proxy turns the operator's machine into an open proxy for the
duration, and the Android emulator does not need one (it reaches host loopback
via `10.0.2.2`).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import errors, processes as processes_mod, session as session_mod
from . import mocks as mocks_mod

LISTEN_HOST = "127.0.0.1"
ADDON = Path(__file__).resolve().parent / "mitm_addon.py"

# Files mitmproxy writes into its confdir that contain ONLY the certificate.
# Everything else there (`mitmproxy-ca.pem`, `mitmproxy-ca.p12`) carries the
# private key and must never be copied into session artifacts.
CERT_ONLY_FILES = ("mitmproxy-ca-cert.cer", "mitmproxy-ca-cert.pem")


def ca_store() -> Path:
    """Machine-level CA directory, deliberately outside session artifacts.

    Two problems are fixed by keeping it here rather than in the session:

    1. mitmproxy writes its CA **private key** into its confdir. A confdir inside
       `<artifacts_dir>` therefore put the key in an artifact directory a user may
       archive or attach to a bug report (CAP-ATTACH-003).
    2. A per-session confdir meant a **new CA per session**, so a certificate
       installed on a device with `--install-ca` was worthless the next time — the
       device would be trusting a CA that no longer signs anything.
    """
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        root = Path(explicit)
    else:
        state = os.environ.get("XDG_STATE_HOME")
        root = Path(state) / "autonom" if state else Path.home() / ".local/state/autonom"
    path = root / "ca"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def publish_certificate(record: dict[str, Any]) -> Path | None:
    """Copy the certificate — and only the certificate — into session artifacts."""
    source = ca_store()
    destination = network_dir(record) / "mitm-ca"
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    published = None
    for name in CERT_ONLY_FILES:
        candidate = source / name
        if candidate.exists():
            target = destination / name
            shutil.copyfile(candidate, target)
            os.chmod(target, 0o644)
            published = published or target
    return published


def find_mitmdump(explicit: str | None = None) -> str:
    candidate = explicit or os.environ.get("AUTONOM_MITMDUMP")
    if candidate:
        return candidate
    path = shutil.which("mitmdump")
    if not path:
        raise errors.tool_missing("mitmdump")
    return path


def network_dir(record: dict[str, Any]) -> Path:
    path = Path(record["artifacts_dir"]) / "network"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def assert_safe_permissions(record: dict[str, Any]) -> None:
    """Refuse to capture traffic into a directory other local users can read."""
    artifacts = Path(record["artifacts_dir"])
    mode = artifacts.stat().st_mode
    if mode & 0o002:
        raise errors.AutonomError(
            errors.UNSAFE_ARTIFACTS_PERMISSIONS,
            f"{artifacts} is world-writable; captured traffic would not be private",
            f"Run: chmod 700 {artifacts}",
        )


def proxy_file(record: dict[str, Any]) -> Path:
    return network_dir(record) / "proxy.json"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((LISTEN_HOST, port))
        except OSError:
            return False
    return True


def _pick_port(requested: int | None) -> int:
    if requested:
        if not _port_free(requested):
            raise errors.AutonomError(
                errors.PORT_UNAVAILABLE,
                f"port {requested} is already in use on {LISTEN_HOST}",
                "Pick another --port, or stop whatever is listening there.",
            )
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LISTEN_HOST, 0))
        return probe.getsockname()[1]


# Android decides a network has no internet by probing these over HTTPS, and
# system components trust only the *system* CA store — never a user-installed
# one. Intercepting them therefore fails the probe, the network loses its
# VALIDATED capability, and apps conclude they are offline and stop making
# requests. Observed exactly that: nine minutes after attach the app under test
# had gone completely silent while the proxy itself worked fine.
CONNECTIVITY_CHECK_HOSTS = (
    "connectivitycheck.gstatic.com",
    "connectivitycheck.android.com",
    "www.google.com",
    "play.googleapis.com",
    "clients3.google.com",
    "captive.apple.com",
    "www.appleiphonecell.com",
)


def connectivity_check_pattern() -> str:
    return "^(" + "|".join(host.replace(".", r"\.") for host in CONNECTIVITY_CHECK_HOSTS) + "):"


def build_argv(
    mitmdump: str,
    *,
    port: int,
    directory: Path,
    confdir: Path,
    capture_bodies: bool,
    mocks_file: Path | str | None = None,
    ignore_hosts: str | None = None,
    intercept_connectivity_checks: bool = False,
) -> list[str]:
    argv = [
        mitmdump,
        "--listen-host", LISTEN_HOST,
        "--listen-port", str(port),
        "--set", f"confdir={confdir}",
        "-s", str(ADDON),
        "--set", f"autonom_dir={directory}",
        "--set", f"autonom_capture_bodies={'true' if capture_bodies else 'false'}",
    ]
    if mocks_file:
        argv += ["--set", f"autonom_mocks={mocks_file}"]
    patterns = []
    if not intercept_connectivity_checks:
        patterns.append(connectivity_check_pattern())
    if ignore_hosts:
        patterns.append(ignore_hosts)
    for pattern in patterns:
        argv += ["--ignore-hosts", pattern]
    argv.append("-q")
    return argv


def status(record: dict[str, Any]) -> dict[str, Any]:
    path = proxy_file(record)
    if not path.exists():
        return {"running": False, "pid": None, "port": None, "reason": "not_started"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"running": False, "pid": None, "port": None, "reason": "unreadable_proxy_file"}
    pid = payload.get("pid")
    if not session_mod.pid_alive(pid):
        return {"running": False, "pid": pid, "port": payload.get("port"), "reason": "stale_pid"}
    return {
        "running": True,
        "pid": pid,
        "port": payload.get("port"),
        "proxy_host": LISTEN_HOST,
        "capture_bodies": payload.get("capture_bodies", False),
        "started_at": payload.get("started_at"),
    }


def start(
    record: dict[str, Any],
    *,
    port: int | None = None,
    capture_bodies: bool = False,
    mitmdump: str | None = None,
    ignore_hosts: str | None = None,
    intercept_connectivity_checks: bool = False,
) -> dict[str, Any]:
    assert_safe_permissions(record)
    current = status(record)
    if current["running"]:
        return current

    binary = find_mitmdump(mitmdump)
    directory = network_dir(record)
    confdir = ca_store()

    chosen = _pick_port(port)
    # Enforcement reads the live registry, so a rule added mid-run takes effect
    # without a restart; the snapshot beside it records what was in force when
    # this run started.
    mocks_file = mocks_mod.registry_file()
    mocks_mod.snapshot(directory / "mocks-snapshot.json")
    argv = build_argv(binary, port=chosen, directory=directory,
                      confdir=confdir, capture_bodies=capture_bodies,
                      mocks_file=mocks_file, ignore_hosts=ignore_hosts,
                      intercept_connectivity_checks=intercept_connectivity_checks)
    log = directory / "mitmdump.log"
    handle = open(log, "ab")
    process = subprocess.Popen(  # noqa: S603 - argv is constructed, never shell
        argv, stdout=handle, stderr=handle, start_new_session=True
    )

    deadline = time.time() + 15
    while time.time() < deadline:
        if not _port_free(chosen):
            break
        if process.poll() is not None:
            detail = log.read_text(encoding="utf-8", errors="replace")[-600:]
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                f"mitmdump exited immediately: {detail.strip()}",
                "Check the mitmproxy install with 'autonom doctor'.",
            )
        time.sleep(0.2)

    payload = {
        "pid": process.pid,
        "port": chosen,
        "proxy_host": LISTEN_HOST,
        "capture_bodies": capture_bodies,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "confdir": str(confdir),
    }
    target = proxy_file(record)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    publish_certificate(record)
    # Machine-level, so this proxy stays findable from any working directory —
    # the session file above is only reachable by someone already standing in
    # the right place.
    processes_mod.register("proxy", process.pid, artifacts_dir=str(directory),
                           port=chosen, session_id=record.get("session_id"))
    return {"running": True, **payload}


def stop(record: dict[str, Any]) -> dict[str, Any]:
    """Idempotent: stopping a proxy that is not running is success."""
    current = status(record)
    path = proxy_file(record)
    if not current["running"]:
        path.unlink(missing_ok=True)
        if current.get("pid"):
            processes_mod.deregister(current["pid"])
        return {"was_running": False}
    session_mod.terminate_pid(current["pid"])
    processes_mod.deregister(current["pid"])
    path.unlink(missing_ok=True)
    return {"was_running": True, "pid": current["pid"], "port": current["port"]}


def ca_certificate(record: dict[str, Any]) -> Path | None:
    """The CA **certificate** only; the private key stays in the machine store."""
    published = network_dir(record) / "mitm-ca"
    for name in CERT_ONLY_FILES:
        candidate = published / name
        if candidate.exists():
            return candidate
    for name in CERT_ONLY_FILES:
        candidate = ca_store() / name
        if candidate.exists():
            return candidate
    return None
