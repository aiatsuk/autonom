"""Stable local host identity (P3, contract section 1).

One host id per Autonom state base, stored once:

    <state base>/host.json    {"schema": 1, "host_id": "h_...", "created_at": "...Z"} (0600)

The state base is the one of the control lease and the process registry
(`$AUTONOM_HOME`, else `$XDG_STATE_HOME/autonom`, else
`~/.local/state/autonom`); it is read through `control._state_base`, which
only reads the environment.

The file is created with an exclusive create and is never overwritten: a
process that loses the creation race reads the winner's file, and a file that
does not hold a valid identity is refused with `host_identity_invalid` and
left byte for byte as it was. Nothing here touches a device.
"""
from __future__ import annotations

import base64
import json
import os
import platform as _stdlib_platform
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, errors
from .control import _state_base

HOST_FILE_NAME = "host.json"
SCHEMA = 1
HOST_ID_PATTERN = re.compile(r"h_[a-z2-7]{26}")
CAPABILITIES = ("sessions", "control.lease", "canvas", "flows", "artifacts")

# A creator that lost the O_EXCL race reads the winner's file; while the
# winner has not written it yet, it is empty (20 reads over about 1 s).
_READ_RETRIES = 20
_READ_RETRY_DELAY_S = 0.05


def state_base() -> Path:
    """The shared Autonom state base (nothing is created)."""
    return _state_base()


def host_file() -> Path:
    """`<state base>/host.json` (nothing is created)."""
    return state_base() / HOST_FILE_NAME


def _new_host_id() -> str:
    """`h_` + lowercase RFC 4648 base32 of 16 random bytes, no padding."""
    encoded = base64.b32encode(secrets.token_bytes(16)).decode("ascii")
    return "h_" + encoded.rstrip("=").lower()


def _invalid(path: Path, reason: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.HOST_IDENTITY_INVALID,
        f"host identity file {path} is invalid: {reason}",
        hint=f"Move {path} aside to mint a new host id; ids in old evidence then "
             "refer to the old one.",
        path=str(path),
    )


def _read_bytes(path: Path) -> bytes | None:
    """The file's bytes, `None` when it does not exist. An empty file (a
    concurrent creator has not written it yet) is read again up to 20 times
    over about 1 s; it stays empty only when its creator died."""
    data = b""
    for attempt in range(_READ_RETRIES):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _invalid(path, f"cannot be read ({exc.strerror or exc})") from exc
        if data:
            return data
        if attempt + 1 < _READ_RETRIES:
            time.sleep(_READ_RETRY_DELAY_S)
    return data


def _parse(path: Path, data: bytes) -> str:
    """The host id held by `data`, or a `host_identity_invalid` refusal."""
    if not data:
        raise _invalid(path, "the file is empty")
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _invalid(path, "not valid JSON") from exc
    if not isinstance(record, dict):
        raise _invalid(path, "not a JSON object")
    schema = record.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        raise _invalid(path, f"schema is {schema!r}, expected {SCHEMA}")
    value = record.get("host_id")
    if not isinstance(value, str) or not HOST_ID_PATTERN.fullmatch(value):
        raise _invalid(path, "host_id does not match ^h_[a-z2-7]{26}$")
    return value


def _ensure_base(base: Path) -> None:
    """Create the state base (0700) when it does not exist yet."""
    if base.is_dir():
        return
    try:
        base.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        return  # a concurrent creator made it, or it is not a directory (open fails)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _create(path: Path) -> str | None:
    """Create the file with a new id; `None` when another process won."""
    _ensure_base(path.parent)
    host_id = _new_host_id()
    record = {
        "schema": SCHEMA,
        "host_id": host_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)
    return host_id


def host_id() -> str:
    """This state base's host id, minted and stored on the first call."""
    path = host_file()
    data = _read_bytes(path)
    if data is None:
        created = _create(path)
        if created is not None:
            return created
        data = _read_bytes(path)
        if data is None:
            raise _invalid(path, "the file vanished while it was being created")
    return _parse(path, data)


def info() -> dict[str, Any]:
    """What `autonom host info` prints after `ok`."""
    return {
        "host_id": host_id(),
        "name": _stdlib_platform.node()[:80] or "localhost",
        "autonom_version": __version__,
        "platform": sys.platform,
        "capabilities": list(CAPABILITIES),
    }
