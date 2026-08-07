#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass
class Problem:
    path: Path
    message: str


def load_json(path: Path, problems: list[Problem]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(Problem(path, "file is missing"))
        return {}
    except json.JSONDecodeError as exc:
        problems.append(Problem(path, f"invalid JSON: {exc}"))
        return {}
    if not isinstance(value, dict):
        problems.append(Problem(path, "top-level JSON value must be an object"))
        return {}
    return value


def parse_frontmatter(path: Path, problems: list[Problem]) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        problems.append(Problem(path, "SKILL.md is missing"))
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        problems.append(Problem(path, "missing YAML frontmatter opener"))
        return {}
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        problems.append(Problem(path, "missing YAML frontmatter closer"))
        return {}
    result: dict[str, str] = {}
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            problems.append(Problem(path, f"unsupported frontmatter line: {raw!r}"))
            continue
        key, value = raw.split(":", 1)
        value = value.strip()
        # A strict YAML parser (Claude Code's) reads an unquoted scalar containing
        # ": " as a nested mapping and drops the whole frontmatter silently. Our
        # own naive split tolerates it, so catch the hazard explicitly.
        if value and value[0] not in "\"'" and ": " in value:
            problems.append(Problem(
                path, f"frontmatter '{key.strip()}' has an unquoted ': ' — quote the value "
                      "or replace the colon (Claude Code drops all metadata otherwise)"
            ))
        result[key.strip()] = value.strip('"\'')
    return result


def read_lib_version(root: Path, problems: list[Problem]) -> str | None:
    init_path = root / "scripts/autonom_lib/__init__.py"
    try:
        text = init_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        problems.append(Problem(init_path, "version source is missing"))
        return None
    match = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', text)
    if not match:
        problems.append(Problem(init_path, "cannot read __version__"))
        return None
    return match.group(1)


def validate(root: Path) -> list[Problem]:
    problems: list[Problem] = []
    lib_version = read_lib_version(root, problems)
    marketplace_path = root / ".agents/plugins/marketplace.json"
    marketplace = load_json(marketplace_path, problems)
    entries = marketplace.get("plugins")
    if not isinstance(entries, list) or not entries:
        problems.append(Problem(marketplace_path, "plugins must be a non-empty array"))
        return problems

    plugin_names: set[str] = set()
    skill_names: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            problems.append(Problem(marketplace_path, "every plugin entry must be an object"))
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            problems.append(Problem(marketplace_path, "plugin entry is missing name"))
            continue
        if name in plugin_names:
            problems.append(Problem(marketplace_path, f"duplicate plugin name: {name}"))
        plugin_names.add(name)

        source = entry.get("source")
        source_path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(source_path, str):
            problems.append(Problem(marketplace_path, f"{name}: missing local source path"))
            continue
        plugin_root = (root / source_path).resolve()
        if not plugin_root.is_relative_to(root.resolve()):
            problems.append(Problem(marketplace_path, f"{name}: source escapes repository root"))
            continue
        if not plugin_root.is_dir():
            problems.append(Problem(plugin_root, "plugin source directory is missing"))
            continue

        manifest_path = plugin_root / ".codex-plugin/plugin.json"
        manifest = load_json(manifest_path, problems)
        if manifest.get("name") != name:
            problems.append(Problem(manifest_path, "manifest name must match marketplace name"))
        version = manifest.get("version")
        if not isinstance(version, str) or not SEMVER.match(version):
            problems.append(Problem(manifest_path, "version must be semantic x.y.z"))
        elif lib_version and version != lib_version:
            problems.append(
                Problem(manifest_path, f"version {version} does not match library {lib_version}")
            )
        skills_rel = manifest.get("skills", "./skills/")
        if not isinstance(skills_rel, str):
            problems.append(Problem(manifest_path, "skills must be a relative path string"))
            continue
        skills_root = (plugin_root / skills_rel).resolve()
        if not skills_root.is_dir():
            problems.append(Problem(skills_root, "skills directory is missing"))
            continue

        for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
            skill_path = skill_dir / "SKILL.md"
            meta = parse_frontmatter(skill_path, problems)
            skill_name = meta.get("name")
            description = meta.get("description", "")
            if skill_name != skill_dir.name:
                problems.append(Problem(skill_path, f"name must equal directory: {skill_dir.name}"))
            if skill_name in skill_names:
                problems.append(Problem(skill_path, f"duplicate skill name: {skill_name}"))
            if skill_name:
                skill_names.add(skill_name)
            if len(description) < 30:
                problems.append(Problem(skill_path, "description is too short for reliable routing"))
            if len(description) > 1536:
                problems.append(
                    Problem(skill_path, "description exceeds the 1,536-char skill-listing budget")
                )

    claude_marketplace_path = root / ".claude-plugin/marketplace.json"
    claude_marketplace = load_json(claude_marketplace_path, problems)
    claude_entries = claude_marketplace.get("plugins")
    if not isinstance(claude_entries, list) or not claude_entries:
        problems.append(Problem(claude_marketplace_path, "plugins must be a non-empty array"))
    else:
        if not claude_marketplace.get("name"):
            problems.append(Problem(claude_marketplace_path, "marketplace name is required"))
        owner = claude_marketplace.get("owner")
        if not isinstance(owner, dict) or not owner.get("name"):
            problems.append(Problem(claude_marketplace_path, "owner.name is required"))
        for entry in claude_entries:
            if not isinstance(entry, dict):
                problems.append(
                    Problem(claude_marketplace_path, "every plugin entry must be an object")
                )
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                problems.append(Problem(claude_marketplace_path, "plugin entry is missing name"))
                continue
            source = entry.get("source")
            if not isinstance(source, str) or not source:
                problems.append(
                    Problem(claude_marketplace_path, f"{name}: source must be a relative path")
                )
                continue
            plugin_root = (root / source).resolve()
            if not plugin_root.is_relative_to(root.resolve()) or not plugin_root.is_dir():
                problems.append(
                    Problem(
                        claude_marketplace_path,
                        f"{name}: source directory is missing or escapes the repository",
                    )
                )
                continue
            manifest_path = plugin_root / ".claude-plugin/plugin.json"
            manifest = load_json(manifest_path, problems)
            if manifest.get("name") != name:
                problems.append(Problem(manifest_path, "manifest name must match marketplace name"))
            version = manifest.get("version")
            if not isinstance(version, str) or not SEMVER.match(version):
                problems.append(Problem(manifest_path, "version must be semantic x.y.z"))
            elif lib_version and version != lib_version:
                problems.append(
                    Problem(manifest_path, f"version {version} does not match library {lib_version}")
                )

    stale_markers = (
        "Verified stable baseline",
        "latest stable baseline",
        "Latest stable Kotlin",
    )
    for path in root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for marker in stale_markers:
            if marker in text:
                problems.append(Problem(path, f"stale-version marker is forbidden: {marker}"))

    for path in root.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        first = path.read_text(encoding="utf-8").splitlines()[:1]
        if path.parent.name == "scripts" and (not first or not first[0].startswith("#!")):
            problems.append(Problem(path, "executable Python helper must have a shebang"))

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an Autonom repository")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    problems = validate(root)
    if problems:
        for problem in problems:
            try:
                display = problem.path.relative_to(root)
            except ValueError:
                display = problem.path
            print(f"ERROR {display}: {problem.message}", file=sys.stderr)
        print(f"Validation failed with {len(problems)} problem(s).", file=sys.stderr)
        return 1
    skill_count = sum(1 for _ in (root / "plugins/autonom/skills").glob("*/SKILL.md"))
    print(f"Validated Codex and Claude manifests and {skill_count} skills.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
