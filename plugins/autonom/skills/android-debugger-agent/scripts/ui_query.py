#!/usr/bin/env python3
"""Select and optionally tap UI Automator nodes via file dump or live adb."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from ui_common import UiNode, filter_nodes, parse_nodes, visible_label


def parse_bool(value: str) -> bool:
    token = value.strip().lower()
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def adb_prefix(adb: str, serial: str | None) -> list[str]:
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    return cmd


def connected_devices(adb: str) -> list[str]:
    proc = subprocess.run(
        [adb, "devices"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "adb devices failed")
    found: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2 and cols[1] == "device":
            found.append(cols[0])
    return found


def resolve_serial(adb: str, serial: str | None) -> str:
    if serial:
        return serial
    devices = connected_devices(adb)
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise RuntimeError("no authorized adb device is connected")
    raise RuntimeError("multiple adb devices are connected; pass --serial")


def dump_live(adb: str, serial: str) -> str:
    cmd = [*adb_prefix(adb, serial), "exec-out", "uiautomator", "dump", "/dev/tty"]
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip() or "uiautomator dump failed")
    return proc.stdout


def select_matches(
    matches: list[UiNode], index: int | None, all_matches: bool
) -> list[UiNode]:
    if all_matches:
        return list(matches)
    if not matches:
        return []
    if index is not None:
        resolved = index if index >= 0 else len(matches) + index
        if resolved < 0 or resolved >= len(matches):
            raise IndexError(f"index {index} is outside {len(matches)} match(es)")
        return [matches[resolved]]
    if len(matches) > 1:
        preview = ", ".join(
            f"{i}:{visible_label(node)}" for i, node in enumerate(matches[:8])
        )
        raise RuntimeError(
            f"selector matched {len(matches)} nodes; add another selector or pass "
            f"--index. Matches: {preview}"
        )
    return [matches[0]]


def click_node(adb: str, serial: str, node: UiNode) -> None:
    box = node.bounds
    if box is None:
        raise RuntimeError("selected node has no bounds")
    x, y = box.center
    proc = subprocess.run(
        [*adb_prefix(adb, serial), "shell", "input", "tap", str(x), str(y)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip() or "adb tap failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query Android UI Automator XML (file or live adb dump)"
    )
    parser.add_argument("input", nargs="?", help="UI XML path; omit for live adb dump")
    parser.add_argument("--adb", default=shutil.which("adb") or "adb")
    parser.add_argument("--serial")
    parser.add_argument("--text")
    parser.add_argument("--desc", help="content-desc or Flutter semantics label")
    parser.add_argument("--resource-id")
    parser.add_argument("--class", dest="class_name")
    parser.add_argument("--package")
    parser.add_argument("--clickable", type=parse_bool)
    parser.add_argument("--enabled", type=parse_bool)
    parser.add_argument("--focusable", type=parse_bool)
    parser.add_argument("--scrollable", type=parse_bool)
    parser.add_argument("--selected", type=parse_bool)
    parser.add_argument("--checked", type=parse_bool)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--contains", action="store_true")
    mode_group.add_argument("--regex", action="store_true")
    parser.add_argument("--case-sensitive", action="store_true")
    parser.add_argument("--index", type=int)
    parser.add_argument("--all", action="store_true", dest="all_matches")
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to poll live UI")
    parser.add_argument("--interval", type=float, default=0.4)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--click", action="store_true")
    return parser


def _match_mode(args: argparse.Namespace) -> str:
    if args.regex:
        return "regex"
    if args.contains:
        return "contains"
    return "exact"


def _load_xml(args: argparse.Namespace, serial: str | None) -> str:
    if args.input:
        return Path(args.input).read_text(encoding="utf-8")
    return dump_live(args.adb, serial or "")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    selectors: dict[str, str | bool | None] = {
        "text": args.text,
        "desc": args.desc,
        "resource_id": args.resource_id,
        "class_name": args.class_name,
        "package": args.package,
        "clickable": args.clickable,
        "enabled": args.enabled,
        "focusable": args.focusable,
        "scrollable": args.scrollable,
        "selected": args.selected,
        "checked": args.checked,
    }
    if all(value is None for value in selectors.values()):
        parser.error("pass at least one selector")
    if args.input and args.wait > 0:
        parser.error("--wait is only valid for live adb queries")

    mode = _match_mode(args)
    serial: str | None = None
    if not args.input or args.click:
        try:
            serial = resolve_serial(args.adb, args.serial)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    deadline = time.monotonic() + max(0.0, args.wait)
    matches: list[UiNode] = []
    while True:
        try:
            xml_text = _load_xml(args, serial)
            matches = filter_nodes(
                parse_nodes(xml_text),
                selectors,
                mode=mode,
                case_sensitive=args.case_sensitive,
            )
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if matches or time.monotonic() >= deadline:
            break
        time.sleep(max(0.05, args.interval))

    try:
        selected = select_matches(matches, args.index, args.all_matches)
    except (RuntimeError, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    if not selected:
        print("error: no matching UI node found", file=sys.stderr)
        return 4

    if args.click:
        if len(selected) != 1:
            print("error: --click requires exactly one selected node", file=sys.stderr)
            return 3
        try:
            click_node(args.adb, serial or "", selected[0])
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 5

    body: dict[str, Any] = {
        "match_count": len(matches),
        "selected_count": len(selected),
        "clicked": bool(args.click),
        "nodes": [node.as_dict() for node in selected],
    }
    if args.json or len(selected) > 1:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    else:
        node = selected[0]
        box = node.bounds
        if box is not None:
            x, y = box.center
            print(f"{x} {y}")
        else:
            print(json.dumps(node.as_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
