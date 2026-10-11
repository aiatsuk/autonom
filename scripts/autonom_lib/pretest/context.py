"""Diff context for a pre-test (tp-002): what changed, where, and what covers it.

``diff_files`` reads the diff three ways — ``--name-status`` for status and
renames, ``--numstat`` for binary files, ``-U0`` for the changed line ranges of
the new side and the hunk header's function context — and caps the hunk data
it reads (``diff_truncated``) while still naming every changed file. The
function context is git's nearest preceding top-level line, so it passes
through the pre-test's free-text masking (``store.clip``) before it is kept.
It is kept for code files only (not for ``NON_CODE_SUFFIXES``: Markdown,
text, ARB, JSON, YAML and other data files, where it is just a nearby line),
and never when it is an import, export, package, library, part, using or
include statement.

``build`` adds what ``autonom proof`` would select (``proof.changed_files``,
``proof.changed_areas``, ``proof.select_suite``) and Runtime Map hints: screens
whose labels match a changed area. Hints are leads for planning checks, marked
``hint: true``, and never count as coverage.

``profile`` is the app profile (``profile.load``: the workspace's
``.autonom/apps/<app-id>/profile.yaml`` first, then the home's) with the path
that was used, or ``null`` when neither exists. A profile that does not parse
or holds a credential-shaped value refuses the context (``profile_invalid``),
so ``pretest start`` creates nothing.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import errors, proof
from . import CONTEXT_SCHEMA, store

DEFAULT_CAP_BYTES = 262_144
MAX_FUNCTIONS = 20
FUNCTION_LIMIT = 200
MAX_HINTS = 20
UNTRACKED_READ_LIMIT = 4 * 1024 * 1024

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
# Files whose hunk-header "function" is only a nearby line of data or prose.
NON_CODE_SUFFIXES = frozenset({
    ".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc", ".arb", ".json", ".jsonc",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".lock", ".properties",
    ".env", ".xml", ".plist", ".strings", ".html", ".svg"})
# Statements that name a dependency, not the enclosing function or class.
_IMPORT_LINE = re.compile(
    r"^\s*+(?:import\b|export\s++(?:\*|\{|['\"])|package\b|library\b|part\b|"
    r"using\b|from\s++\S++\s++import\b|#\s*+(?:include|import)\b|@import\b|"
    r"(?:const|let|var)\s++\w++\s*+=\s*+require\()")


def keeps_functions(path: str) -> bool:
    """Whether hunk-header function context is kept for ``path`` (code only)."""
    name = path.rsplit("/", 1)[-1].lower()
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return suffix not in NON_CODE_SUFFIXES and not name.startswith(".env")


def is_import_line(text: str) -> bool:
    return bool(_IMPORT_LINE.match(text or ""))
_STATUS_MAP = {"M": "M", "A": "A", "D": "D", "R": "R", "C": "A", "T": "M"}


def _git_bytes(repo: Path, argv: list[str], what: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.quotepath=off", *argv],
            cwd=repo, capture_output=True, timeout=60, check=False)
    except FileNotFoundError:
        raise errors.AutonomError(errors.BACKEND_FAILED, "git is not on PATH",
                                  hint="The pre-test reads the diff with git.") from None
    except subprocess.TimeoutExpired:
        raise errors.AutonomError(errors.BACKEND_FAILED, f"git {what} timed out",
                                  hint="Check the repository is local and not locked.") from None
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()[:300]
        raise errors.AutonomError(
            errors.BACKEND_FAILED, f"git {what} failed: {message}",
            hint="Run from inside the repository; --base and --head must exist locally.")
    return completed.stdout


def _range(base: str, head: str | None) -> str:
    return f"{base}...{head}" if head else base


def verify_ref(repo: Path, ref: Any, flag: str) -> str:
    """Refuse a ref git could read as an option, then require it to name a
    commit. Runs before any diff: ``--base=--output=FILE`` must never reach
    ``git diff``."""
    if not isinstance(ref, str) or not ref.strip():
        raise errors.AutonomError(errors.INVALID_VALUE, f"{flag} needs a git ref",
                                  hint=f"For example {flag} main.")
    if ref.startswith("-") or any(ord(char) < 32 or char == "\x7f" for char in ref):
        raise errors.AutonomError(
            errors.INVALID_VALUE, f"{flag} {ref!r} is not a git ref",
            hint=f"Name a branch, tag or commit, e.g. {flag} main; a ref never "
                 "starts with '-'.", ref=ref)
    _git_bytes(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                      f"{ref}^{{commit}}"], f"rev-parse {ref}")
    return ref


def _z_fields(raw: bytes) -> list[str]:
    return [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0")]


def _name_status(repo: Path, target: str) -> list[dict[str, Any]]:
    fields = _z_fields(_git_bytes(repo, ["diff", "-M", "--name-status", "-z", "--end-of-options",
                                         target, "--"],
                                  f"diff --name-status {target}"))
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        code = fields[index]
        if not code:
            index += 1
            continue
        letter = code[0]
        if letter in ("R", "C"):
            if index + 2 >= len(fields):
                break
            old, new = fields[index + 1], fields[index + 2]
            entry = {"path": new, "status": _STATUS_MAP[letter]}
            if letter == "R":
                entry["renamed_from"] = old
            out.append(entry)
            index += 3
        else:
            if index + 1 >= len(fields):
                break
            out.append({"path": fields[index + 1], "status": _STATUS_MAP.get(letter, "M")})
            index += 2
    return out


def _binary_paths(repo: Path, target: str) -> set[str]:
    """Paths numstat reports as ``-\t-`` (binary)."""
    fields = _z_fields(_git_bytes(repo, ["diff", "-M", "--numstat", "-z", "--end-of-options",
                                         target, "--"],
                                  f"diff --numstat {target}"))
    binary: set[str] = set()
    index = 0
    while index < len(fields):
        head = fields[index]
        if not head:
            index += 1
            continue
        parts = head.split("\t")
        if len(parts) >= 3 and parts[2] == "":
            # rename: "added\tdeleted\t" then old, new
            new = fields[index + 2] if index + 2 < len(fields) else ""
            if parts[0] == "-" and parts[1] == "-":
                binary.add(new)
            index += 3
            continue
        if len(parts) >= 3 and parts[0] == "-" and parts[1] == "-":
            binary.add("\t".join(parts[2:]))
        index += 1
    return binary


def _unquote(path: str) -> str:
    """git's C-style quoting of unusual paths (``"a\\tb"``)."""
    if not (len(path) >= 2 and path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1]
    raw = bytearray()
    index = 0
    simple = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "v": 11}
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            nxt = body[index + 1]
            if nxt in simple:
                raw.append(simple[nxt])
                index += 2
                continue
            if re.match(r"[0-7]{3}", body[index + 1:index + 4]):
                raw.append(int(body[index + 1:index + 4], 8))
                index += 4
                continue
        raw.extend(char.encode("utf-8"))
        index += 1
    return raw.decode("utf-8", "replace")


def _hunks(repo: Path, target: str, cap_bytes: int,
           ) -> tuple[dict[str, dict[str, list]], bool, int]:
    """-> ({path: {"ranges", "functions"}}, truncated, bytes read). Reads at most
    ``cap_bytes`` of hunk data, then stops the git process."""
    # Explicit prefixes: diff.mnemonicPrefix, diff.noprefix or diff.dstPrefix
    # would otherwise change the "+++ b/" names the parser matches.
    argv = ["git", "-c", "core.quotepath=off", "diff", "-U0", "-M", "--no-color",
            "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", "--end-of-options",
            target, "--"]
    try:
        process = subprocess.Popen(argv, cwd=repo, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise errors.AutonomError(errors.BACKEND_FAILED, "git is not on PATH",
                                  hint="The pre-test reads the diff with git.") from None
    found: dict[str, dict[str, list]] = {}
    current: dict[str, list] | None = None
    in_header = False
    used = 0
    truncated = False
    assert process.stdout is not None
    try:
        for raw in process.stdout:
            used += len(raw)
            if used > cap_bytes:
                truncated = True
                break
            line = raw.decode("utf-8", "surrogateescape").rstrip("\n")
            if line.startswith("diff --git "):
                current = None
                in_header = True
                continue
            # Only the file header names a file: an added line "++ x" shows
            # as "+++ x" inside a hunk and must stay content.
            if in_header and line.startswith("+++ "):
                # git ends a name holding a space with a tab; a real tab is quoted
                name = line[4:].rstrip("\t")
                if name == "/dev/null":
                    current = None
                    continue
                name = _unquote(name)
                if name.startswith("b/"):
                    name = name[2:]
                current = found.setdefault(name, {"ranges": [], "functions": [],
                                                  "code": keeps_functions(name)})
                continue
            match = _HUNK.match(line) if line.startswith("@@ ") else None
            if match:
                in_header = False
            if match and current is not None:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                if count > 0:
                    current["ranges"].append([start, start + count - 1])
                # The function context is git's nearest preceding top-level
                # line, which in a .env or settings file can be a credential:
                # it is masked like any free text, then cut (contract 8.3).
                raw_function = match.group(3)
                function = store.clip(raw_function, FUNCTION_LIMIT) \
                    if raw_function.strip() and current.get("code") \
                    and not is_import_line(raw_function) else ""
                if function and function not in current["functions"] \
                        and len(current["functions"]) < MAX_FUNCTIONS:
                    current["functions"].append(function)
    finally:
        if truncated:
            process.kill()
        process.stdout.close()
        stderr = process.stderr.read() if process.stderr else b""
        if process.stderr:
            process.stderr.close()
        code = process.wait()
    if not truncated and code != 0:
        raise errors.AutonomError(
            errors.BACKEND_FAILED,
            f"git diff -U0 {target} failed: {stderr.decode('utf-8', 'replace').strip()[:300]}",
            hint="Run from inside the repository; --base and --head must exist locally.")
    return found, truncated, min(used, cap_bytes)


def _untracked(repo: Path) -> list[str]:
    raw = _git_bytes(repo, ["ls-files", "--others", "--exclude-standard", "--full-name", "-z"],
                     "ls-files --others")
    return [name for name in _z_fields(raw) if name]


def diff_files(repo: str | Path, base: str, head: str | None,
               cap_bytes: int = DEFAULT_CAP_BYTES) -> dict[str, Any]:
    repo_path = Path(repo)
    verify_ref(repo_path, base, "--base")
    if head is not None:
        verify_ref(repo_path, head, "--head")
    target = _range(base, head)
    entries = _name_status(repo_path, target)
    binary = _binary_paths(repo_path, target)
    hunks, truncated, used = _hunks(repo_path, target, cap_bytes)
    files: list[dict[str, Any]] = []
    for entry in entries:
        path = entry["path"]
        info = hunks.get(path, {"ranges": [], "functions": []})
        item: dict[str, Any] = {"path": path, "status": entry["status"]}
        if "renamed_from" in entry:
            item["renamed_from"] = entry["renamed_from"]
        item["binary"] = path in binary
        item["ranges"] = [] if item["binary"] else info["ranges"]
        item["functions"] = [] if item["binary"] else info["functions"]
        files.append(item)
    if head is None:
        # Like proof: against the working tree a new, never-added file is a change.
        listed = {item["path"] for item in files}
        for name in _untracked(repo_path):
            if name in listed:
                continue
            item = {"path": name, "status": "A", "binary": False, "ranges": [],
                    "functions": []}
            try:
                with open(repo_path / name, "rb") as handle:
                    data = handle.read(UNTRACKED_READ_LIMIT + 1)
            except OSError:
                data = b""
            if b"\0" in data[:8000]:
                item["binary"] = True
            elif data:
                if truncated or used + len(data) > cap_bytes or len(data) > UNTRACKED_READ_LIMIT:
                    truncated = True
                else:
                    used += len(data)
                    lines = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
                    item["ranges"] = [[1, lines]]
            files.append(item)
    return {"changed_files": files, "diff_truncated": truncated}


# --- Runtime Map hints -------------------------------------------------------------

_STOP_WORDS = {
    "lib", "src", "test", "tests", "main", "dart", "kotlin", "java", "swift", "app",
    "apps", "core", "common", "shared", "util", "utils", "impl", "index", "page",
    "screen", "view", "widget", "widgets", "model", "models", "file", "files",
    "android", "ios", "assets", "images", "json", "yaml", "autonom", "flows",
}
_SPLIT = re.compile(r"[^A-Za-z0-9А-Яа-яЁё]+")
_CAMEL = re.compile(r"(?<=[a-zа-я])(?=[A-ZА-Я])")


def _tokens(text: str) -> list[str]:
    words: list[str] = []
    for chunk in _SPLIT.split(_CAMEL.sub(" ", text)):
        word = chunk.lower()
        if len(word) >= 4 and word not in _STOP_WORDS and not word.isdigit():
            if word not in words:
                words.append(word)
    return words


def _graph(app_id: str | None) -> dict[str, Any] | None:
    """The app's Runtime Map without creating its directory on a read."""
    if not app_id:
        return None
    from ..atlas import graph as graph_mod

    path = graph_mod.apps_home() / app_id / "atlas" / "graph.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def runtime_map_hints(app_id: str | None, changed: list[str],
                      areas: list[str]) -> list[dict[str, Any]]:
    graph = _graph(app_id)
    if not graph:
        return []
    area_tokens: list[tuple[str, str]] = []
    for name in changed:
        if name.startswith(".autonom/"):
            continue  # flow and profile files are test assets, not app areas
        parts = Path(name).parts
        area = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
        stemmed = "/".join([*parts[:-1], Path(name).stem]) if parts else name
        for token in _tokens(stemmed):
            if (token, area) not in area_tokens:
                area_tokens.append((token, area))
    hints: list[dict[str, Any]] = []
    screens = graph.get("screens")
    if not isinstance(screens, dict):
        return []
    for screen_id, screen in screens.items():
        if not isinstance(screen, dict):
            continue
        labels = [str(label) for label in (screen.get("labels") or [])]
        haystack = " ".join(labels).lower()
        matched = next((area for token, area in area_tokens if token in haystack), None)
        if matched is None:
            continue
        hints.append({"screen_id": str(screen_id), "labels": labels[:5],
                      "matched_area": matched, "hint": True})
        if len(hints) >= MAX_HINTS:
            break
    return hints


def build(repo: str | Path | None, base: str | None, head: str | None,
          flows_dir: str | Path | None, session: dict[str, Any] | None,
          app_id: str | None) -> dict[str, Any]:
    """``autonom.pretest-context/v1`` for ``repo`` between ``base`` and ``head``."""
    context: dict[str, Any] = {
        "schema": CONTEXT_SCHEMA,
        "repo": str(repo) if repo else None,
        "base": base,
        "head": head,
        "changed_files": [],
        "diff_truncated": False,
        "changed_areas": [],
        "flows": {"flows_dir": None, "selected": [], "invalid": [], "uncovered_files": []},
        "runtime_map_hints": [],
        "profile": None,
    }
    if not repo or not base:
        return context
    repo_path = Path(repo).resolve()
    from . import profile as profile_mod

    context["profile"] = profile_mod.context_block(repo_path, app_id)
    diff = diff_files(repo_path, base, head)
    names = list(proof.changed_files(repo_path, base, head))
    for item in diff["changed_files"]:
        if item["path"] not in names:
            names.append(item["path"])
    context["changed_files"] = diff["changed_files"]
    context["diff_truncated"] = diff["diff_truncated"]
    context["changed_areas"] = proof.changed_areas(names) if names else []
    directory = Path(flows_dir) if flows_dir else repo_path / ".autonom" / "flows"
    covered: list[str] = []
    if directory.is_dir():
        suite = proof.select_suite(directory, repo_path, names)
        context["flows"]["flows_dir"] = str(directory.resolve())
        context["flows"]["selected"] = [
            {"flow": str(Path(entry["path"]).resolve()), "reasons": list(entry["reasons"])}
            for entry in suite["selected"]]
        context["flows"]["invalid"] = list(suite["invalid"])
        covered = list(suite["covered"])
    context["flows"]["uncovered_files"] = [name for name in names if name not in covered]
    context["runtime_map_hints"] = runtime_map_hints(app_id, names, context["changed_areas"])
    return context
