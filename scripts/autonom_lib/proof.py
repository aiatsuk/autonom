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
- a flow whose ``runFlow`` graph reaches a changed file (a subflow, at any
  depth) is selected — the parent is what exercises the subflow;
- a flow whose ``properties.covers`` globs (comma-separated, relative to
  the repo root) match a changed file is selected;
- a flow tagged ``pull-request`` is always selected.

The change set is the diff **plus untracked files** when comparing against
the working tree — a brand-new flow or source file is a change. A flow that
does not load is reported in ``invalid_flows``, never skipped silently, and
a ``pass`` that leaves changed files uncovered carries ``partial: true``
with a warning (the verdict vocabulary itself never changes).
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

from . import errors
from .flow import validator as flow_validator


def _git(repo: Path, argv: list[str], what: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", *argv],
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
            f"git {what} failed: {completed.stderr.strip()[:300]}",
            hint="Run from inside the repository; refs must exist locally.",
        )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def changed_files(repo: Path, base: str, head: str | None) -> list[str]:
    target = f"{base}...{head}" if head else base
    changed = _git(repo, ["diff", "--name-only", target], f"diff {target}")
    if head is None:
        # Against the working tree a new, never-added file is a change too;
        # `git diff` alone does not list it. Paths stay top-level relative,
        # exactly like the diff's.
        for name in _git(repo, ["ls-files", "--others", "--exclude-standard",
                                "--full-name"], "ls-files --others"):
            if name not in changed:
                changed.append(name)
    return changed


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


def _relative(path: Path, repo: Path) -> str | None:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _subflow_files(flow, source: Path) -> list[Path]:
    """Files a flow's runFlow steps reference, at any nesting depth."""
    found: list[Path] = []

    def walk(steps) -> None:
        for step in steps:
            if step.command == "runFlow" and "file" in step.args:
                found.append((source.resolve().parent / step.args["file"]).resolve())
            nested = step.args.get("commands")
            if isinstance(nested, list):
                walk(nested)

    walk((*flow.on_flow_start, *flow.steps, *flow.on_flow_complete))
    return found


def _flow_graph(file: Path, flow) -> tuple[list[Path], dict[str, Any] | None]:
    """-> (every subflow file reachable from ``file``, first load problem)."""
    reachable: list[Path] = []
    pending = [(file.resolve(), flow)]
    seen = {file.resolve()}
    while pending:
        source, current = pending.pop()
        for child in _subflow_files(current, source):
            if child in seen:
                continue  # cycles are flow check's to report; walk each once
            seen.add(child)
            reachable.append(child)
            try:
                pending.append((child, flow_validator.load_flow(child)))
            except errors.AutonomError as exc:
                return reachable, {"code": exc.code, "message": exc.message,
                                   "subflow": str(child)}
    return reachable, None


def select_suite(flows_dir: Path, repo: Path,
                 changed: list[str]) -> dict[str, Any]:
    """-> {selected [{path, flow, reasons}], covered [...], invalid [...]}.

    ``invalid`` lists every discovered flow that does not load (or whose
    subflow does not), repo-relative, with its error code — a flow that
    cannot be read is a coverage gap to report, not a file to forget.
    """
    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    invalid: list[dict[str, Any]] = []
    changed_set = set(changed)
    for file in flow_validator.discover(flows_dir):
        relative = _relative(file, repo)
        try:
            flow = flow_validator.load_flow(file)
        except errors.AutonomError as exc:
            invalid.append({"flow": relative or str(file),
                            "error_code": exc.code, "error": exc.message})
            continue
        reasons: list[str] = []
        if relative and relative in changed_set:
            reasons.append("flow file changed")
            covered.add(relative)
        reachable, problem = _flow_graph(file, flow)
        if problem is not None:
            invalid.append({"flow": relative or str(file),
                            "error_code": problem["code"],
                            "error": problem["message"],
                            "subflow": _relative(Path(problem["subflow"]), repo)
                            or problem["subflow"]})
        changed_children = sorted({name for name in
                                   (_relative(child, repo) for child in reachable)
                                   if name and name in changed_set})
        if changed_children:
            reasons.append(f"subflow changed: {', '.join(changed_children[:5])}")
            covered.update(changed_children)
        matched = _covers(flow, changed)
        if matched:
            reasons.append(f"covers: {', '.join(sorted(set(matched))[:5])}")
            covered.update(matched)
        if "pull-request" in flow.tags:
            reasons.append("tagged pull-request")
        if reasons:
            selected.append({"path": file, "flow": flow, "reasons": reasons})
    return {"selected": selected, "covered": sorted(covered), "invalid": invalid}


def select_flows(flows_dir: Path, repo: Path,
                 changed: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """-> (selected [{path, flow, reasons}], covered_files).

    Kept for callers of the two-value shape; ``select_suite`` also returns
    the invalid flows.
    """
    suite = select_suite(flows_dir, repo, changed)
    return suite["selected"], suite["covered"]


def coverage_flags(status: str, uncovered: list[str],
                   invalid: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Extra result keys that keep a ``pass`` honest about its gaps.

    A pass with changed files no flow selected is a *partial* pass: the
    selected suite passed, but not everything changed was verified.
    Invalid flows are a warning on any verdict.
    """
    flags: dict[str, Any] = {}
    warnings: list[str] = []
    if status == "pass" and uncovered:
        flags["partial"] = True
        warnings.append(f"partial pass: {len(uncovered)} changed file(s) have "
                        "no selecting flow and were not verified")
    if invalid:
        warnings.append(f"{len(invalid)} flow file(s) failed to load and were "
                        "not considered for selection")
    if warnings:
        flags["warnings"] = warnings
    return flags


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
    # Leftover uncovered files do not soften the verdict: the suite that WAS
    # selected passed, and the per-file gaps are listed by name beside it.
    return "pass"


def _display_path(value: str, repo: Path | None) -> str:
    """Repo-relative for proof.md: absolute paths leak the author's machine
    and do not resolve for a reviewer."""
    path = Path(value)
    if not path.is_absolute():
        return value
    bases = [repo] if repo is not None else []
    bases.append(flow_validator.workspace_root(path))
    for base in bases:
        relative = _relative(path, base)
        if relative:
            return relative
    return path.name


def render_markdown(result: dict[str, Any], repo: Path | None = None) -> str:
    if repo is None and result.get("repo"):
        repo = Path(result["repo"])
    status = result["status"]
    flags = coverage_flags(status, result.get("uncovered_files") or [],
                           result.get("invalid_flows"))
    partial = result.get("partial", flags.get("partial", False))
    title = status.upper() + (" (partial)" if partial else "")
    lines = [f"# Autonom proof: {title}", ""]
    lines.append(f"`{result['base']}` → `{result.get('head') or 'worktree'}` · "
                 f"{len(result['changed_files'])} changed file(s)")
    lines.append("")
    if result.get("blocked_reason"):
        lines += [f"**Blocked:** {result['blocked_reason']}", ""]
    for warning in result.get("warnings") or flags.get("warnings") or []:
        lines.append(f"> **Warning:** {warning}")
    if result.get("warnings") or flags.get("warnings"):
        lines.append("")
    if result["changed_areas"]:
        lines.append("**Changed areas:**")
        lines += [f"- {area}" for area in result["changed_areas"]]
        lines.append("")
    if result["runs"]:
        lines.append("**Verified:**")
        for run in result["runs"]:
            mark = {"passed": "✅", "failed": "❌"}.get(run["status"], "▫️")
            lines.append(f"- {mark} `{_display_path(run['flow'], repo)}` — "
                         f"{run['status']}"
                         + (f" ({run['failure']['error_code']})"
                            if run.get("failure") else ""))
        lines.append("")
    if result["uncovered_files"]:
        lines.append("**Not covered** (changed, no selecting flow):")
        lines += [f"- {name}" for name in result["uncovered_files"][:15]]
        if len(result["uncovered_files"]) > 15:
            lines.append(f"- … and {len(result['uncovered_files']) - 15} more")
        lines.append("")
    if result.get("invalid_flows"):
        lines.append("**Invalid flows** (failed to load, not selectable):")
        lines += [f"- `{_display_path(item['flow'], repo)}` — "
                  f"{item.get('error_code')}"
                  for item in result["invalid_flows"][:15]]
        lines.append("")
    lines.append(f"_Selected {len(result['selected'])} flow(s); "
                 "a missing edge means unverified, never proven-safe._")
    return "\n".join(lines) + "\n"
