"""Host-wide control lease per target (P2, contract section 2).

One lease file per target lives in the machine state store:

    <state base>/control/                                  directory 0700
    <state base>/control/.lock                             flock target of every write (0600)
    <state base>/control/<platform>__<safe target>.json    one lease file per target (0600)

The state base is `$AUTONOM_HOME`, else `$XDG_STATE_HOME/autonom`, else
`~/.local/state/autonom` (the same base as the process registry). The safe
target part is exactly `session._safe_target`, so Python and Node name the
same file for the same target.

A lease names a holder `{kind, id, label}`, a generation that only a grant
increments (it survives release and expiry and never decreases), and an
expiry the holder renews. Every write happens under one `flock` and replaces
the file atomically (fsynced temporary file, `os.replace`), so lock-free
readers (Node, `status`, admission) see the old or the new file, never a
partial one. Reads never create the directory or any file; only a grant does.

Admission (`admission`, contract 2.4) is the single rule every mutating route
applies at execution. With no lease file for a target, every caller without a
claim is admitted exactly as before the lease existed.

This module is the only writer of lease files; the Canvas reaches it through
the bridge ops `control-acquire`/`control-release`/`control-renew`.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import errors
from .session import _UNSAFE_TARGET_CHARS, _safe_target

try:  # pragma: no cover - fcntl exists on macOS and Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

SCHEMA = 1
KINDS = ("human", "agent")
DEFAULT_AGENT_TTL_S = 60
HUMAN_TTL_S = 30
MIN_TTL_S, MAX_TTL_S = 5, 3600
ENV_HOLDER = "AUTONOM_CONTROL_HOLDER"
ENV_GENERATION = "AUTONOM_CONTROL_GENERATION"
ENV_NOW_MS = "AUTONOM_CONTROL_NOW_MS"  # test seam, honoured only with AUTONOM_TEST_MODE=1
ENV_TEST_MODE = "AUTONOM_TEST_MODE"

CAPABILITY = "control.lease"
REASONS = ("acquired", "taken_over", "renewed", "released")
MODES = ("readonly", "lifecycle", "host", "mutating", "flow")
ADMITTED_MODES = ("mutating", "flow")

CONTROL_DIR_NAME = "control"
LOCK_NAME = ".lock"
TMP_PREFIX = ".lease-"
TMP_SUFFIX = ".tmp"
STALE_TMP_S = 60

# Identifier patterns are applied with `fullmatch` only: in Python `$` also
# matches before a trailing newline, so `match` with `^...$` would accept
# 'agent:x\n', which the Node side (no `m` flag) rejects.
HOLDER_ID_RE = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
OPERATION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
PLATFORMS = ("android", "ios")
MAX_LABEL = 80
# Generations stay exact in JavaScript (`Number.MAX_SAFE_INTEGER`), so Node
# compares the same values; larger numbers are malformed.
MAX_GENERATION = 2**53 - 1
_MAX_GENERATION_DIGITS = len(str(MAX_GENERATION))
_DIGITS = re.compile(r"[0-9]+")
_GENERATION_IN_RAW = re.compile(rb'"generation"\s*:\s*(\d+)')
_LEASE_NAME = re.compile(r"[A-Za-z0-9._-]{1,16}__[A-Za-z0-9._-]+\.json")


@dataclass(frozen=True)
class Claim:
    """A caller's statement 'I hold the lease (at this generation)'."""

    holder_id: str
    generation: int | None = None


# --- Clock --------------------------------------------------------------------

def _now_ms() -> int:
    """Wall-clock epoch milliseconds. `AUTONOM_CONTROL_NOW_MS` replaces the
    clock only when `AUTONOM_TEST_MODE` is exactly "1" (a test seam: tests
    pin expiry without sleeping); otherwise it is ignored."""
    if os.environ.get(ENV_TEST_MODE) == "1":
        raw = os.environ.get(ENV_NOW_MS, "").strip()
        if _DIGITS.fullmatch(raw) and len(raw) <= _MAX_GENERATION_DIGITS:
            return int(raw)
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    """UTC ISO 8601 with milliseconds and `Z`, from integer epoch ms."""
    seconds, millis = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{millis:03d}Z"


# --- Layout -------------------------------------------------------------------

def _state_base() -> Path:
    explicit = os.environ.get("AUTONOM_HOME")
    if explicit:
        return Path(explicit)
    state = os.environ.get("XDG_STATE_HOME")
    return Path(state) / "autonom" if state else Path.home() / ".local/state/autonom"


def _control_path() -> Path:
    """The lease directory; nothing is created."""
    return _state_base() / CONTROL_DIR_NAME


def control_dir() -> Path:
    """The lease directory, created with mode 0700 (writers only)."""
    path = _control_path()
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _platform_part(platform: str) -> str:
    return _UNSAFE_TARGET_CHARS.sub("_", platform or "android")[:16]


def _check_target(platform: Any, target_id: Any) -> None:
    """Only `android` and `ios` are platforms: any other string could alias
    another target's file name (`android_` + `x` and `android` + `_x`)."""
    if platform not in PLATFORMS:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "the control lease platform must be 'android' or 'ios'",
            "Pass the target's platform (android or ios).", option="platform")
    if not isinstance(target_id, str) or not target_id:
        raise errors.AutonomError(
            errors.INVALID_VALUE, "the control lease needs a target id",
            "Pass the adb serial or the Simulator UDID.", option="target_id")


def lease_path(platform: str, target_id: str) -> Path:
    """`<state base>/control/<platform>__<safe target>.json`; nothing is created."""
    _check_target(platform, target_id)
    return _control_path() / f"{_platform_part(platform)}__{_safe_target(target_id)}.json"


# --- Reading ------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_holder(holder: Any) -> bool:
    return (isinstance(holder, dict)
            and holder.get("kind") in KINDS
            and isinstance(holder.get("id"), str) and bool(holder.get("id"))
            and isinstance(holder.get("label"), str))


def _valid_lease(lease: Any) -> bool:
    """Schema 1 shape check: what admission and status rely on."""
    if not isinstance(lease, dict) or lease.get("schema") != SCHEMA:
        return False
    generation = lease.get("generation")
    if not _is_int(generation) or not 0 <= generation <= MAX_GENERATION:
        return False
    if not isinstance(lease.get("platform"), str) or not isinstance(lease.get("target_id"), str):
        return False
    holder = lease.get("holder")
    if holder is None:
        return True
    return _valid_holder(holder) and _is_int(lease.get("expires_at_ms"))


def _read_raw(path: Path) -> tuple[dict | None, bytes | None]:
    """(valid lease or None, raw bytes or None when the file is missing).

    Only damaged *content* (not UTF-8, not JSON, nested too deeply for the
    parser, not schema 1) reads as no lease. An I/O failure (permission denied, EIO, a directory in the file's place)
    raises `OSError`: such a file may hold a live lease, so callers fail
    closed instead of treating it like a missing or corrupt one."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - any decode failure (ValueError, RecursionError, ...)
        return None, raw
    return (value if _valid_lease(value) else None), raw


def _read_error(exc: OSError, path: Path) -> errors.AutonomError:
    return errors.AutonomError(
        errors.OUTPUT_NOT_WRITABLE,
        f"the control lease file could not be read: {exc.strerror or exc.__class__.__name__}",
        f"Check the permissions of {path}; a lease file that cannot be read is never treated "
        "as free.",
        path=str(path), capability=CAPABILITY)


def _belongs_to(lease: dict | None, platform: str, target_id: str) -> bool:
    return (lease is not None and lease.get("platform") == platform
            and lease.get("target_id") == target_id)


def _read_target(path: Path, platform: str, target_id: str
                 ) -> tuple[dict | None, bytes | None, bool]:
    """(the target's own lease or None, raw bytes, foreign). `foreign` is true
    when the file holds a valid lease of another target whose name maps to
    the same file; readers then see no lease for this target."""
    lease, raw = _read_raw(path)
    if lease is not None and not _belongs_to(lease, platform, target_id):
        return None, raw, True
    return lease, raw, False


def read_lease(platform: str, target_id: str) -> dict | None:
    """The target's lease, or None when it is missing, has damaged content,
    is not schema 1 or names another target (readers then behave as if no
    lease existed). A file that exists but cannot be read raises
    `output_not_writable` (fail closed)."""
    path = lease_path(platform, target_id)
    try:
        return _read_target(path, platform, target_id)[0]
    except OSError as exc:
        raise _read_error(exc, path) from exc


def lease_state(lease: dict | None, now_ms: int) -> str:
    """`none` (no lease, or never granted), `free` (released), `held` (live
    holder) or `expired` (holder present, `now_ms >= expires_at_ms`)."""
    if lease is None or not _valid_lease(lease):
        return "none"
    holder = lease.get("holder")
    if holder is None:
        return "none" if lease.get("generation") == 0 else "free"
    return "held" if now_ms < lease["expires_at_ms"] else "expired"


# --- Admission ----------------------------------------------------------------

_HINTS = {
    errors.CONTROL_HELD: (
        "Wait until 'autonom control status --target {target}' shows it free, or ask the holder "
        "to release it; a human can take over from the Canvas."),
    errors.CONTROL_GENERATION_STALE: (
        "Your lease was replaced (generation {generation} now); acquire it again before driving "
        "the device; never replay an input whose outcome you did not see."),
    errors.CONTROL_LEASE_EXPIRED: (
        "Renew before the expiry ('autonom control renew') or acquire it again."),
    errors.CONTROL_NOT_HELD: (
        "Acquire the lease first ('autonom control acquire --holder <id>') or drop the control "
        "claim (--control-holder / AUTONOM_CONTROL_HOLDER)."),
}


def _refusal(code: str, lease: dict | None, claim: Claim | None, *,
             platform: str | None, target_id: str | None) -> dict:
    lease = lease if lease is not None and _valid_lease(lease) else None
    platform = platform if platform is not None else (lease or {}).get("platform")
    target_id = target_id if target_id is not None else (lease or {}).get("target_id")
    generation = lease.get("generation") if lease else None
    holder = lease.get("holder") if lease else None
    target = target_id or "the target"
    claimed = claim.holder_id if claim else None
    if code == errors.CONTROL_HELD and holder:
        message = (f"{target} is controlled by {holder['kind']} '{holder['label']}' "
                   f"(generation {generation})")
    elif code == errors.CONTROL_GENERATION_STALE:
        message = (f"the control claim of '{claimed}' on {target} is stale: generation "
                   f"{claim.generation if claim else None} given, the lease is at generation "
                   f"{generation}")
    elif code == errors.CONTROL_LEASE_EXPIRED:
        message = (f"the control lease of '{claimed}' on {target} expired at "
                   f"{(lease or {}).get('expires_at')}")
    elif code == errors.CONTROL_HELD:
        message = f"{target} is controlled by another holder"
    elif claimed is not None:
        message = f"'{claimed}' does not hold the control lease of {target}"
    else:
        message = f"nobody holds the control lease of {target}"
    result: dict[str, Any] = {
        "code": code,
        "message": message,
        "hint": _HINTS[code].format(target=target, generation=generation),
        "platform": platform,
        "target_id": target_id,
        "capability": CAPABILITY,
        "generation": generation,
        "holder": dict(holder) if isinstance(holder, dict) else None,
        "expires_at": (lease or {}).get("expires_at") if holder else None,
    }
    if claim is not None:
        result["claimed_holder"] = claim.holder_id
        result["claimed_generation"] = claim.generation
    return result


def admission(lease: dict | None, claim: Claim | None, now_ms: int, *,
              platform: str | None = None, target_id: str | None = None) -> dict | None:
    """The admission rule (contract 2.4): None = admitted; else the refusal
    `{"code", "message", "hint", **extras}`. The first matching row decides.
    `platform`/`target_id` only fill the refusal extras (else the lease's)."""
    lease = lease if lease is not None and _valid_lease(lease) else None
    state = lease_state(lease, now_ms)

    def refuse(code: str) -> dict:
        return _refusal(code, lease, claim, platform=platform, target_id=target_id)

    if state == "none":
        return None if claim is None else refuse(errors.CONTROL_NOT_HELD)          # rows 1, 2
    if claim is None:
        return refuse(errors.CONTROL_HELD) if state == "held" else None            # rows 3, 4
    assert lease is not None
    holder = lease.get("holder") or {}
    same = holder.get("id") == claim.holder_id
    generation_matches = claim.generation is None or claim.generation == lease["generation"]
    if state == "held" and same:
        return None if generation_matches else refuse(errors.CONTROL_GENERATION_STALE)  # rows 5, 6
    if claim.generation is not None and claim.generation < lease["generation"]:
        return refuse(errors.CONTROL_GENERATION_STALE)                             # row 7
    if state == "held":
        return refuse(errors.CONTROL_HELD)                                         # row 8
    if state == "expired" and same and generation_matches:
        return refuse(errors.CONTROL_LEASE_EXPIRED)                                # row 9
    return refuse(errors.CONTROL_NOT_HELD)                                         # row 10


def _raise(refusal: dict, **more: Any) -> None:
    extras = {key: value for key, value in refusal.items()
              if key not in ("code", "message", "hint")}
    extras.update(more)
    raise errors.AutonomError(refusal["code"], refusal["message"], refusal["hint"], **extras)


def new_operation_id() -> str:
    """`op_<12 lowercase hex>`: attribution of one admitted mutation."""
    return f"op_{secrets.token_hex(6)}"


def _check_operation_id(operation_id: Any) -> str:
    if operation_id is None:
        return new_operation_id()
    if not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(operation_id):
        raise errors.AutonomError(
            errors.INVALID_VALUE,
            "the operation id must be 1-64 characters of A-Z a-z 0-9 . _ : -",
            "Pass --operation-id with letters, digits, '.', '_', ':' or '-' only, or omit it.",
            option="operation_id")
    return operation_id


def admit(platform: str, target_id: str, claim: Claim | None, *, now_ms: int | None = None,
          operation_id: str | None = None) -> dict:
    """Check admission for one mutation on the target at execution time.

    Raises the control `AutonomError` on refusal (no device call may follow).
    Returns `{"leased", "generation", "holder_id", "operation_id"}`:
    `leased` is true only when the claim's holder holds a live lease;
    `generation` is the lease file's generation when one exists, else None."""
    operation = _check_operation_id(operation_id)
    now = _now_ms() if now_ms is None else int(now_ms)
    lease = read_lease(platform, target_id)  # an unreadable file raises (fail closed)
    refusal = admission(lease, claim, now, platform=platform, target_id=target_id)
    if refusal is not None:
        _raise(refusal)
    leased = claim is not None and lease_state(lease, now) == "held"
    return {
        "leased": leased,
        "generation": lease.get("generation") if lease else None,
        "holder_id": claim.holder_id if leased and claim else None,
        "operation_id": operation,
    }


# --- Validation ---------------------------------------------------------------

def _invalid(message: str, hint: str, option: str) -> errors.AutonomError:
    return errors.AutonomError(errors.INVALID_VALUE, message, hint, option=option)


def _check_holder_id(holder_id: Any, option: str = "holder") -> str:
    if not isinstance(holder_id, str) or not HOLDER_ID_RE.fullmatch(holder_id):
        raise _invalid(
            "the control holder id must be 1-128 characters of A-Z a-z 0-9 . _ : @ -",
            "Pass a holder id such as 'agent:worker-1' (letters, digits, '.', '_', ':', '@', '-').",
            option)
    return holder_id


def _check_generation(generation: Any, *, required: bool, option: str = "generation") -> int | None:
    if generation is None:
        if required:
            raise _invalid("a control generation is required",
                           "Pass the generation your acquire returned (--generation N).", option)
        return None
    if _is_int(generation) and 1 <= generation <= MAX_GENERATION:
        return generation
    # The length check comes before int(): Python refuses to convert strings
    # of more than 4300 digits with a bare ValueError.
    if (isinstance(generation, str) and _DIGITS.fullmatch(generation)
            and len(generation) <= _MAX_GENERATION_DIGITS
            and 1 <= int(generation) <= MAX_GENERATION):
        return int(generation)
    raise _invalid(f"the control generation must be an integer from 1 to {MAX_GENERATION}",
                   "Pass the generation your acquire returned (an integer >= 1).", option)


def _check_kind(kind: Any) -> str:
    if kind not in KINDS:
        raise _invalid("the control holder kind must be 'human' or 'agent'",
                       "Pass --kind agent or --kind human.", "kind")
    return kind


def _check_label(label: Any, holder_id: str) -> str:
    if label is None:
        return holder_id[:MAX_LABEL]
    if (not isinstance(label, str) or not 1 <= len(label) <= MAX_LABEL
            or not label.isprintable()):
        raise _invalid(f"the control label must be 1-{MAX_LABEL} printable characters",
                       f"Pass a short one-line --label of at most {MAX_LABEL} characters.", "label")
    return label


def _check_ttl(ttl_s: Any, kind: str) -> int:
    if ttl_s is None:
        return HUMAN_TTL_S if kind == "human" else DEFAULT_AGENT_TTL_S
    if not _is_int(ttl_s) or not MIN_TTL_S <= ttl_s <= MAX_TTL_S:
        raise _invalid(f"the control lease ttl must be an integer from {MIN_TTL_S} to {MAX_TTL_S} seconds",
                       f"Pass --ttl between {MIN_TTL_S} and {MAX_TTL_S}.", "ttl")
    return ttl_s


def _kept_ttl(lease: dict, ttl_s: Any) -> int:
    """The ttl of a renewal: the explicit one when given, else the lease's own
    `ttl_ms` (a lease acquired with --ttl 600 keeps 600 s), else the kind
    default when the stored value is unusable."""
    kind = (lease.get("holder") or {}).get("kind")
    if ttl_s is not None:
        return _check_ttl(ttl_s, kind)
    stored = lease.get("ttl_ms")
    if _is_int(stored) and stored % 1000 == 0 and MIN_TTL_S <= stored // 1000 <= MAX_TTL_S:
        return stored // 1000
    return _check_ttl(None, kind)


def _check_session_id(session_id: Any) -> str | None:
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise _invalid("the session id recorded in a control lease is malformed",
                       "Pass the id of an Autonom session (s_...), or none.", "session_id")
    return session_id


def claim_from(holder: str | None, generation: str | int | None) -> Claim | None:
    """The control claim of an invocation. A flag wins over its environment
    variable (`AUTONOM_CONTROL_HOLDER`, `AUTONOM_CONTROL_GENERATION`), field by
    field; an empty environment value counts as absent. No holder and no
    generation: None. A generation without a holder: `usage_error`. Malformed
    values: `invalid_value`."""
    if holder is None:
        holder = os.environ.get(ENV_HOLDER) or None
        holder_option = ENV_HOLDER
    else:
        holder_option = "control_holder"
    if generation is None:
        generation = os.environ.get(ENV_GENERATION) or None
        generation_option = ENV_GENERATION
    else:
        generation_option = "control_generation"
    if holder is None:
        if generation is None:
            return None
        raise errors.AutonomError(
            errors.USAGE_ERROR, "a control generation was given without a control holder",
            "Pass --control-holder (or AUTONOM_CONTROL_HOLDER) together with "
            "--control-generation, or drop the generation.", option=generation_option)
    return Claim(_check_holder_id(holder, holder_option),
                 _check_generation(generation, required=False, option=generation_option))


# --- Writing ------------------------------------------------------------------

@contextlib.contextmanager
def _locked() -> Iterator[Path]:
    """Hold the exclusive lease lock; yields the lease directory. A failing
    filesystem (unwritable state store, full disk) becomes
    `output_not_writable`; the previous lease file is then left unchanged."""
    try:
        with _locked_raw() as directory:
            yield directory
    except OSError as exc:
        location = _control_path()
        raise errors.AutonomError(
            errors.OUTPUT_NOT_WRITABLE,
            f"the control lease store could not be written: {exc.strerror or exc.__class__.__name__}",
            f"Check free space and permissions of {location}.",
            path=str(location), capability=CAPABILITY) from exc


@contextlib.contextmanager
def _locked_raw() -> Iterator[Path]:
    directory = control_dir()
    lock_path = directory / LOCK_NAME
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _sweep_temporaries(directory: Path) -> None:
    """Remove `.lease-*.tmp` leftovers of a crashed writer older than 60 s."""
    cutoff = time.time() - STALE_TMP_S
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not (name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)):
            continue
        path = directory / name
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _write_atomic(path: Path, lease: dict) -> None:
    data = (json.dumps(lease, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=path.parent)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
    try:
        dir_descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_descriptor)
    except OSError:
        pass
    finally:
        os.close(dir_descriptor)


def _recovered_generation(raw: bytes | None) -> int:
    """The largest generation a damaged file still names (0 when none).
    Numbers above `MAX_GENERATION` are ignored (checked by length before
    `int()`, which refuses strings of more than 4300 digits); `MAX_GENERATION`
    itself is kept, so the next grant is refused as exhausted."""
    if not raw:
        return 0
    found = [int(match) for match in _GENERATION_IN_RAW.findall(raw)
             if len(match) <= _MAX_GENERATION_DIGITS]
    found = [value for value in found if value <= MAX_GENERATION]
    return max(found) if found else 0


def _refuse_foreign(path: Path, platform: str, target_id: str) -> None:
    raise errors.AutonomError(
        errors.INVALID_VALUE,
        f"the control lease file {path.name} belongs to another target than {platform} {target_id}",
        "Two target ids map to the same lease file name; release the other target's lease or "
        "use the exact target id.",
        option="target_id", path=str(path), capability=CAPABILITY,
        platform=platform, target_id=target_id)


def _read_locked(path: Path, platform: str, target_id: str) -> tuple[dict | None, bytes | None]:
    """The writer's read inside the lock: an I/O failure refuses the write
    (`output_not_writable`, fail closed: a grant never replaces a lease it
    could not read), and a file of another target is never overwritten."""
    try:
        lease, raw, foreign = _read_target(path, platform, target_id)
    except OSError as exc:
        raise _read_error(exc, path) from exc
    if foreign:
        _refuse_foreign(path, platform, target_id)
    return lease, raw


def _grant_answer(lease: dict, *, renewed: bool, taken_over: bool) -> dict:
    return {
        "ok": True,
        "acquired": True,
        "renewed": renewed,
        "taken_over": taken_over,
        "lease": lease,
        "generation": lease["generation"],
        "state": "held",
    }


def _stamp(lease: dict, now: int, *, reason: str) -> None:
    lease["reason"] = reason
    lease["updated_at"] = _iso(now)
    lease["updated_at_ms"] = now


def _extend(lease: dict, now: int, ttl_s: int) -> None:
    lease["renewed_at"] = _iso(now)
    lease["expires_at"] = _iso(now + ttl_s * 1000)
    lease["expires_at_ms"] = now + ttl_s * 1000
    lease["ttl_ms"] = ttl_s * 1000


def acquire(platform: str, target_id: str, *, holder_id: str, kind: str = "agent",
            label: str | None = None, ttl_s: int | None = None, takeover: bool = False,
            session_id: str | None = None, now_ms: int | None = None) -> dict:
    """Grant (or, for the same live holder, renew in place) the target's lease.

    A live lease of another holder is refused `control_held`; with `takeover`
    a human replaces it (generation + 1), an agent never does. A grant always
    increments the generation."""
    _check_target(platform, target_id)
    holder_id = _check_holder_id(holder_id)
    kind = _check_kind(kind)
    label = _check_label(label, holder_id)
    ttl = _check_ttl(ttl_s, kind)
    session_id = _check_session_id(session_id)
    path = lease_path(platform, target_id)
    with _locked():
        _sweep_temporaries(path.parent)
        now = _now_ms() if now_ms is None else int(now_ms)
        lease, raw = _read_locked(path, platform, target_id)
        state = lease_state(lease, now)
        current = (lease or {}).get("holder")
        if state == "held" and current and current["id"] == holder_id:
            renewed = dict(lease)
            _extend(renewed, now, _kept_ttl(lease, ttl_s))
            if session_id is not None:
                renewed["session_id"] = session_id
            _stamp(renewed, now, reason="renewed")
            _write_atomic(path, renewed)
            return _grant_answer(renewed, renewed=True, taken_over=False)
        if state == "held":
            claim = Claim(holder_id, None)
            refusal = _refusal(errors.CONTROL_HELD, lease, claim,
                               platform=platform, target_id=target_id)
            if not takeover:
                _raise(refusal)
            if kind != "human":
                refusal["hint"] = ("Only a human takes over a live lease (from the Canvas); wait "
                                   "until the holder releases it or the lease expires.")
                _raise(refusal, takeover_requires="human")
        if lease is not None:
            generation = lease["generation"] + 1
        else:
            generation = _recovered_generation(raw) + 1
        if generation > MAX_GENERATION:
            raise _invalid("the control generation of this target is exhausted",
                           f"Remove {path} by hand; generations stop at {MAX_GENERATION}.",
                           "generation")
        taken_over = state == "held"
        granted: dict[str, Any] = {
            "schema": SCHEMA,
            "platform": platform,
            "target_id": target_id,
            "generation": generation,
            "holder": {"kind": kind, "id": holder_id, "label": label},
            "session_id": session_id,
            "acquired_at": _iso(now),
        }
        _extend(granted, now, ttl)
        granted["previous_holder"] = dict(current) if taken_over and current else None
        _stamp(granted, now, reason="taken_over" if taken_over else "acquired")
        _write_atomic(path, granted)
        return _grant_answer(granted, renewed=False, taken_over=taken_over)


def _surely_missing(path: Path) -> bool:
    """True only when the lease file does not exist (ENOENT), so a release or
    renew on a missing lease answers without creating anything. Any other
    failure (an unsearchable control directory, EIO) is False: the caller
    then decides under the lock, where an I/O failure is
    `output_not_writable` (fail closed)."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def release(platform: str, target_id: str, *, holder_id: str, generation: int | None = None,
            now_ms: int | None = None) -> dict:
    """Release the holder's lease (live or expired); the generation is kept.
    A release never creates a file."""
    _check_target(platform, target_id)
    holder_id = _check_holder_id(holder_id)
    generation = _check_generation(generation, required=False)
    claim = Claim(holder_id, generation)
    path = lease_path(platform, target_id)
    if _surely_missing(path):
        _raise(_refusal(errors.CONTROL_NOT_HELD, None, claim, platform=platform, target_id=target_id))
    with _locked():
        _sweep_temporaries(path.parent)
        now = _now_ms() if now_ms is None else int(now_ms)
        lease, _raw = _read_locked(path, platform, target_id)
        state = lease_state(lease, now)
        current = (lease or {}).get("holder")
        if state in ("held", "expired") and current and current["id"] == holder_id:
            if generation is not None and generation != lease["generation"]:
                _raise(_refusal(errors.CONTROL_GENERATION_STALE, lease, claim,
                                platform=platform, target_id=target_id))
            released = dict(lease)
            released["holder"] = None
            released["previous_holder"] = dict(current)
            released["expires_at"] = None
            released["expires_at_ms"] = None
            _stamp(released, now, reason="released")
            _write_atomic(path, released)
            return {"ok": True, "released": True, "was_expired": state == "expired",
                    "lease": released}
        _raise(_refusal(errors.CONTROL_NOT_HELD, lease, claim, platform=platform, target_id=target_id))
    raise AssertionError("unreachable")  # pragma: no cover


def renew(platform: str, target_id: str, *, holder_id: str, generation: int,
          ttl_s: int | None = None, now_ms: int | None = None) -> dict:
    """Extend the holder's live lease at the same generation. An expired
    lease is never revived (`control_lease_expired`); other refusals follow
    the admission rows 6, 7, 8 and 10."""
    _check_target(platform, target_id)
    holder_id = _check_holder_id(holder_id)
    generation = _check_generation(generation, required=True)
    claim = Claim(holder_id, generation)
    path = lease_path(platform, target_id)
    if _surely_missing(path):
        _raise(_refusal(errors.CONTROL_NOT_HELD, None, claim, platform=platform, target_id=target_id))
    if ttl_s is not None:
        _check_ttl(ttl_s, "agent")
    with _locked():
        _sweep_temporaries(path.parent)
        now = _now_ms() if now_ms is None else int(now_ms)
        lease, _raw = _read_locked(path, platform, target_id)
        refusal = admission(lease, claim, now, platform=platform, target_id=target_id)
        if refusal is not None:
            _raise(refusal)
        assert lease is not None and lease.get("holder")
        renewed = dict(lease)
        _extend(renewed, now, _kept_ttl(lease, ttl_s))
        _stamp(renewed, now, reason="renewed")
        _write_atomic(path, renewed)
        return _grant_answer(renewed, renewed=True, taken_over=False)


# --- Status -------------------------------------------------------------------

_STATUS_KEYS = ("platform", "target_id", "state", "generation", "holder", "expires_at",
                "remaining_ms", "session_id", "updated_at", "took_over_from")


def _status_entry(lease: dict, now: int) -> dict:
    state = lease_state(lease, now)
    remaining = None
    if state == "held":
        remaining = max(0, lease["expires_at_ms"] - now)
    elif state == "expired":
        remaining = 0
    return {
        "platform": lease.get("platform"),
        "target_id": lease.get("target_id"),
        "state": state,
        "generation": lease.get("generation"),
        "holder": lease.get("holder"),
        "expires_at": lease.get("expires_at") if lease.get("holder") else None,
        "remaining_ms": remaining,
        "session_id": lease.get("session_id"),
        "updated_at": lease.get("updated_at"),
        "took_over_from": _took_over_from(lease, state),
    }


def _took_over_from(lease: dict, state: str) -> dict | None:
    """The holder a Take over replaced, while the lease it granted is still held."""
    previous = lease.get("previous_holder")
    if state != "held":
        return None
    return dict(previous) if isinstance(previous, dict) else None


def _unreadable_entry(platform: str | None, target_id: str | None, name: str) -> dict:
    entry = {key: None for key in _STATUS_KEYS}
    entry.update({"platform": platform, "target_id": target_id, "state": "unreadable", "file": name})
    return entry


def status(platform: str | None = None, target_id: str | None = None, *,
           now_ms: int | None = None) -> list[dict]:
    """Lock-free view of the lease files: one entry per file, or only the
    given target's (an empty list when it has none). Nothing is created."""
    now = _now_ms() if now_ms is None else int(now_ms)
    if platform is not None and target_id is not None:
        path = lease_path(platform, target_id)
        try:
            lease, raw, foreign = _read_target(path, platform, target_id)
        except OSError:
            return [_unreadable_entry(platform, target_id, path.name)]
        if lease is not None:
            return [_status_entry(lease, now)]
        if raw is None or foreign:
            return []
        return [_unreadable_entry(platform, target_id, path.name)]
    directory = _control_path()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    entries: list[dict] = []
    for name in names:
        if not _LEASE_NAME.fullmatch(name):
            continue
        try:
            lease, raw = _read_raw(directory / name)
        except OSError:
            lease, raw = None, b""
        if raw is None:
            continue  # removed between the listing and the read
        if lease is not None:
            entry = _status_entry(lease, now)
        else:
            entry = _unreadable_entry(name.split("__", 1)[0], None, name)
        if platform is not None and entry["platform"] != platform:
            continue
        if target_id is not None and entry["target_id"] != target_id:
            continue
        entries.append(entry)
    return entries


# --- Invocation context -------------------------------------------------------
# Module-level (not a ContextVar) so `report serve` worker threads see the
# invocation that the CLI's main() began.

def _fresh_invocation() -> dict[str, Any]:
    return {"claim": None, "operation_id": None, "mode": None, "admitted": {}}


_INVOCATION: dict[str, Any] = _fresh_invocation()


def begin_invocation(*, claim: Claim | None, operation_id: str | None, mode: str) -> None:
    """Start one CLI invocation's control context; resets the admitted set.
    Without an operation id one is generated, so a refusal and an admitted
    verb are both attributable."""
    global _INVOCATION
    if mode not in MODES:
        raise _invalid(f"unknown control mode {mode!r}", "Use one of: " + ", ".join(MODES), "mode")
    if claim is not None and not isinstance(claim, Claim):
        raise _invalid("the control claim must be a Claim", "Build it with claim_from().", "claim")
    _INVOCATION = {
        "claim": claim,
        "operation_id": _check_operation_id(operation_id),
        "mode": mode,
        "admitted": {},
    }


def end_invocation() -> None:
    global _INVOCATION
    _INVOCATION = _fresh_invocation()


def invocation() -> dict:
    current = _INVOCATION
    return {**current, "admitted": dict(current["admitted"])}


def admit_once(platform: str, target_id: str) -> dict | None:
    """In `mutating`/`flow` mode: `admit()` once per target per invocation and
    remember the admitted result (a refusal raises and is not remembered).
    In every other mode (or outside an invocation): None, nothing is read."""
    current = _INVOCATION
    if current["mode"] not in ADMITTED_MODES:
        return None
    key = (platform, target_id)
    known = current["admitted"].get(key)
    if known is not None:
        return known
    result = admit(platform, target_id, current["claim"], operation_id=current["operation_id"])
    current["admitted"][key] = result
    return result
