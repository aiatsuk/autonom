"""Android emulator (AVD) lifecycle: discover the binary, list, boot, kill.

The `emulator` binary ships with the Android SDK but is rarely on PATH.
Discovery mirrors how the SDK is actually installed: an explicit override
first, then the SDK the local adb belongs to, then the well-known SDK roots,
then PATH. A missing binary is an expected condition (`emulator_not_found`),
not a crash — `devices` simply omits the AVD list then.

Shutdown refuses anything that is not an `emulator-<port>` serial: `adb emu
kill` on hardware is impossible, and powering off a person's phone would be
the wrong kind of surprise anyway.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import adb as adb_mod
from . import errors
from . import processes

try:  # POSIX (macOS, Linux: every platform Autonom runs on); absent on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

AVD_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
EMULATOR_SERIAL = re.compile(r"^emulator-\d+$")
# The even console ports an emulator may take (adb port = console port + 1).
EMULATOR_PORTS = range(5554, 5683, 2)
# One boot at a time picks its serial: `<state base>/locks/emulator-boot.lock`.
BOOT_LOCK = "emulator-boot.lock"
BOOT_LOCK_POLL = 0.1


def find_emulator(explicit: str | None = None, *, adb_path: str | None = None) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("AUTONOM_EMULATOR")
    if env:
        candidates.append(env)
    if adb_path:
        sdk = Path(adb_path).resolve().parent.parent
        candidates.append(str(sdk / "emulator" / "emulator"))
    for root_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(root_var)
        if root:
            candidates.append(str(Path(root) / "emulator" / "emulator"))
    candidates.append(str(Path.home() / "Library/Android/sdk/emulator/emulator"))
    on_path = shutil.which("emulator")
    if on_path:
        candidates.append(on_path)
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise errors.AutonomError(
        errors.EMULATOR_NOT_FOUND,
        "Android `emulator` binary not found",
        "Install the SDK 'emulator' package (Android Studio does), or set AUTONOM_EMULATOR.",
    )


def list_avds(emulator_bin: str) -> list[str]:
    completed = subprocess.run(
        [emulator_bin, "-list-avds"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    names: list[str] = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        # The emulator interleaves INFO chatter with names; AVD names cannot
        # contain spaces, so anything else is noise.
        if line and AVD_NAME.match(line):
            names.append(line)
    return names


# --- AVD hardware profiles --------------------------------------------------
#
# `-list-avds` gives names only. Which of them is the phone with the 1080x2400
# screen, or the tablet, lives in each AVD's `config.ini` — and an agent asked
# to "test on a phone-sized emulator" has to read it, or guess from the name.

_INI_LINE = re.compile(r"^\s*([^#=\s][^=]*?)\s*=\s*(.*?)\s*$")


def avd_home() -> Path:
    """Where AVDs live: `$ANDROID_AVD_HOME`, else `~/.android/avd`."""
    override = os.environ.get("ANDROID_AVD_HOME")
    return Path(override).expanduser() if override else Path.home() / ".android" / "avd"


def _read_ini(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _INI_LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def describe_avd(name: str) -> dict[str, Any]:
    """The hardware profile behind one AVD, from its ini files.

    `<home>/<name>.ini` holds the API target and the path of the `.avd`
    directory (AVDs can live outside the home); `<dir>/config.ini` holds the
    `hw.*` profile. Anything unreadable is reported as null, never guessed.
    """
    home = avd_home()
    pointer = _read_ini(home / f"{name}.ini")
    directory = Path(pointer["path"]).expanduser() if pointer.get("path") else home / f"{name}.avd"
    config = _read_ini(directory / "config.ini")
    width = _as_int(config.get("hw.lcd.width"))
    height = _as_int(config.get("hw.lcd.height"))
    api = None
    target = pointer.get("target") or config.get("image.sysdir.1") or ""
    match = re.search(r"android-(\d+)", target)
    if match:
        api = int(match.group(1))
    return {
        "name": name,
        "device": config.get("hw.device.name") or None,
        "screen": (
            {"width": width, "height": height,
             "density": _as_int(config.get("hw.lcd.density"))}
            if width and height else None
        ),
        "api": api,
        "abi": config.get("abi.type") or None,
        "path": str(directory) if config else None,
    }


def describe_avds(names: list[str]) -> list[dict[str, Any]]:
    return [describe_avd(name) for name in names]


def running_avd_name(adb_path: str, serial: str) -> str | None:
    """The AVD a running emulator was booted from, via its console.

    `adb emu avd name` answers only on `emulator-<port>` serials; hardware is
    skipped without an adb call. Best-effort: None when the console is
    unreachable, so the inventory never fails on it.
    """
    if not EMULATOR_SERIAL.match(serial):
        return None
    try:
        completed = adb_mod.run_adb(
            adb_path, ["emu", "avd", "name"], serial=serial, timeout=10, check=False,
        )
    except errors.AutonomError:
        return None
    output = completed.stdout if isinstance(completed.stdout, str) else ""
    for line in output.splitlines():
        line = line.strip()
        if line and line not in ("OK", "KO") and not line.startswith("KO:") and AVD_NAME.match(line):
            return line
    return None


def _proxy_hostport(value: str) -> str:
    """`HOST:PORT` from either a bare `HOST:PORT` or a full `http(s)://` URL.

    The emulator's launch flag wants a URL, while the registry and the
    transparent-capture check compare against the loopback `HOST:PORT` the
    proxy binds to, so both forms have to reduce to the same string.
    """
    text = value.strip()
    for scheme in ("http://", "https://"):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    return text.rstrip("/")


def _emulator_proxy_url(value: str) -> str:
    return f"http://{_proxy_hostport(value)}"


def proxy_routing(serial: str) -> str | None:
    """The `HOST:PORT` an emulator on `serial` was booted routed through.

    `-http-proxy` is a launch-time flag — it cannot be applied to a running
    emulator — so `devices boot --http-proxy` records it on the harness-owned
    process entry, and this reads it back by serial. None when the emulator was
    not booted routed (or was not booted by Autonom), which is exactly when a
    transparent `network attach --system-ca` must refuse.
    """
    for entry in processes.entries():
        if entry.get("kind") == "emulator" and entry.get("serial") == serial:
            routed = entry.get("http_proxy")
            if routed:
                return str(routed)
    return None


def boot_lock_path() -> Path:
    """`<state base>/locks/emulator-boot.lock`, its directory private (0700).
    The state base is the process registry's (`processes.registry_dir()`)."""
    directory = processes.registry_dir().parent / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    return directory / BOOT_LOCK


def _acquire_boot_lock(timeout: float) -> Callable[[], None]:
    """Take the machine's boot lock within `timeout` seconds; returns the
    release function (idempotent). Timing out is `boot_timeout` with
    `waiting_for: "boot_lock"`: another boot is still picking its serial."""
    if fcntl is None:  # pragma: no cover - Windows
        return lambda: None
    path = boot_lock_path()
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise errors.AutonomError(
                    errors.BOOT_TIMEOUT,
                    f"another emulator boot held the boot lock for more than {timeout:g}s",
                    "Another 'devices boot' is still choosing its serial; wait for it, "
                    "or re-run with a larger --timeout.",
                    waiting_for="boot_lock",
                ) from None
            time.sleep(BOOT_LOCK_POLL)
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            os.close(descriptor)  # releases the lock

    return release


@contextlib.contextmanager
def boot_lock(timeout: float) -> Iterator[None]:
    """Hold the machine's emulator boot lock (flock) for the block.

    Raises `boot_timeout` (extra `waiting_for: "boot_lock"`) when it cannot be
    taken within `timeout` seconds."""
    release = _acquire_boot_lock(timeout)
    try:
        yield
    finally:
        release()


def running_avds(adb_path: str) -> dict[str, str]:
    """AVD name -> serial of every running emulator (`emulator-*` in state
    `device`) whose console names its AVD. Best-effort per emulator."""
    found: dict[str, str] = {}
    for device in adb_mod.list_devices(adb_path):
        if device.state != "device" or not EMULATOR_SERIAL.match(device.serial):
            continue
        name = running_avd_name(adb_path, device.serial)
        if name and name not in found:
            found[name] = device.serial
    return found


def _port_of(serial: str | None) -> int | None:
    if serial and EMULATOR_SERIAL.match(serial):
        return int(serial.rsplit("-", 1)[1])
    return None


def _loopback_port_free(port: int) -> bool:
    """Whether an emulator could take `port` on loopback: nothing listens
    there (a connect is refused) and it can be bound. SO_REUSEADDR as the
    emulator binds: a port left in TIME_WAIT by an emulator that just
    stopped is free, a port something listens on is not."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", port))
        except OSError:
            pass
        else:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _launching_on(port: int) -> int | None:
    """The pid of an emulator this harness launched on `port` that is still
    running, though adb may not list it yet (a `--port` boot releases the
    boot lock right after its launch): its registry row names
    `emulator-<port>`. A row whose pid is gone or now runs something else
    does not count."""
    wanted = f"emulator-{port}"
    for entry in processes.entries():
        if entry.get("kind") != "emulator" or entry.get("serial") != wanted:
            continue
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            pass  # exists, owned by someone else
        command = processes.command_of(pid)
        if command is not None and "-avd" not in command:
            continue
        return pid
    return None


def _discard_child(child: subprocess.Popen) -> None:
    """Stop and reap an emulator this boot launched that cannot be its own."""
    if child.poll() is None:
        processes.terminate_group(child.pid, timeout=5.0)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        child.wait(timeout=5)
    processes.deregister(child.pid)


def _check_port(port: int | None) -> None:
    if port is None:
        return
    if isinstance(port, bool) or not isinstance(port, int) or port not in EMULATOR_PORTS:
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            f"--port {port!r} is not an emulator console port",
            f"Pick an even port from {EMULATOR_PORTS.start} to {EMULATOR_PORTS.stop - 2}; "
            "the emulator's serial becomes emulator-<port>.",
            port=port,
        )


def _port_unavailable(port: int, reason: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.PORT_UNAVAILABLE,
        f"emulator port {port} is not available: {reason}",
        "Pick another even --port (5554-5682), or leave --port out to let the "
        "emulator choose.",
        port=port, serial=f"emulator-{port}",
    )


def boot_avd(
    emulator_bin: str,
    adb_path: str,
    name: str,
    *,
    wait: bool = True,
    timeout: float = 180.0,
    http_proxy: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    """Start AVD `name` and (with `wait`) return once it has booted.

    Safe to run several at once: under the machine's boot lock an AVD that is
    already running is returned as it is (`already_running`), and a new
    emulator's serial is claimed only by this boot — `emulator-<port>` with
    `port`, otherwise a new serial whose console names this AVD. The lock is
    held until the serial is settled (right after the launch with `port`)."""
    _check_port(port)
    known = list_avds(emulator_bin)
    if name not in known:
        raise errors.AutonomError(
            errors.AVD_NOT_FOUND,
            f"AVD '{name}' does not exist",
            f"Available: {', '.join(known) if known else 'none — create one in Android Studio'}.",
        )
    started = time.monotonic()
    deadline = started + timeout
    release = _acquire_boot_lock(timeout)
    try:
        return _boot_locked(emulator_bin, adb_path, name, wait=wait, timeout=timeout,
                            http_proxy=http_proxy, port=port, release=release,
                            started=started, deadline=deadline)
    finally:
        release()


def _boot_locked(emulator_bin: str, adb_path: str, name: str, *, wait: bool,
                 timeout: float, http_proxy: str | None, port: int | None,
                 release: Callable[[], None], started: float,
                 deadline: float) -> dict[str, Any]:
    running = running_avds(adb_path)
    if name in running:
        serial = running[name]
        return {"avd": name, "pid": None, "booted": False, "waited": False,
                "already_running": True, "serial": serial, "target_id": serial,
                "port": _port_of(serial)}
    listed = adb_mod.list_devices(adb_path)
    before = {device.serial for device in listed}
    if port is not None:
        wanted = f"emulator-{port}"
        if wanted in before:
            raise _port_unavailable(port, f"{wanted} is already running another AVD")
        launching = _launching_on(port)
        if launching is not None:
            raise _port_unavailable(
                port, f"emulator pid {launching} was launched on it and is still starting")
        for candidate in (port, port + 1):
            if not _loopback_port_free(candidate):
                raise _port_unavailable(port, f"127.0.0.1:{candidate} is in use")
    argv = [emulator_bin, "-avd", name]
    if port is not None:
        argv += ["-port", str(port)]
    routed_hostport = None
    if http_proxy:
        # A launch-time flag that routes ALL of the emulator's TCP through the
        # host proxy at the network level — no device proxy setting, no app
        # change. It cannot be injected into a running emulator.
        routed_hostport = _proxy_hostport(http_proxy)
        argv += ["-http-proxy", _emulator_proxy_url(http_proxy)]
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Owned by the harness, not by a session: `devices boot` has no session
    # directory to point at, and an entry without one used to be classified
    # an orphan — so `cleanup` would have killed an emulator a session was
    # using. `devices shutdown` removes the entry. The proxy the emulator was
    # routed through is recorded here so `network attach --system-ca` can prove
    # the transparent path is available before touching the device.
    processes.register("emulator", child.pid, avd=name, owner=processes.HARNESS_OWNER,
                       http_proxy=routed_hostport,
                       serial=f"emulator-{port}" if port is not None else None)
    if port is not None:
        # The serial is fixed by the port: nothing is left to pick.
        release()
    detail: dict[str, Any] = {"avd": name, "pid": child.pid}
    if routed_hostport:
        detail["http_proxy"] = routed_hostport
    if not wait:
        return {**detail, "booted": False, "waited": False, "already_running": False,
                "port": port}

    claimed: str | None = f"emulator-{port}" if port is not None else None
    confirmed = False
    while time.monotonic() < deadline:
        ready = {device.serial for device in adb_mod.list_devices(adb_path)
                 if device.state == "device"}
        serial = None
        if claimed is not None:
            serial = claimed if claimed in ready else None
            if serial and not confirmed:
                # `emulator-<port>` is this boot's only when its console names
                # this AVD: an emulator that took the port first (and made this
                # one fail to bind) must never be reported as this boot's.
                running = running_avd_name(adb_path, serial)
                if running is None:
                    serial = None  # the console does not answer yet
                elif running != name:
                    _discard_child(child)
                    raise _port_unavailable(
                        port or 0, f"{serial} runs AVD '{running}', not '{name}'")
                else:
                    confirmed = True
        else:
            # A new serial is this boot's only when its console names this
            # AVD: a parallel boot's emulator may appear first. A console
            # that does not answer yet is asked again on the next round.
            for candidate in sorted(ready - before):
                if running_avd_name(adb_path, candidate) == name:
                    claimed = serial = candidate
                    processes.update(child.pid, serial=serial)
                    release()
                    break
        if serial:
            completed = adb_mod.run_adb(
                adb_path, ["shell", "getprop", "sys.boot_completed"], serial=serial, check=False
            )
            if (completed.stdout or "").strip() == "1":
                processes.update(child.pid, serial=serial)
                # A pin recorded for an earlier emulator on this port must
                # never be replayed onto the one just booted.
                dropped = _forget_device_state(serial)
                return {**detail, "booted": True, "waited": True,
                        "serial": serial, "target_id": serial,
                        "stale_pin_dropped": dropped, "already_running": False,
                        "port": _port_of(serial)}
        if child.poll() is not None and serial is None and time.monotonic() - started > 3:
            raise errors.AutonomError(
                errors.BACKEND_FAILED,
                f"emulator exited with code {child.returncode} before a device appeared",
                f"Run '{emulator_bin} -avd {name}' by hand to see its output.",
            )
        time.sleep(1.0)
    raise errors.AutonomError(
        errors.BOOT_TIMEOUT,
        f"AVD '{name}' did not reach boot_completed within {int(timeout)}s",
        "It may still be booting — check 'autonom devices', or re-run with a larger --timeout.",
    )


def kill_emulator(adb_path: str, serial: str, *, timeout: float = 15.0) -> dict[str, Any]:
    if not EMULATOR_SERIAL.match(serial):
        raise errors.AutonomError(
            errors.EMULATOR_ONLY,
            f"'{serial}' is not an emulator; refusing to power off hardware",
            "Only emulator-<port> targets can be shut down; unplug physical devices by hand.",
        )
    # Asked before the kill: once the console is gone the AVD name is too, and
    # an entry booted with --no-wait carries only the AVD name.
    avd = running_avd_name(adb_path, serial)
    # Also before the kill: `emu kill` saves a quickboot snapshot, so a pin
    # left on the device would come back on its next boot with no record
    # left to undo it. A refused or failed restore never stops the shutdown.
    restore = _restore_pins(adb_path, serial)
    adb_mod.run_adb(adb_path, ["emu", "kill"], serial=serial, check=False)
    deadline = time.monotonic() + timeout
    gone = False
    while time.monotonic() < deadline:
        if all(device.serial != serial for device in adb_mod.list_devices(adb_path)):
            gone = True
            break
        time.sleep(0.5)
    released = forget_emulator(serial, avd) if gone else []
    # The serial's status-bar/animation record describes this emulator; the
    # next one to take the port must not inherit it (re-asserted before every
    # capture, restored by clear/reset). Dropped even when the kill has not
    # finished yet: the emulator is on its way out either way.
    dropped = _forget_device_state(serial) or bool(restore.get("record_found"))
    detail: dict[str, Any] = {"serial": serial, "target_id": serial, "stopped": True,
                              "gone": gone, "registry_released": released,
                              "pin_record_dropped": dropped,
                              "pins_restored": restore["pins_restored"]}
    if restore["warnings"]:
        detail["warnings"] = restore["warnings"]
    return detail


def _restore_pins(adb_path: str, serial: str) -> dict[str, Any]:
    # Lazy for the same reason as `_forget_device_state`.
    from . import simulator
    from .platform import ANDROID, Target
    target = Target(ANDROID, serial, adb_path, {"serial": serial})
    try:
        return simulator.restore_before_shutdown(target)
    except (errors.AutonomError, OSError, ValueError) as exc:
        return {"pins_restored": [], "warnings": [{
            "code": "pin_restore_failed",
            "error": f"the recorded pins could not be restored before shutdown: {exc}",
            "hint": "Boot the emulator and run 'autonom simulator status-bar clear' "
                    "and 'autonom simulator animations reset'.",
        }]}


def _forget_device_state(serial: str) -> bool:
    # Imported lazily: the lifecycle module stays independent of the
    # simulator-control layer at import time.
    from . import simulator
    return simulator.forget_device(serial)


def forget_emulator(serial: str, avd: str | None = None) -> list[int]:
    """Drop the registry entries of an emulator that has been shut down.

    Matched by serial (recorded once boot completed) or, for an entry that
    never learned its serial, by AVD name. Without this the dead pid lingered
    and every later `doctor` warned about stale process entries.
    """
    released = []
    for entry in processes.entries():
        if entry.get("kind") != "emulator" or not entry.get("pid"):
            continue
        if entry.get("serial") == serial or (
                avd and not entry.get("serial") and entry.get("avd") == avd):
            processes.deregister(int(entry["pid"]))
            released.append(int(entry["pid"]))
    return released
