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
# Spawns tried for a picked (not explicit) port before `backend_failed`.
PROXY_START_ATTEMPTS = 3
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


def _already_running(current: dict[str, Any], *, port: int | None,
                     capture_bodies: bool) -> dict[str, Any]:
    """The running proxy, plus a warning when the request asked for another one.

    Starting is idempotent, but silently returning a proxy on a different port,
    or one that is (not) writing full bodies, would let the caller believe its
    flags took effect. The running proxy is kept — restarting it would drop
    in-flight traffic — and the mismatch is reported as requested vs actual.
    """
    result = {**current, "already_running": True}
    requested: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    if port and port != current.get("port"):
        requested["port"], actual["port"] = port, current.get("port")
    if bool(capture_bodies) != bool(current.get("capture_bodies")):
        requested["capture_bodies"] = bool(capture_bodies)
        actual["capture_bodies"] = bool(current.get("capture_bodies"))
    if requested:
        result["requested"] = requested
        result["warnings"] = [{
            "code": "proxy_already_running",
            "error": "the proxy is already running with different settings; "
                     "the running proxy was kept: "
                     + ", ".join(f"{key} requested {requested[key]!r}, actual {actual[key]!r}"
                                 for key in requested),
            "hint": "Run 'autonom network stop' and start again to apply the new "
                    "--port / --capture-bodies.",
        }]
    return result


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
        return _already_running(current, port=port, capture_bodies=capture_bodies)

    binary = find_mitmdump(mitmdump)
    directory = network_dir(record)
    confdir = ca_store()

    # Enforcement reads the live registry, so a rule added mid-run takes effect
    # without a restart; the snapshot beside it records what was in force when
    # this run started.
    mocks_file = mocks_mod.registry_file()
    mocks_mod.snapshot(directory / "mocks-snapshot.json")
    log = directory / "mitmdump.log"
    # A free port is picked by binding and releasing it, so another proxy
    # starting at the same moment (two Canvas devices) can take it before
    # mitmdump does: mitmdump then exits before binding, and a picked port is
    # tried again (up to PROXY_START_ATTEMPTS). An explicit port is tried once.
    attempts = 1 if port else PROXY_START_ATTEMPTS
    process = None
    chosen = 0
    detail = ""
    for _attempt in range(attempts):
        chosen = _pick_port(port)
        argv = build_argv(binary, port=chosen, directory=directory,
                          confdir=confdir, capture_bodies=capture_bodies,
                          mocks_file=mocks_file, ignore_hosts=ignore_hosts,
                          intercept_connectivity_checks=intercept_connectivity_checks)
        process, detail = _spawn_and_wait(argv, chosen, log)
        if process is not None:
            break
    if process is None:
        if port and not _port_free(chosen):
            raise errors.AutonomError(
                errors.PORT_UNAVAILABLE,
                f"port {chosen} is already in use on {LISTEN_HOST}",
                "Pick another --port, or stop whatever is listening there.",
            )
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"mitmdump exited immediately: {detail.strip()}",
            "Check the mitmproxy install with 'autonom doctor'.",
        )

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
    # The signature lets `cleanup` and `session stop` verify the pid is still
    # this mitmdump (its argv names the addon) before they kill it.
    processes_mod.register("proxy", process.pid, artifacts_dir=str(directory),
                           port=chosen, session_id=record.get("session_id"),
                           signature=processes_mod.ADDON_MARKER)
    return {"running": True, **payload}


def _spawn_and_wait(argv: list[str], port: int,
                    log: Path) -> tuple[subprocess.Popen | None, str]:
    """Start mitmdump and wait (up to 15 s) until it listens on `port`.

    "The port is no longer free" is not proof: another process (a second
    proxy started at the same moment) may have taken the picked port, and our
    mitmdump is then about to exit with "address in use". So the listener
    must belong to the spawned process group (`_listener_owned_by`; mitmdump
    runs in its own session, so its pid is the group id). Where no tool can
    say who listens, the child must still be running `OWNER_SETTLE_SECONDS`
    after the port was taken.

    Returns the process, or None and a reason when it exited first or the
    port is held by someone else (the child is then stopped and reaped;
    nothing is left behind)."""
    # The child gets its own copy of the descriptor; this process's copy was
    # never closed, leaking one open file per `network start`.
    with open(log, "ab") as handle:
        process = subprocess.Popen(  # noqa: S603 - argv is constructed, never shell
            argv, stdout=handle, stderr=handle, start_new_session=True
        )

    def tail() -> str:
        return log.read_text(encoding="utf-8", errors="replace")[-600:]

    deadline = time.time() + 15
    foreign_seen = 0
    unknown_since: float | None = None
    while time.time() < deadline:
        exited = process.poll() is not None
        if not _port_free(port):
            owner = _listener_owned_by(port, process.pid)
            if owner:
                return process, ""
            if owner is None:
                if exited:
                    return None, tail()
                unknown_since = unknown_since or time.time()
                if time.time() - unknown_since >= OWNER_SETTLE_SECONDS:
                    return process, ""
            else:
                # Seen twice, so a listener caught between bind and listen
                # is not mistaken for a stranger.
                foreign_seen += 1
                if exited or foreign_seen >= 2:
                    _discard(process)
                    return None, (f"port {port} was taken by another process "
                                  f"before mitmdump could listen on it. {tail()}")
        else:
            foreign_seen = 0
            if exited:
                return None, tail()
        time.sleep(0.2)
    if foreign_seen:
        _discard(process)
        return None, f"port {port} was taken by another process"
    return process, ""


# How long a spawned mitmdump must stay up after its port was taken, when
# neither lsof nor /proc can say which process listens there.
OWNER_SETTLE_SECONDS = 1.5
_LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof")


def _in_group(pid: int, pgid: int) -> bool:
    if pid == pgid:
        return True
    try:
        return os.getpgid(pid) == pgid
    except OSError:
        return False


def _listener_owned_by(port: int, pgid: int) -> bool | None:
    """Whether the TCP listener on `port` belongs to process group `pgid`.

    True or False when lsof (or Linux's /proc) could tell, None when nothing
    could. A listener lsof cannot see (another user's) counts as foreign."""
    lsof = shutil.which("lsof") or next(
        (path for path in _LSOF_CANDIDATES if os.access(path, os.X_OK)), None)
    if lsof:
        try:
            completed = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=10, check=False)
        except (OSError, subprocess.SubprocessError, ValueError):
            completed = None
        if completed is not None:
            pids = [int(item) for item in (completed.stdout or "").split() if item.isdigit()]
            if pids:
                return any(_in_group(pid, pgid) for pid in pids)
            # Exit 1 with nothing on stderr: no listener lsof may see.
            if completed.returncode == 1 and not (completed.stderr or "").strip():
                return False
    return _proc_listener_owned_by(port, pgid)


def _proc_listener_owned_by(port: int, pgid: int) -> bool | None:
    """The /proc answer (Linux without lsof): the listening sockets on `port`
    from /proc/net/tcp*, matched against the open files of the group."""
    inodes: set[str] = set()
    readable = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        readable = True
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":  # 0A: LISTEN
                continue
            try:
                if int(fields[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(fields[9])
            except (IndexError, ValueError):
                continue
    if not readable:
        return None
    if not inodes:
        return False
    wanted = {f"socket:[{inode}]" for inode in inodes}
    members = set(processes_mod.group_members(pgid)) | {pgid}
    for pid in members:
        try:
            names = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for name in names:
            try:
                if os.readlink(f"/proc/{pid}/fd/{name}") in wanted:
                    return True
            except OSError:
                continue
    return False


def _discard(process: subprocess.Popen) -> None:
    """Stop a spawned mitmdump that lost its port, and reap it."""
    if process.poll() is None:
        processes_mod.terminate_group(process.pid, timeout=5.0)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


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
