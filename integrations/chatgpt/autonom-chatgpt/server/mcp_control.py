"""The agent control lease keeper and the lease view (contract 3.6).

``LeaseKeeper`` holds the P2 lease of the agent holder
``chatgpt-agent:<host_id>`` per target:

- ``ensure(ref)`` reuses a cached lease with more than 10 s left, else runs
  ``control acquire`` (which renews an own live lease in place); a refusal is
  ``rejected`` with the lease view in ``control`` and drops the cache entry;
  ``--takeover`` is never used.
- ``drop(platform, target_id)`` forgets an entry (pause and resume do this);
  ``note(ref, result)`` drops it after a mutation answered with a control code.
- ``renew(ref)`` is the run keep-alive (``control renew``); ``release(ref)``
  releases with the cached generation (stop hook, shutdown, ``release_control``).
"""
from __future__ import annotations

import calendar
import re
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from mcp_cli import CONTROL_CODES, CliResult
from mcp_common import AGENT_HOLDER_LABEL, ToolError, agent_holder_id, new_operation_id, rejected

RENEW_MARGIN_S = 10.0
VIEW_STATES = ("none", "free", "held", "expired")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$")


def parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds of a UTC ``...Z`` timestamp, or None."""
    if not isinstance(value, str):
        return None
    match = _ISO.match(value)
    if not match:
        return None
    parts = [int(item) for item in match.groups()[:6]]
    fraction = match.group(7)
    seconds = calendar.timegm((parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], 0, 0, 0))
    return seconds + (float("0." + fraction) if fraction else 0.0)


def _holder(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return None
    label = value.get("label")
    return {"kind": str(value.get("kind") or "unknown")[:40], "id": value["id"][:200],
            "label": label[:200] if isinstance(label, str) else None}


def _generation(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def lease_view(entry: Optional[Mapping[str, Any]], mine_ids: Set[str]) -> Dict[str, Any]:
    """The adapter lease view from a ``control status`` entry or a Canvas ``lease``."""
    if not entry:
        return {"state": "none", "holder": None, "generation": None, "expires_at": None,
                "mine": False, "role": None, "took_over_from": None}
    state = entry.get("state")
    holder = _holder(entry.get("holder"))
    if state not in VIEW_STATES:
        state = "unknown" if state is not None else ("held" if holder else "free")
    role = holder["kind"] if holder and holder["kind"] in ("agent", "human") else None
    expires = entry.get("expires_at")
    return {
        "state": state,
        "holder": holder,
        "generation": _generation(entry.get("generation")),
        "expires_at": expires[:64] if isinstance(expires, str) else None,
        "mine": bool(holder and holder["id"] in mine_ids),
        "role": role,
        "took_over_from": _holder(entry.get("took_over_from")),
    }


def refusal_view(error: Mapping[str, Any], mine_ids: Set[str],
                 now: Optional[float] = None) -> Dict[str, Any]:
    """The lease view a CLI lease refusal implies (its holder, generation, expiry).

    The CLI names the lease's last holder in ``control_generation_stale`` and
    ``control_not_held`` refusals too, also when that lease already expired, so
    a holder is ``held`` only while its ``expires_at`` is still ahead.
    """
    code = error.get("error_code")
    holder = error.get("holder")
    generation = error.get("generation")
    expires = parse_iso(error.get("expires_at"))
    current = time.time() if now is None else now
    if code == "control_lease_expired":
        state = "expired"
    elif isinstance(holder, dict):
        state = "expired" if expires is not None and expires <= current else "held"
    elif generation is None:
        state = "none"
    else:
        state = "free"
    return lease_view({"state": state, "holder": holder, "generation": generation,
                       "expires_at": error.get("expires_at")}, mine_ids)


class LeaseKeeper:
    """Agent lease cache per ``(platform, target_id)``."""

    def __init__(self, runner: Any, refs: Any, config: Mapping[str, Any],
                 log: Callable[[str], None], clock: Callable[[], float] = time.time) -> None:
        self.runner = runner
        self.refs = refs
        self.config = config
        self.log = log
        self.clock = clock
        self.human_holder_ids: Set[str] = set()
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # -- identity and views

    def holder_id(self) -> str:
        return agent_holder_id(self.refs.local_host()["host_id"])

    def mine_ids(self) -> Set[str]:
        ids = set(self.human_holder_ids)
        host_id = self.refs.cached_host_id()
        if host_id:
            ids.add(agent_holder_id(host_id))
        return ids

    def view(self, entry: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        return lease_view(entry, self.mine_ids())

    def ttl(self) -> int:
        return int(self.config.get("agent_lease_ttl_s") or 120)

    @staticmethod
    def _key(ref: Mapping[str, Any]) -> Tuple[str, str]:
        return str(ref["platform"]), str(ref["target_id"])

    def cached(self, platform: str, target_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._cache.get((platform, target_id))
            return dict(entry) if entry else None

    def drop(self, platform: str, target_id: str) -> None:
        with self._lock:
            self._cache.pop((platform, target_id), None)

    def _store(self, ref: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
        generation = _generation(data.get("generation"))
        lease = data.get("lease") if isinstance(data.get("lease"), dict) else {}
        if generation is None:
            generation = _generation(lease.get("generation"))
        if generation is None:
            raise ToolError("cli_output_invalid", "The control answer has no generation")
        expires = parse_iso(lease.get("expires_at"))
        if expires is None:
            expires = self.clock() + self.ttl()
        entry = {"generation": generation, "expires_at": expires, "session_id": str(ref["session_id"])}
        with self._lock:
            self._cache[self._key(ref)] = entry
        return dict(entry)

    def _refused(self, ref: Mapping[str, Any], result: CliResult) -> ToolError:
        self.drop(*self._key(ref))
        if result.cli_error_code in CONTROL_CODES:
            return result.to_error(control=refusal_view(result.error, self.mine_ids(), self.clock()))
        return result.to_error()

    def note(self, ref: Mapping[str, Any], result: CliResult) -> Optional[Dict[str, Any]]:
        """After a mutation: drop the entry when the CLI answered a control code and
        return the lease view to report (None otherwise). Never repeats anything."""
        if result.cli_error_code in CONTROL_CODES:
            self.drop(*self._key(ref))
            return refusal_view(result.error, self.mine_ids(), self.clock())
        return None

    # -- lease operations

    def claim(self, ref: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        entry = self.cached(*self._key(ref))
        if entry is None:
            return None
        return {"holder": self.holder_id(), "generation": entry["generation"]}

    def ensure(self, ref: Mapping[str, Any], *, operation_id: Optional[str] = None) -> Dict[str, Any]:
        """The agent claim ``{holder, generation}`` for the session's target."""
        holder = self.holder_id()
        entry = self.cached(*self._key(ref))
        if entry is not None and entry["expires_at"] - self.clock() > RENEW_MARGIN_S:
            return {"holder": holder, "generation": entry["generation"]}
        result = self.runner.run("control_acquire", {
            "session_id": ref["session_id"], "holder": holder, "label": AGENT_HOLDER_LABEL,
            "ttl": self.ttl()}, operation_id=operation_id or new_operation_id())
        if not result.ok:
            raise self._refused(ref, result)
        stored = self._store(ref, result.data or {})
        return {"holder": holder, "generation": stored["generation"]}

    def renew(self, ref: Mapping[str, Any]) -> Dict[str, Any]:
        """Run keep-alive: ``control renew`` at the cached generation."""
        entry = self.cached(*self._key(ref))
        if entry is None:
            raise rejected("control_not_held", "This server holds no agent lease on the target")
        holder = self.holder_id()
        result = self.runner.run("control_renew", {
            "session_id": ref["session_id"], "holder": holder, "generation": entry["generation"],
            "ttl": self.ttl()})
        if not result.ok:
            raise self._refused(ref, result)
        stored = self._store(ref, result.data or {})
        return {"holder": holder, "generation": stored["generation"]}

    def release(self, ref: Mapping[str, Any], *, generation: Optional[int] = None,
                operation_id: Optional[str] = None) -> Dict[str, Any]:
        """Release the agent lease with the cached (or given) generation; the lease view after."""
        key = self._key(ref)
        entry = self.cached(*key)
        if generation is None:
            if entry is None:
                raise rejected("control_not_held", "This server holds no agent lease on the target",
                               control=None)
            generation = entry["generation"]
        result = self.runner.run("control_release", {
            "session_id": ref["session_id"], "holder": self.holder_id(), "generation": generation},
            operation_id=operation_id or new_operation_id())
        self.drop(*key)
        if not result.ok:
            raise self._refused(ref, result)
        lease = (result.data or {}).get("lease")
        return self.view(lease if isinstance(lease, dict) else None)

    def release_all(self) -> List[Dict[str, Any]]:
        """Release every lease this server holds (shutdown); never raises."""
        with self._lock:
            items = list(self._cache.items())
        results: List[Dict[str, Any]] = []
        for (platform, target_id), entry in items:
            ref = {"platform": platform, "target_id": target_id, "session_id": entry["session_id"]}
            try:
                self.release(ref)
                results.append({"target": "%s:%s" % (platform, target_id), "ok": True})
            except ToolError as exc:
                results.append({"target": "%s:%s" % (platform, target_id), "ok": False,
                                "error_code": exc.error_code})
            except Exception as exc:  # noqa: BLE001 - shutdown goes on
                results.append({"target": "%s:%s" % (platform, target_id), "ok": False,
                                "error_code": type(exc).__name__})
        return results

    def status(self, ref: Mapping[str, Any]) -> Dict[str, Any]:
        """The lease view of the session's target from ``control status``."""
        result = self.runner.run("control_status", {"session_id": ref["session_id"]})
        if not result.ok:
            raise result.to_error()
        data = result.data or {}
        lease = data.get("lease")
        if not isinstance(lease, dict):
            leases = data.get("leases")
            lease = leases[0] if isinstance(leases, list) and leases and isinstance(leases[0], dict) else None
        return self.view(lease)
