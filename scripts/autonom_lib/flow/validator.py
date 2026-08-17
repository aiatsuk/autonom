"""Flow v1 semantic validation over files: load, contain, and walk subflows.

``flow check`` promises that an invalid path is refused before any device
action, so the whole ``runFlow`` graph is loaded and validated statically:

- subflow paths resolve relative to the referencing flow's directory;
- the resolved real path (symlinks followed — ``Path.resolve()``, not string
  normalization) must stay inside the workspace root;
- recursion and cycles are refused with the full chain named;
- every reached file must parse and build.

Workspace root (decision D4): the nearest ancestor of the *root* flow's
directory that contains a ``.autonom`` directory; else the root flow's own
directory.
"""
from __future__ import annotations

from pathlib import Path

from .. import errors
from . import maestro as maestro_mod
from . import parser as parser_mod
from . import schema as schema_mod


def workspace_root(flow_path: Path) -> Path:
    directory = flow_path.resolve().parent
    for ancestor in (directory, *directory.parents):
        if (ancestor / ".autonom").is_dir():
            return ancestor
    return directory


def load_flow(path: Path) -> schema_mod.Flow:
    """Parse + build one file (no subflow traversal).

    A file whose header carries no ``schema:`` field is a Maestro document
    (decision D6, Phase 6): it is converted through the Core Profile importer
    on the fly — same refusals as ``flow import`` — and the returned flow is
    marked ``converted_from = "maestro"``. Nested ``runFlow`` children go
    through this same loader, so a Maestro tree converts as a whole.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise errors.AutonomError(
            errors.FLOW_FILE_NOT_FOUND, f"flow file not found: {path}",
            hint="Check the path; flow files use the .yaml extension.",
            file=str(path),
        )
    except IsADirectoryError:
        raise errors.AutonomError(
            errors.FLOW_FILE_NOT_FOUND, f"{path} is a directory, not a flow file",
            file=str(path),
        )
    if maestro_mod.is_maestro_document(text):
        canonical = maestro_mod.import_flow(text, str(path))
        flow = schema_mod.build_flow(
            parser_mod.parse_document(canonical, str(path)))
        flow.converted_from = "maestro"
        return flow
    document = parser_mod.parse_document(text, str(path))
    return schema_mod.build_flow(document)


def _subflow_steps(flow: schema_mod.Flow):
    def walk(steps):
        for step in steps:
            if step.command == "runFlow" and "file" in step.args:
                yield step
                continue
            # every nested command list may reference subflow files:
            # inline runFlow bodies, group, retry, repeat
            nested = step.args.get("commands")
            if isinstance(nested, list):
                yield from walk(nested)
    yield from walk((*flow.on_flow_start, *flow.steps, *flow.on_flow_complete))


def validate_tree(path: Path, root: Path | None = None,
                  _stack: list | None = None,
                  _cache: dict | None = None) -> schema_mod.Flow:
    """Validate ``path`` and every flow reachable through runFlow."""
    resolved = path.resolve()
    root = root or workspace_root(resolved)
    stack = _stack if _stack is not None else []
    cache = _cache if _cache is not None else {}

    if resolved in stack:
        chain = [str(p) for p in (*stack[stack.index(resolved):], resolved)]
        raise errors.AutonomError(
            errors.FLOW_CYCLE_DETECTED,
            f"runFlow cycle: {' -> '.join(chain)}",
            hint="Subflows must form a tree; extract the shared part instead "
                 "of calling back.",
            chain=chain,
        )
    if resolved in cache:
        return cache[resolved]

    flow = load_flow(resolved)
    stack.append(resolved)
    try:
        for step in _subflow_steps(flow):
            target = (resolved.parent / step.args["file"]).resolve()
            if not target.is_relative_to(root):
                raise errors.AutonomError(
                    errors.FLOW_PATH_ESCAPES_WORKSPACE,
                    f"{flow.path}:{step.line}:{step.col}: runFlow target "
                    f"{step.args['file']!r} resolves outside the workspace root {root}",
                    hint="Subflows must live inside the workspace; symlinks are "
                         "resolved before the check.",
                    file=flow.path, line=step.line, column=step.col,
                    target=str(target), workspace=str(root),
                )
            if not target.exists():
                raise errors.AutonomError(
                    errors.FLOW_FILE_NOT_FOUND,
                    f"{flow.path}:{step.line}:{step.col}: runFlow target "
                    f"{step.args['file']!r} does not exist",
                    file=flow.path, line=step.line, column=step.col,
                    target=str(target),
                )
            validate_tree(target, root=root, _stack=stack, _cache=cache)
    finally:
        stack.pop()
    cache[resolved] = flow
    return flow


def discover(directory: Path) -> list[Path]:
    """All flow files under a directory, stable order."""
    return sorted(p for p in directory.rglob("*.yaml") if p.is_file())
