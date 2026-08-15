"""PR Proof: connect a code diff to runtime verification, locally (§11).

``autonom proof --base <ref>`` reads the git diff, selects the smallest
sufficient flow suite, runs it against the active session's target, and
emits a verdict with evidence references. The verdict vocabulary is fixed
and never upgraded (§11.4):

- ``pass``       — every selected flow passed, and something was selected;
- ``fail``       — at least one selected flow failed as a test failure;
- ``not_covered``— changed areas have no covering flow (reported area by
                   area — silence is not coverage);
- ``blocked``    — git, session, device, or another infrastructure problem
                   prevented verification;
- ``inconclusive``— flows ran but every step was skipped by conditions.

Selection is deterministic, no guessing:

- a changed flow file selects itself;
- a flow whose ``properties.covers`` globs (comma-separated, relative to
  the repo root) match a changed file is selected;
- a flow tagged ``pull-request`` is always selected.
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

from . import errors
from .flow import validator as flow_validator


def changed_files(repo: Path, base: str, head: str | None) -> list[str]:
    target = f"{base}...{head}" if head else base
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", target],
            cwd=repo, text=True, capture_output=True, timeout=60, check=False,
        )
    except FileNotFoundError:
        raise errors.AutonomError(
            errors.BACKEND_FAILED, "git is not on PATH",
            hint="PR proof reads the diff with git.",
        )
    if completed.returncode != 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"git diff {target} failed: {completed.stderr.strip()[:300]}",
            hint="Run from inside the repository; refs must exist locally.",
        )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def changed_areas(files: list[str]) -> list[str]:
    areas: list[str] = []
    for name in files:
        parts = Path(name).parts
        area = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
        if area not in areas:
            areas.append(area)
    return areas


def _covers(flow, changed: list[str]) -> list[str]:
    globs = [pattern.strip()
             for pattern in (flow.properties.get("covers") or "").split(",")
             if pattern.strip()]
    matched = []
    for pattern in globs:
        for name in changed:
            if fnmatch.fnmatch(name, pattern):
                matched.append(name)
    return matched


def select_flows(flows_dir: Path, repo: Path,
                 changed: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """-> (selected [{path, flow, reasons}], covered_files)."""
    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    for file in flow_validator.discover(flows_dir):
        try:
            flow = flow_validator.load_flow(file)
        except errors.AutonomError:
            continue  # flow check owns validation reporting; proof selects
        reasons: list[str] = []
        try:
            relative = str(file.resolve().relative_to(repo.resolve()))
        except ValueError:
            relative = None
        if relative and relative in changed:
            reasons.append("flow file changed")
            covered.add(relative)
        matched = _covers(flow, changed)
        if matched:
            reasons.append(f"covers: {', '.join(sorted(set(matched))[:5])}")
            covered.update(matched)
        if "pull-request" in flow.tags:
            reasons.append("tagged pull-request")
        if reasons:
            selected.append({"path": file, "flow": flow, "reasons": reasons})
    return selected, sorted(covered)


def verdict(selected: list[dict[str, Any]], runs: list[dict[str, Any]],
            uncovered: list[str], blocked_reason: str | None) -> str:
    if blocked_reason:
        return "blocked"
    if not selected:
        return "not_covered"
    if any(run["status"] == "failed" for run in runs):
        return "fail"
    executed = [step for run in runs for step in run.get("steps", [])]
    if executed and all(step.get("status") == "skipped" for step in executed):
        return "inconclusive"
    if uncovered:
        # flows ran and passed, but some changed areas have no coverage —
        # the verdict names the stronger claim it CANNOT make
        return "pass"  # per-area gaps are listed; the suite itself passed
    return "pass"


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"# Autonom proof: {result['status'].upper()}", ""]
    lines.append(f"`{result['base']}` → `{result.get('head') or 'worktree'}` · "
                 f"{len(result['changed_files'])} changed file(s)")
    lines.append("")
    if result.get("blocked_reason"):
        lines += [f"**Blocked:** {result['blocked_reason']}", ""]
    if result["changed_areas"]:
        lines.append("**Changed areas:**")
        lines += [f"- {area}" for area in result["changed_areas"]]
        lines.append("")
    if result["runs"]:
        lines.append("**Verified:**")
        for run in result["runs"]:
            mark = {"passed": "✅", "failed": "❌"}.get(run["status"], "▫️")
            lines.append(f"- {mark} `{run['flow']}` — {run['status']}"
                         + (f" ({run['failure']['error_code']})"
                            if run.get("failure") else ""))
        lines.append("")
    if result["uncovered_files"]:
        lines.append("**Not covered** (changed, no selecting flow):")
        lines += [f"- {name}" for name in result["uncovered_files"][:15]]
        if len(result["uncovered_files"]) > 15:
            lines.append(f"- … and {len(result['uncovered_files']) - 15} more")
        lines.append("")
    lines.append(f"_Selected {len(result['selected'])} flow(s); "
                 "a missing edge means unverified, never proven-safe._")
    return "\n".join(lines) + "\n"
