#!/usr/bin/env python3
"""Resolve and invoke the Flutter SDK that the repository pin intends.

Order of preference:
1. Repository-local FVM SDK (``.fvm/flutter_sdk/bin/flutter``)
2. ``fvm flutter`` when the repo is FVM-pinned and ``fvm`` is on PATH
3. System ``flutter`` on PATH (only when the repo is not FVM-pinned, or when
   ``--allow-system`` is set)
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def has_fvm_config(root: Path) -> bool:
    return (root / ".fvmrc").is_file() or (root / ".fvm/fvm_config.json").is_file()


def resolve_flutter(root: Path, *, allow_system_with_fvm: bool = False) -> list[str]:
    bundled = root / ".fvm" / "flutter_sdk" / "bin" / "flutter"
    if bundled.is_file():
        return [str(bundled)]

    if has_fvm_config(root):
        if shutil.which("fvm"):
            return ["fvm", "flutter"]
        if not allow_system_with_fvm:
            raise RuntimeError(
                "This repository is FVM-pinned, but neither "
                ".fvm/flutter_sdk/bin/flutter nor fvm is available. "
                "Install/select the pinned SDK or pass --allow-system explicitly."
            )

    on_path = shutil.which("flutter")
    if on_path:
        return [on_path]
    raise RuntimeError(
        "Flutter was not found in the repository FVM directory or on PATH"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Flutter SDK selected by the active repository"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--allow-system",
        action="store_true",
        help="allow PATH Flutter even when the repository is FVM-pinned",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="print the resolved command and exit",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()

    root = Path(options.root).resolve()
    flutter_args = (
        options.args[1:] if options.args[:1] == ["--"] else options.args
    )

    try:
        command = [
            *resolve_flutter(root, allow_system_with_fvm=options.allow_system),
            *flutter_args,
        ]
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if options.print_only:
        print(shlex.join(command))
        return 0

    completed = subprocess.run(command, cwd=root, env=os.environ.copy(), check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
