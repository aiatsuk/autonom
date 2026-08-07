#!/usr/bin/env python3
"""CLI: compact text summary of a UI Automator XML dump."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ui_common import parse_nodes, summarize


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a compact text tree from a UI Automator XML dump"
    )
    parser.add_argument("input", help="UI XML path, or '-' for stdin")
    parser.add_argument("output", nargs="?", help="optional path for the text summary")
    parser.add_argument("--max-depth", type=int, default=30)
    args = parser.parse_args()

    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        rendered = "\n".join(summarize(parse_nodes(raw), max_depth=args.max_depth)) + "\n"
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
