#!/usr/bin/env python3
"""Inspect Flutter / Dart / Android toolchain declarations in a repository.

Emits observed values only — never asserts that a version is "latest".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def first_match(patterns: Iterable[str], texts: Iterable[str]) -> str | None:
    compiled = [re.compile(p, re.MULTILINE) for p in patterns]
    for text in texts:
        if not text:
            continue
        for pattern in compiled:
            hit = pattern.search(text)
            if hit:
                return hit.group(1).strip()
    return None


def run(command: list[str], cwd: Path, timeout: int = 20) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env={**os.environ, "CI": "true"},
        )
        return {
            "command": command,
            "exit_code": completed.returncode,
            "output": completed.stdout.strip(),
        }
    except FileNotFoundError:
        return {"command": command, "error": "not found"}
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        return {"command": command, "error": "timeout", "output": partial.strip()}


def resolve_flutter(root: Path) -> list[str]:
    fvm_present = (root / ".fvmrc").exists() or (root / ".fvm").exists()
    if fvm_present and shutil.which("fvm"):
        return ["fvm", "flutter"]
    return ["flutter"]


_CANDIDATE_RELATIVE: Sequence[str] = (
    "pubspec.yaml",
    ".fvmrc",
    ".fvm/fvm_config.json",
    "android/settings.gradle",
    "android/settings.gradle.kts",
    "android/build.gradle",
    "android/build.gradle.kts",
    "android/app/build.gradle",
    "android/app/build.gradle.kts",
    "android/gradle/libs.versions.toml",
    "android/gradle/wrapper/gradle-wrapper.properties",
    "settings.gradle",
    "settings.gradle.kts",
    "build.gradle",
    "build.gradle.kts",
    "app/build.gradle",
    "app/build.gradle.kts",
    "gradle/libs.versions.toml",
    "gradle/wrapper/gradle-wrapper.properties",
)


def project_files(root: Path) -> list[Path]:
    return [root / rel for rel in _CANDIDATE_RELATIVE if (root / rel).exists()]


def inspect(root: Path, execute: bool) -> dict[str, Any]:
    files = project_files(root)
    bodies = [read(path) for path in files]
    pubspec = read(root / "pubspec.yaml")

    is_flutter = bool(
        pubspec and re.search(r"^\s*flutter\s*:\s*$", pubspec, re.MULTILINE)
    )
    is_dart = bool(pubspec)
    has_android = (root / "android").is_dir() or any(
        (root / name).exists() for name in ("settings.gradle", "settings.gradle.kts")
    )

    wrapper = first_match(
        [r"distributionUrl=.*gradle-([0-9][0-9A-Za-z.+-]*)-(?:all|bin)\.zip"],
        bodies,
    )
    compile_sdk = first_match(
        [r"compileSdk\s*[=:]?\s*(\d+)", r"compileSdkVersion\s+(\d+)"],
        bodies,
    )
    target_sdk = first_match(
        [r"targetSdk\s*[=:]?\s*(\d+)", r"targetSdkVersion\s+(\d+)"],
        bodies,
    )
    min_sdk = first_match(
        [r"minSdk\s*[=:]?\s*(\d+)", r"minSdkVersion\s+(\d+)"],
        bodies,
    )
    agp = first_match(
        [
            r"id\(\s*[\"']com\.android\.(?:application|library)[\"']\s*\)\s+version\s+[\"']([^\"']+)",
            r"com\.android\.(?:application|library)[\"']?\s+version\s+[\"']([^\"']+)",
            r"com\.android\.tools\.build:gradle:([^\"'\s]+)",
            r"^agp\s*=\s*[\"']([^\"']+)",
        ],
        bodies,
    )
    kotlin = first_match(
        [
            r"id\(\s*[\"']org\.jetbrains\.kotlin\.android[\"']\s*\)\s+version\s+[\"']([^\"']+)",
            r"org\.jetbrains\.kotlin\.android[\"']?\s+version\s+[\"']([^\"']+)",
            r"kotlin_version\s*=\s*[\"']([^\"']+)",
            r"^kotlin\s*=\s*[\"']([^\"']+)",
        ],
        bodies,
    )
    dart_constraint = first_match([r"^\s*sdk:\s*[\"']?([^\n\"']+)"], [pubspec])
    flutter_constraint = first_match(
        [r"^\s*flutter:\s*[\"']([^\"']+)[\"']"], [pubspec]
    )

    snapshot: dict[str, Any] = {
        "root": str(root),
        "project": {
            "flutter": is_flutter,
            "dart": is_dart,
            "android": has_android,
        },
        "declared": {
            "dart_sdk": dart_constraint,
            "flutter_sdk": flutter_constraint,
            "gradle": wrapper,
            "android_gradle_plugin": agp,
            "kotlin": kotlin,
            "compile_sdk": compile_sdk,
            "target_sdk": target_sdk,
            "min_sdk": min_sdk,
        },
        "files_inspected": [str(path.relative_to(root)) for path in files],
        "policy": "Observed values only; no value is asserted to be latest.",
    }

    if execute:
        commands: dict[str, Any] = {}
        flutter = resolve_flutter(root)
        commands["flutter_version"] = run([*flutter, "--version", "--machine"], root)
        commands["flutter_doctor"] = run([*flutter, "doctor", "-v"], root, timeout=60)
        commands["dart_version"] = run(["dart", "--version"], root)
        commands["java_version"] = run(["java", "-version"], root)
        commands["adb_devices"] = run(["adb", "devices", "-l"], root)
        gradlew = (
            root / "android" / "gradlew"
            if (root / "android" / "gradlew").exists()
            else root / "gradlew"
        )
        if gradlew.exists():
            commands["gradle_version"] = run(
                [str(gradlew), "--version"], gradlew.parent, timeout=60
            )
        snapshot["commands"] = commands
    return snapshot


def render_text(snapshot: dict[str, Any]) -> str:
    lines = [f"Root: {snapshot['root']}"]
    detected = [name for name, on in snapshot["project"].items() if on]
    lines.append("Detected: " + (", ".join(detected) if detected else "unknown"))
    lines.append("Declared toolchain:")
    for key, value in snapshot["declared"].items():
        lines.append(f"  {key}: {value or '<not found>'}")
    if "commands" in snapshot:
        lines.append("Local commands:")
        for name, value in snapshot["commands"].items():
            status = value.get("exit_code", value.get("error", "unknown"))
            first_line = value.get("output", "").splitlines()[:1]
            suffix = f" — {first_line[0]}" if first_line else ""
            lines.append(f"  {name}: {status}{suffix}")
    lines.append(snapshot["policy"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Flutter and Android toolchain declarations"
    )
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="also run installed toolchain commands",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"Root does not exist: {root}", file=sys.stderr)
        return 2

    snapshot = inspect(root, args.execute)
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    else:
        print(render_text(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
