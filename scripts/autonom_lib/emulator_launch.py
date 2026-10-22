"""Interactive, single-run emulator jobs on macOS.

launchd separates QEMU from a background caller's CPU/I/O policy. Jobs are
not installed at login and never respawn. Linux and explicit direct launches
retain Popen. Private temporary plists are removed immediately after bootstrap.
"""
from __future__ import annotations

import contextlib
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import errors, processes

LAUNCHCTL = "/bin/launchctl"
_SERVICE = re.compile(r"gui/\d+/com\.autonom\.emulator\.[0-9a-f]{32}$")


def remove_service(service: str) -> None:
    # Registry metadata must never be able to remove an unrelated launch job.
    if not _SERVICE.fullmatch(service) or not service.startswith(f"gui/{os.getuid()}/"):
        return
    subprocess.run([LAUNCHCTL, "bootout", service], capture_output=True,
                   timeout=10, check=False)


class LaunchdChild:
    def __init__(self, pid: int, service: str):
        self.pid = pid
        self.service = service
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        result = subprocess.run([LAUNCHCTL, "print", self.service],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0 and re.search(
                rf"^\s*pid = {self.pid}\s*$", result.stdout, re.MULTILINE):
            return None
        match = re.search(r"^\s*last exit code = (-?\d+)", result.stdout, re.MULTILINE)
        self.returncode = int(match[1]) if match else -1
        self.close()
        return self.returncode

    def close(self) -> None:
        remove_service(self.service)

    def wait(self, timeout: float = 5) -> int:
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.service, timeout)
            time.sleep(0.05)
        return self.returncode


def start(argv: list[str]) -> subprocess.Popen | LaunchdChild:
    mode = os.environ.get("AUTONOM_EMULATOR_LAUNCH_MODE", "auto")
    if mode not in ("auto", "direct"):
        raise errors.AutonomError(errors.INVALID_VALUE,
                                  "AUTONOM_EMULATOR_LAUNCH_MODE must be auto or direct")
    if sys.platform != "darwin" or mode == "direct":
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    label = "com.autonom.emulator." + uuid.uuid4().hex
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    # Same environment/cwd as Popen, including alternate SDK/AVD homes.
    # Never log this document: callers may have credentials in their environment.
    job = {
        "Label": label,
        "ProgramArguments": [str(Path(argv[0]).resolve()), *argv[1:],
                             "-no-window", "-no-snapshot", "-gpu", "host"],
        "ProcessType": "Interactive",
        "RunAtLoad": False,
        "KeepAlive": False,
        "LaunchOnlyOnce": True,
        "EnvironmentVariables": dict(os.environ),
        "WorkingDirectory": str(Path.cwd()),
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    fd, name = tempfile.mkstemp(suffix=".plist", dir=processes.registry_dir())
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(job, handle)
        result = subprocess.run([LAUNCHCTL, "bootstrap", domain, name],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode:
            raise RuntimeError("bootstrap failed")
        # -p returns the PID even if a short-lived child has already exited.
        result = subprocess.run([LAUNCHCTL, "kickstart", "-p", service],
                                capture_output=True, text=True, timeout=10, check=False)
        if (result.returncode or not result.stdout.strip().isdigit()
                or int(result.stdout.strip()) <= 0):
            raise RuntimeError("kickstart failed")
        return LaunchdChild(int(result.stdout.strip()), service)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            remove_service(service)
        # Do not include launchctl output: it can echo argv/environment secrets.
        raise errors.AutonomError(
            errors.BACKEND_FAILED, "Could not launch the emulator as an interactive macOS job",
            "Run from a logged-in macOS desktop session. For an explicit unmanaged launch, "
            "set AUTONOM_EMULATOR_LAUNCH_MODE=direct (background throttling may return).",
        ) from exc
    finally:
        Path(name).unlink(missing_ok=True)
