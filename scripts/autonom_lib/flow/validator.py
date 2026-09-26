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

import hashlib
import re
from pathlib import Path

from .. import errors
from . import FLOW_SCHEMA_ID
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
        raw = path.read_bytes()
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
    # Decoded exactly as ``read_text`` would (universal newlines), from the
    # bytes that are also hashed: ``source_sha256`` names what was parsed,
    # not what the file holds by the time someone asks.
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    # before the Maestro sniff: a BOM hides the `schema:` line from it
    text = parser_mod.strip_bom(text)
    if maestro_mod.is_maestro_document(text):
        canonical = maestro_mod.import_flow(text, str(path))
        flow = schema_mod.build_flow(
            parser_mod.parse_document(canonical, str(path)))
        flow.converted_from = "maestro"
    else:
        flow = schema_mod.build_flow(parser_mod.parse_document(text, str(path)))
    flow.source_sha256 = digest
    return flow


def source_sha256(flow: schema_mod.Flow) -> str | None:
    """sha256 of the bytes ``load_flow`` parsed; None for a flow built in
    memory (never loaded from a file)."""
    return getattr(flow, "source_sha256", None)


def _all_steps(steps):
    """Every step, composite bodies included (inline runFlow, group, retry,
    repeat) — the same file only, never a runFlow ``file:`` child."""
    for step in steps:
        yield step
        nested = step.args.get("commands")
        if isinstance(nested, list):
            yield from _all_steps(nested)


def _check_links(flow: schema_mod.Flow) -> None:
    """An ``openLink`` URL must be absolute (carry a scheme).

    A recorded ``open <url> --serial S`` once compiled to ``openLink:
    emulator-5556``; the OS cannot open that, and the run only failed on the
    device. A URL that starts with ``${VAR}`` is judged at run time.
    """
    for step in _all_steps((*flow.on_flow_start, *flow.steps,
                            *flow.on_flow_complete)):
        if step.command != "openLink":
            continue
        url = step.args.get("url", "")
        if schema_mod.url_has_scheme(url) is False:
            raise errors.AutonomError(
                errors.FLOW_COMMAND_INVALID,
                f"{flow.path}:{step.line}:{step.col}: openLink url {url!r} has "
                "no scheme",
                hint="openLink takes an absolute URL: myapp://path or "
                     "https://host/path.",
                file=flow.path, line=step.line, column=step.col,
                command="openLink",
            )


def _ios_blockers(flow: schema_mod.Flow, path: Path, cache: dict) -> list:
    """``(owner flow, step, (code, message, hint))`` for every step that would
    run on iOS but cannot.

    Walks hooks, composite bodies, and runFlow ``file:`` children already in
    ``cache`` (a child's own hooks never run, as in the executor). A runFlow
    whose ``when.platform`` names another platform never runs on iOS, so its
    body does not count.
    """
    found: list = []
    visited: set = set()

    def walk(steps, owner: schema_mod.Flow, owner_path: Path) -> None:
        for step in steps:
            if step.command == "runFlow":
                when = step.args.get("when")
                if when is not None and when.platform and when.platform != "ios":
                    continue
                if "commands" in step.args:
                    walk(step.args["commands"], owner, owner_path)
                elif "file" in step.args:
                    target = (owner_path.parent / step.args["file"]).resolve()
                    child = cache.get(target)
                    if child is not None and target not in visited:
                        visited.add(target)
                        walk(child.steps, child, target)
                continue
            nested = step.args.get("commands")
            if isinstance(nested, list):
                walk(nested, owner, owner_path)
                continue
            reason = schema_mod.android_only(step)
            if reason is not None:
                found.append((owner, step, reason))

    walk((*flow.on_flow_start, *flow.steps, *flow.on_flow_complete), flow, path)
    return found


def _platforms(flow: schema_mod.Flow, blockers: list) -> list[str]:
    declared = list(flow.requires_platforms or schema_mod.PLATFORMS)
    if blockers:
        declared = [platform for platform in declared if platform != "ios"]
    return declared


def flow_platforms(flow: schema_mod.Flow) -> list[str]:
    """The platforms ``flow`` can actually run on.

    ``requires.platform`` when declared, else both — minus iOS when a step
    that runs there is Android-only (``back``, ``clearState``,
    ``setOrientation``, ``launchApp.clearState``, a ``KEYCODE_*`` key). A flow
    from ``validate_tree`` counts its runFlow children; one from ``load_flow``
    only its own steps.
    """
    inferred = getattr(flow, "inferred_platforms", None)
    if inferred is not None:
        return list(inferred)
    return _platforms(flow, _ios_blockers(flow, Path(flow.path), {}))


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
    _check_links(flow)
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
            if target in stack:
                # Named at the runFlow step that closes the loop, so the
                # envelope points at a line to edit, not only at a chain.
                chain = [str(p) for p in (*stack[stack.index(target):], target)]
                raise errors.AutonomError(
                    errors.FLOW_CYCLE_DETECTED,
                    f"{flow.path}:{step.line}:{step.col}: runFlow cycle: "
                    f"{' -> '.join(chain)}",
                    hint="Subflows must form a tree; extract the shared part "
                         "instead of calling back.",
                    chain=chain, file=flow.path, line=step.line,
                    column=step.col, target=str(target),
                )
            validate_tree(target, root=root, _stack=stack, _cache=cache)
    finally:
        stack.pop()
    blockers = _ios_blockers(flow, resolved, cache)
    if blockers and "ios" in flow.requires_platforms:
        owner, step, (code, message, hint) = blockers[0]
        raise errors.AutonomError(
            code,
            f"{owner.path}:{step.line}:{step.col}: {message}, but "
            f"{flow.path} declares requires.platform ios",
            hint=f"{hint} Or drop ios from requires.platform.",
            file=owner.path, line=step.line, column=step.col,
            command=step.command, platforms=flow.requires_platforms,
        )
    flow.inferred_platforms = _platforms(flow, blockers)
    cache[resolved] = flow
    return flow


def subflow_files(path: Path) -> list[Path]:
    """Every file reached from ``path`` through runFlow (resolved, sorted,
    the root itself excluded) — the same graph a run of ``path`` loads."""
    cache: dict = {}
    validate_tree(path, _cache=cache)
    root = path.resolve()
    return sorted(item for item in cache if item != root)


# App Skill overlays (`app-skill promote`) live under `.autonom/apps/<id>/`:
# selectors.yaml, fixtures.yaml, compatibility.yaml, and promoted *copies* of
# flows. None of them is a suite member — scaffolds are not flows, and a
# promoted copy would run the same journey twice.
_OVERLAY_PARTS = (".autonom", "apps")
_FLOW_SCHEMA_FAMILY = FLOW_SCHEMA_ID.rsplit("/", 1)[0] + "/"
_SCHEMA_LINE_RE = re.compile(r"^schema\s*:\s*['\"]?([^'\"#\s]+)")


def _foreign_schema(path: Path) -> str | None:
    """The non-flow ``schema:`` a header names (a sibling format), else None.

    Decided by the documented header, never by a parse attempt: a flow with
    ``schema: autonom.dev/flow/<anything>`` is kept (an unsupported version
    must still fail loudly), and a Maestro document has no ``schema:`` at
    all, so both stay suite members.
    """
    try:
        text = parser_mod.strip_bom(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None  # unreadable: let load_flow report it
    for line in text.split("\n"):
        if line.strip() == "---":
            return None
        match = _SCHEMA_LINE_RE.match(line)
        if match:
            schema = match.group(1)
            return None if schema.startswith(_FLOW_SCHEMA_FAMILY) else schema
    return None


def _declares_foreign_schema(path: Path) -> bool:
    return _foreign_schema(path) is not None


def _is_overlay(path: Path, directory: Path) -> bool:
    parts = path.relative_to(directory).parts
    return any(parts[i:i + 2] == _OVERLAY_PARTS for i in range(len(parts) - 1))


def discover(directory: Path) -> list[Path]:
    """All flow files under a directory, stable order.

    App Skill overlay trees and files that declare another Autonom schema
    (``autonom.selectors/v1`` and friends) are not flows and are skipped;
    ``discover_with_skipped`` names them.
    """
    return discover_with_skipped(directory)[0]


def discover_with_skipped(directory: Path) -> tuple[list[Path], list[dict]]:
    """``discover`` plus every ``*.yaml`` it left out and why.

    A skipped file is silent in ``discover``; a mistyped header such as
    ``schema: autonom.dev/flows/v1`` looks exactly like a sibling format, so
    ``flow run|check|list <dir>`` should warn with this list instead of
    running fewer flows than the directory holds. Each entry is
    ``{"file", "reason", "schema"?}`` with ``reason`` one of
    ``app_skill_overlay`` or ``non_flow_schema``.
    """
    flows: list[Path] = []
    skipped: list[dict] = []
    for path in sorted(directory.rglob("*.yaml")):
        if not path.is_file():
            continue
        if _is_overlay(path, directory):
            skipped.append({"file": str(path), "reason": "app_skill_overlay"})
            continue
        schema = _foreign_schema(path)
        if schema is not None:
            skipped.append({"file": str(path), "reason": "non_flow_schema",
                            "schema": schema})
            continue
        flows.append(path)
    return flows, skipped


def skipped_warning(skipped: list[dict]) -> dict | None:
    """One warning for the non-flow-schema files ``discover`` skipped.

    App Skill overlays are skipped by design and never warned about; a
    file whose header names another schema might be a typo, so it is.
    """
    foreign = [item for item in skipped if item.get("reason") == "non_flow_schema"]
    if not foreign:
        return None
    schemas = sorted({item["schema"] for item in foreign})
    return {
        "code": "flow_files_skipped",
        "message": (f"skipped {len(foreign)} file(s) (non-Flow schema: "
                    f"{', '.join(schemas)})"),
        "hint": (f"A Flow v1 file declares 'schema: {FLOW_SCHEMA_ID}'; fix a "
                 "mistyped header, or ignore this if the file is another format."),
        "files": [item["file"] for item in foreign],
        "schemas": schemas,
    }
