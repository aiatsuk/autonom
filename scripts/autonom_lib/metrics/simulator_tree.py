"""Whole-simulator memory: every host process of one booted iOS Simulator.

A simulator is a tree of ordinary host processes under its `launchd_sim`.
Their summed physical footprint is the number that decides how many
simulators fit on a machine, and what `simulator slim` cuts. Three host
calls, each once: `pgrep` for the root, `ps` for the tree and CPU, `top`
for the footprints.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Callable, Sequence

from .. import errors, ios_simctl, ios_slim
from ..platform import ANDROID, Target

SEMANTICS = "ios_simulator_tree_footprint_v1"
ROOT_MARKER = "{udid}/data/var/run/launchd_bootstrap"
TOP_PROCESSES = 5
HOST_TIMEOUT = 60
LIMITATIONS = [
    "Footprint is the host's phys_footprint of each simulator process (top's MEM "
    "column), not guest jetsam accounting",
    "One sample; a simulator keeps settling for minutes after boot",
    "Simulator.app and CoreSimulatorService are shared by all simulators and are "
    "not counted",
]
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)([BKMGT]?)[+-]?$")
_UNITS = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_host(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(list(argv), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False, timeout=HOST_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise errors.AutonomError(
            errors.TOOL_MISSING, f"host '{argv[0]}' unavailable: {exc}",
            f"'{argv[0]}' ships with macOS; check PATH.", tool=argv[0]) from exc


def parse_memory(value: str) -> int | None:
    """`top`'s MEM column in bytes: `1234K`, `56M+`, `1.5G-`, `0B` (binary
    suffixes; a trailing + or - marks growth since the last sample)."""
    match = _MEMORY.match(value.strip().upper())
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS[match.group(2)])


def parse_ps(text: str) -> dict[int, tuple[int, float, str]]:
    """`ps -axo pid,ppid,%cpu,comm` -> pid: (ppid, cpu percent, command)."""
    table: dict[int, tuple[int, float, str]] = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            cpu = float(parts[2].replace(",", "."))
        except ValueError:
            cpu = 0.0
        table[int(parts[0])] = (int(parts[1]), cpu, parts[3].strip())
    return table


def parse_top(text: str) -> dict[int, int]:
    """`top -l 1 -stats pid,mem` -> pid: footprint bytes."""
    memory: dict[int, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        value = parse_memory(parts[1])
        if value is not None:
            memory[int(parts[0])] = value
    return memory


def tree_pids(root: int, table: dict[int, tuple[int, float, str]]) -> list[int]:
    """`root` and every descendant in the `ps` table."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _cpu, _comm) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    queue = [root]
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        queue.extend(children.get(pid, ()))
    return sorted(seen)


def find_root(udid: str, runner: Runner) -> int | None:
    """The simulator's launchd_sim: the first process whose command line
    names this device's bootstrap path."""
    completed = runner(["pgrep", "-f", ROOT_MARKER.format(udid=udid)])
    for line in (completed.stdout or "").splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def slim_block(udid: str) -> dict[str, Any]:
    """The slim state from the override store; `unknown` when it cannot be
    read. Best effort: it never fails the measurement."""
    try:
        summary = ios_slim.summarize(ios_slim.disabled_in(ios_slim.read_store(udid)))
        return {"state": summary["state"], "disabled": summary["disabled"],
                "total": summary["total"], "source": "store"}
    except Exception:  # noqa: BLE001 - a side note must not break the metric
        return {"state": "unknown", "disabled": None, "source": "store"}


def _not_booted(udid: str, state: str | None, reason: str) -> errors.AutonomError:
    return errors.AutonomError(
        errors.UNSUPPORTED_CAPABILITY,
        f"simulator {udid} is {state or 'unknown'}; {reason}",
        hint="Boot it first ('autonom devices boot --udid <UDID>'), then retry.",
        capability="metrics.simulator", target_id=udid, state=state)


def take(target: Target, *, runner: Runner = run_host) -> dict[str, Any]:
    if target.platform == ANDROID:
        raise errors.AutonomError(
            errors.UNSUPPORTED_CAPABILITY,
            "metrics simulator sums the host processes of an iOS Simulator; an "
            "Android emulator is one qemu process",
            hint="Use it with --platform ios --udid <UDID>; for an app's memory on "
                 "Android use 'autonom metrics snapshot'.",
            capability="metrics.simulator")
    udid = ios_slim.require_udid(target.target_id)
    found = ios_simctl.find_simulator(target.tool, udid)
    if found is None:
        raise errors.AutonomError(
            errors.NO_TARGET, f"no simulator with UDID {udid}",
            "List the simulators with 'autonom devices --platform ios'.", target_id=udid)
    if found.state != "Booted":
        raise _not_booted(udid, found.state, "only a booted simulator has processes "
                                             "to measure")
    root = find_root(udid, runner)
    if root is None:
        raise _not_booted(udid, found.state, "its launchd_sim process was not found "
                                             "on this host")
    table = parse_ps(runner(["ps", "-axo", "pid,ppid,%cpu,comm"]).stdout or "")
    if root not in table:
        raise _not_booted(udid, found.state, "its launchd_sim process ended while it "
                                             "was measured")
    pids = tree_pids(root, table)
    memory = parse_top(runner(["top", "-l", "1", "-stats", "pid,mem"]).stdout or "")
    limitations = list(LIMITATIONS)
    measured = [pid for pid in pids if pid in memory]
    if len(measured) < len(pids):
        limitations.append(f"{len(pids) - len(measured)} of {len(pids)} processes had "
                           "no footprint in the top sample (started or ended between "
                           "the two host calls)")
    largest = sorted(measured, key=lambda pid: (-memory[pid], pid))[:TOP_PROCESSES]
    return {
        "ok": True, "platform": "ios", "metric_semantics": SEMANTICS,
        "root_pid": root,
        "processes": len(pids),
        "footprint_bytes": sum(memory[pid] for pid in measured),
        "cpu_percent": round(sum(table[pid][1] for pid in pids), 1),
        "top": [{"name": table[pid][2].rsplit("/", 1)[-1], "pid": pid,
                 "bytes": memory[pid]} for pid in largest],
        "slim": slim_block(udid),
        "source": "host_pgrep+ps+top",
        "limitations": limitations,
    }
